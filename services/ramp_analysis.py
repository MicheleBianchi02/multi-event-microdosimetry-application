"""
Ramp-sample analysis (AQR1 files) — the data layer of the standalone
``--ramp`` window (``views/ramp_analysis_window.py``).

Pure numpy on top of the AQR1 reader of ``services/ramp_storage.py``: one
channel of a published ramp folder is loaded as a *gap-aware* window
sequence, counts are converted to volts, and the three analysis primitives
the window needs are provided:

- :meth:`RampChannel.averaged_psd` — the ensemble-averaged power spectrum:
  the one-sided PSD of every valid window (same normalization as
  ``core.calc_stat.one_sided_psd``) averaged across windows, with the
  across-window standard deviation.  All windows share the length
  ``samples_per_window`` and the dt recorded in the header, so the
  per-window frequency axes coincide exactly — no resampling.
- :attr:`RampChannel.stats` — per-sample-index mean/std across all valid
  windows (the reference band) and the per-window railed-sample counts
  (samples pinned at either full scale: with the NI's clamping semantics
  that is the overflow indication).
- :func:`derivative` — per-window derivative, consecutive-difference or
  centered moving least-squares slope.

Gap awareness: the ``.bin`` data section is a contiguous append of the
samples that *arrived* — a dropped block (recorded as a gap in
``index.json``) means every later sample sits at an earlier file offset.
Windows are re-anchored through the gaps' absolute sample indices; a window
straddling a gap is discarded, never silently misaligned (the same contract
as the live window extractor).  Without gaps the mapping degenerates to the
plain reshape.
"""
import json
import os
from dataclasses import dataclass

import numpy as np

from services.ramp_storage import (
    MODE_NAMES,
    STATE_ABORTED,
    STATE_FINALIZED,
    STATE_RUNNING,
    open_ramp_file,
)

DETREND_MODES = ("linear", "mean", "none")
TAPER_MODES = ("hann", "rect")
DERIVATIVE_MODES = ("diff", "linear")

STATE_NAMES = {
    STATE_RUNNING: "running",
    STATE_FINALIZED: "finalized",
    STATE_ABORTED: "aborted",
}


# ---------------------------------------------------------------------------
# Folder / archive discovery
# ---------------------------------------------------------------------------

def _recorded_ramp_folder(workspace_dir: str, folder: str) -> str | None:
    """Resolve a ramp folder recorded in a settings.json against the
    workspace root.  Archives saved on Windows record the relative path
    with backslashes ('ramp\\test_3') — normalize them, or on Linux the
    join looks for a single folder LITERALLY named 'ramp\\test_3' and
    every measurement silently disappears from the workspace scan
    (forward slashes work natively on Windows too, so this is safe
    everywhere)."""
    if not folder:
        return None
    folder_path = os.path.normpath(
        os.path.join(workspace_dir, folder.replace("\\", "/"))
    )
    return folder_path if os.path.isdir(folder_path) else None


def ramp_measurements_in_workspace(workspace_dir: str) -> list:
    """Every measurement of a workspace that published ramp files.

    Returns a list of dicts (newest first): {label, folder, zip_path},
    where label is the archive name plus its start timestamp.
    """
    from services.measurement_catalog import scan_workspace

    found = []
    for record in reversed(scan_workspace(workspace_dir)):
        ramp = (record.settings or {}).get("ramp") or {}
        folder_path = _recorded_ramp_folder(workspace_dir, ramp.get("folder"))
        if folder_path is None:
            continue
        stamp = record.timestamp_iso.replace("T", " ")[:19] if record.timestamp_iso else ""
        found.append(
            {
                "label": f"{record.filename}   {stamp}".strip(),
                "folder": folder_path,
                "zip_path": record.path,
            }
        )
    return found


def ramp_folder_from_zip(zip_path: str) -> str | None:
    """The ramp folder referenced by a measurement archive (the "ramp"
    section's folder is recorded relative to the workspace root, and the
    ZIP lives in that root).  None when the archive has no ramp section."""
    import zipfile

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "settings.json" not in zf.namelist():
                return None
            with zf.open("settings.json") as sf:
                settings = json.load(sf)
    except Exception:
        return None
    folder = (settings.get("ramp") or {}).get("folder")
    if not folder:
        return None
    return _recorded_ramp_folder(os.path.dirname(zip_path), folder)


# ---------------------------------------------------------------------------
# One channel: gap-aware window access over the AQR1 memmap
# ---------------------------------------------------------------------------

@dataclass
class _Run:
    """One contiguous stretch of file samples covering a contiguous stretch
    of absolute sample indices (between two gaps)."""

    abs_start: int    # absolute sample index of the run's first sample
    file_start: int   # its position in the file, in samples
    length: int       # samples in the run


def _merge_gaps(gaps) -> list:
    """Sorted, merged [abs_start, missing] ranges (the index may contain
    overlapping/duplicate records)."""
    ranges = sorted(
        (int(g[0]), int(g[0]) + int(g[1])) for g in (gaps or []) if int(g[1]) > 0
    )
    merged = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


class RampChannel:
    """One channel's ramp file: header facts plus gap-aware access to the
    integration windows, in acquisition order."""

    def __init__(self, path: str, gaps=None):
        header, view = open_ramp_file(path)
        self.path = path
        self.header = header
        self.view = view                     # uint32 memmap, len = samples_written
        self.n = int(header["samples_per_window"])
        self.dt = float(header["ramp_dt_s"])
        self.bits = int(header["adc_bits"])
        self.vmin = float(header["range_min_v"])
        self.vmax = float(header["range_max_v"])
        if self.n < 2:
            raise ValueError(f"{path}: degenerate window size {self.n}")
        if self.dt <= 0:
            raise ValueError(f"{path}: invalid ramp dt {self.dt}")

        written = int(view.shape[0])
        runs = self._build_runs(_merge_gaps(gaps), written)
        self.window_ids, self.window_offsets = self._map_windows(runs)
        self._stats = None                   # lazy ensemble statistics

    # ------------------------------------------------------------- mapping

    @staticmethod
    def _build_runs(merged_gaps: list, written: int) -> list:
        """File-sample runs between the gaps: file position <-> absolute
        sample index.  After a gap the absolute index jumps forward while
        the file position does not — the samples in between never arrived."""
        runs = []
        file_pos = 0
        abs_pos = 0
        for lo, hi in merged_gaps:
            if lo > abs_pos and file_pos < written:
                length = min(lo - abs_pos, written - file_pos)
                if length > 0:
                    runs.append(_Run(abs_pos, file_pos, length))
                    file_pos += length
                    abs_pos += length
            abs_pos = max(abs_pos, hi)
        if file_pos < written:
            runs.append(_Run(abs_pos, file_pos, written - file_pos))
        return runs

    def _map_windows(self, runs: list) -> tuple:
        """Windows fully contained in one run (acquisition ids in order) and
        their file offsets.  A window straddling a gap is dropped."""
        n = self.n
        ids, offsets = [], []
        for run in runs:
            first = -(-run.abs_start // n)                 # ceil: first whole window
            last = (run.abs_start + run.length) // n       # exclusive
            for w in range(first, last):
                ids.append(w)
                offsets.append(run.file_start + (w * n - run.abs_start))
        return np.asarray(ids, dtype=np.int64), np.asarray(offsets, dtype=np.int64)

    # ------------------------------------------------------------- basics

    @property
    def label(self) -> str:
        return self.header["channel_label"] or os.path.basename(self.path)

    @property
    def n_windows(self) -> int:
        return int(self.window_ids.shape[0])

    @property
    def timing_mode(self) -> str:
        return MODE_NAMES.get(self.header["timing_mode"], "internal")

    @property
    def state(self) -> str:
        return STATE_NAMES.get(self.header["state"], "unknown")

    def to_volts(self, counts: np.ndarray) -> np.ndarray:
        """V = V_min + counts/(2^bits − 1) · span (the data.csv convention)."""
        return self.vmin + (counts / float(2**self.bits - 1)) * (self.vmax - self.vmin)

    def window(self, w_id: int) -> np.ndarray:
        """One window (volts) by its acquisition id."""
        return self.to_volts(self.window_counts(w_id))

    def window_counts(self, w_id: int) -> np.ndarray:
        """One window's raw uint32 counts by its acquisition id."""
        pos = int(np.searchsorted(self.window_ids, w_id))
        if pos >= self.n_windows or self.window_ids[pos] != w_id:
            raise KeyError(f"ramp window {w_id} is not available (gap?)")
        off = int(self.window_offsets[pos])
        return np.asarray(self.view[off : off + self.n], dtype=np.float64)

    def windows_batch(self, ids) -> np.ndarray:
        """A (len(ids), n) volts matrix of the requested windows."""
        ids = np.asarray(sorted(ids), dtype=np.int64)
        pos = np.searchsorted(self.window_ids, ids)
        if not np.array_equal(self.window_ids[pos], ids):
            raise KeyError("a requested ramp window is not available (gap?)")
        idx = self.window_offsets[pos][:, None] + np.arange(self.n)[None, :]
        return self.to_volts(self.view[idx].astype(np.float64))

    def _iter_window_slices(self, batch: int = 256):
        """Yield (ids, offsets) over every valid window, in order — one
        gather per batch is all any whole-ensemble pass needs."""
        for start in range(0, self.n_windows, batch):
            stop = min(start + batch, self.n_windows)
            yield self.window_ids[start:stop], self.window_offsets[start:stop]

    def iter_batches(self, batch: int = 256):
        """Yield (ids, volts-matrix) over every valid window, in order —
        the streaming entry point for whole-ensemble passes."""
        n_ar = np.arange(self.n)
        for ids, offs in self._iter_window_slices(batch):
            idx = offs[:, None] + n_ar[None, :]
            yield ids, self.to_volts(self.view[idx].astype(np.float64))

    # ------------------------------------------------------------- stats

    @property
    def has_stats(self) -> bool:
        """Whether the ensemble statistics were already computed (accessing
        :attr:`stats` computes them — a whole-file pass)."""
        return self._stats is not None

    @property
    def stats(self) -> dict:
        """Ensemble statistics over every valid window (computed once):
        per-sample mean/std (the reference band) and per-window counts of
        railed samples (pinned at either full scale)."""
        if self._stats is None:
            self.compute_stats()
        return self._stats

    def compute_stats(self, progress=None) -> dict:
        """The whole-file ensemble pass behind :attr:`stats`.

        ``progress(done_windows, total_windows)`` is called after every
        batch — for background workers driving a progress bar; pass None
        to skip.  The result is cached on the channel (``stats`` returns
        it without re-reading the file).
        """
        full = 2**self.bits - 1
        n_ar = np.arange(self.n)
        sum_v = np.zeros(self.n)
        sum_sq = np.zeros(self.n)
        railed = np.zeros(self.n_windows, dtype=np.int64)
        count = 0
        total = self.n_windows
        for ids, offs in self._iter_window_slices():
            counts = self.view[offs[:, None] + n_ar[None, :]]
            volts = self.to_volts(counts.astype(np.float64))
            sum_v += volts.sum(axis=0)
            sum_sq += (volts**2).sum(axis=0)
            count += volts.shape[0]
            railed[np.searchsorted(self.window_ids, ids)] = (
                (counts == 0) | (counts == full)
            ).sum(axis=1)
            if progress is not None:
                progress(count, total)
        mean = sum_v / max(count, 1)
        var = np.zeros(self.n) if count < 2 else (sum_sq - count * mean**2) / (count - 1)
        self._stats = {
            "mean": mean,
            "std": np.sqrt(np.maximum(var, 0.0)),
            "count": count,
            "railed_per_window": railed,
            "railed_total": int(railed.sum()),
        }
        return self._stats

    # ------------------------------------------------------------- spectrum

    def averaged_psd(self, taper: str = "hann", detrend: str = "linear",
                     batch: int = 256):
        """Ensemble-averaged one-sided PSD over every valid window.

        Per window: optional detrend (linear LSQ line by default — the ramp
        trend otherwise dominates the spectrum as 1/f²), taper, rfft; the
        normalization is exactly ``core.calc_stat.one_sided_psd``'s (rect:
        |X|²/(fs·n), hann: |X|²/(fs·Σw²), interior bins folded ×2), so the
        values agree with the FFT analysis of the classic pipeline.

        Returns (freqs, psd_mean, psd_std) — the std is across windows —
        or (None, None, None) when there is nothing to average.
        """
        if self.n_windows == 0:
            return None, None, None
        if taper not in TAPER_MODES:
            raise ValueError(f"unknown taper {taper!r}")
        if detrend not in DETREND_MODES:
            raise ValueError(f"unknown detrend {detrend!r}")

        n = self.n
        fs = 1.0 / self.dt
        if taper == "hann":
            w = np.hanning(n)
            denom = fs * np.sum(w**2)
        else:
            w = None
            denom = fs * n

        sum_p = np.zeros(n // 2 + 1)
        sum_pq = np.zeros(n // 2 + 1)
        count = 0
        for _, volts in self.iter_batches(batch):
            x = _detrend(volts, detrend)
            if w is not None:
                x = x * w[None, :]
            psd = (np.abs(np.fft.rfft(x, axis=1)) ** 2) / denom
            if n % 2 == 0:
                psd[:, 1:-1] *= 2.0
            else:
                psd[:, 1:] *= 2.0
            sum_p += psd.sum(axis=0)
            sum_pq += (psd**2).sum(axis=0)
            count += psd.shape[0]

        freqs = np.fft.rfftfreq(n, d=self.dt)
        mean = sum_p / count
        var = np.zeros_like(mean) if count < 2 else (sum_pq - count * mean**2) / (count - 1)
        return freqs, mean, np.sqrt(np.maximum(var, 0.0))

    def window_dbv_spectrum(self, w_id: int):
        """Plain rfft of ONE ramp window, as dBV per FFT line.

        The oscilloscope Spectrum convention: rms-normalized lines
        (ms = 2·(|X|/n)², dBV = 10·log10(ms) — a sine of peak amplitude A
        reads A/sqrt(2) in its line, and e_n = 10^(dBV/20)/sqrt(df)
        recovers the one-sided noise density).  Rectangular window, no
        detrend, no taper; the window mean is removed (the DC line
        carries no information) and the DC and Nyquist lines are dropped
        (the latter has no negative-frequency twin — 6 dB low by
        convention).  Raises KeyError for an unavailable window (gap).
        """
        x = self.window(w_id)
        n = x.size
        spec = np.abs(np.fft.rfft(x - x.mean()))
        ms = 2.0 * (spec / n) ** 2  # V^2 rms per FFT line
        freqs = np.fft.rfftfreq(n, d=self.dt)
        if n % 2 == 0:
            ms, freqs = ms[1:-1], freqs[1:-1]  # drop DC and Nyquist
        else:
            ms, freqs = ms[1:], freqs[1:]  # odd n: no Nyquist line
        return freqs, 10.0 * np.log10(np.maximum(ms, 1e-30))

    def lag_difference_spectrum(self, m: int, batch: int = 256, progress=None):
        """Ensemble-averaged spectrum of the non-overlapping lag-m
        difference d_k = v[(k+1)·m] − v[k·m], averaged over all windows.

        The difference sequence is sampled once per Δt = m·dt, so its
        spectrum is computed on that grid: the Nyquist frequency is
        1/(2·m·dt) — it SHRINKS with m — and the differencing transfer
        function 2|sin(πf·Δt)| has its first zero at f = 1/Δt = 2×Nyquist,
        i.e. exactly outside the band: no in-band zeros.  (Everything
        above 1/(2Δt) folds into the band — that is not an artifact but
        precisely what a gated measurement of the Δt-increment sees.)
        Each window yields n//m difference samples, so the frequency
        resolution stays ~1/T_window for every m.

        Same rms convention as :meth:`window_dbv_spectrum`: each line is
        the mean-square of that frequency component of the DIFFERENCED
        sequence, power-averaged over every valid window (dBV =
        10·log10(ms); a tone of amplitude A reads A·2|sin(πf·Δt)|/√2 in
        its line).  The per-window mean of the difference is removed and
        the DC and Nyquist lines are dropped.  ``progress(done, total)``
        is called per batch for background workers.

        Returns (freqs, ms_mean, n_windows_averaged); raises ValueError
        for m < 1 or fewer than 8 difference samples per window.
        """
        if m < 1:
            raise ValueError("m must be >= 1")
        n_grid = self.n // m            # Δt-spaced grid points v[k·m]
        n_diff = n_grid - 1             # consecutive differences on that grid
        if n_diff < 8:
            raise ValueError(f"m = {m} yields only {n_diff} difference samples "
                             f"per window of {self.n} (need at least 8)")
        if self.n_windows == 0:
            return None, None, 0

        n_ar = np.arange(self.n)
        sum_ms = np.zeros(n_diff // 2 + 1)
        count = 0
        total = self.n_windows
        for ids, offs in self._iter_window_slices(batch):
            idx = offs[:, None] + n_ar[None, :]
            volts = self.to_volts(self.view[idx].astype(np.float64))
            # decimate onto the Δt grid (samples 0, m, 2m, …), then take
            # consecutive differences: d_k = v[(k+1)m] − v[km]
            v_dec = volts[:, : n_grid * m].reshape(
                volts.shape[0], n_grid, m)[:, :, 0]
            d = v_dec[:, 1:] - v_dec[:, :-1]
            d -= d.mean(axis=1, keepdims=True)
            spec = np.abs(np.fft.rfft(d, axis=1))
            sum_ms += (2.0 * (spec / n_diff) ** 2).sum(axis=0)
            count += d.shape[0]
            if progress is not None:
                progress(count, total)

        freqs = np.fft.rfftfreq(n_diff, d=m * self.dt)   # the Δt grid
        ms = sum_ms / max(count, 1)
        if n_diff % 2 == 0:
            ms, freqs = ms[1:-1], freqs[1:-1]  # drop DC and Nyquist
        else:
            ms, freqs = ms[1:], freqs[1:]  # odd n: no Nyquist line
        return freqs, ms, count

    def ramp_spectrum_averaged(self, batch: int = 256, progress=None):
        """Ensemble-averaged plain FFT of the windows (no difference, no
        detrend, no taper — the ramp as acquired).

        Identical normalization to :meth:`lag_difference_spectrum` and the
        oscilloscope's Spectrum tab: per window the mean is removed, the
        rfft lines carry 2·(|X|/n)² (V² rms — a tone of amplitude A reads
        A/√2 in its line), the DC and Nyquist lines are dropped, and the
        mean-squares are power-averaged over every valid window.  This is
        the curve to overlay against the lag-m difference: the ratio of
        the two at any frequency is exactly the 2|sin(πf·Δt)| transfer
        function (visible 1/f suppression).
        """
        n = self.n
        if self.n_windows == 0:
            return None, None, 0
        n_ar = np.arange(n)
        sum_ms = np.zeros(n // 2 + 1)
        count = 0
        total = self.n_windows
        for ids, offs in self._iter_window_slices(batch):
            idx = offs[:, None] + n_ar[None, :]
            volts = self.to_volts(self.view[idx].astype(np.float64))
            volts -= volts.mean(axis=1, keepdims=True)
            spec = np.abs(np.fft.rfft(volts, axis=1))
            sum_ms += (2.0 * (spec / n) ** 2).sum(axis=0)
            count += volts.shape[0]
            if progress is not None:
                progress(count, total)

        freqs = np.fft.rfftfreq(n, d=self.dt)
        ms = sum_ms / max(count, 1)
        if n % 2 == 0:
            ms, freqs = ms[1:-1], freqs[1:-1]  # drop DC and Nyquist
        else:
            ms, freqs = ms[1:], freqs[1:]  # odd n: no Nyquist line
        return freqs, ms, count


def _detrend(x: np.ndarray, mode: str) -> np.ndarray:
    """Remove the per-row trend of a (windows, n) matrix."""
    if mode == "none":
        return x
    if mode == "mean":
        return x - x.mean(axis=1, keepdims=True)
    if mode == "linear":
        t = np.arange(x.shape[1], dtype=np.float64)
        t -= t.mean()
        slope = (x @ t) / np.dot(t, t)
        return x - x.mean(axis=1, keepdims=True) - slope[:, None] * t[None, :]
    raise ValueError(f"unknown detrend {mode!r}")


# ---------------------------------------------------------------------------
# Derivatives
# ---------------------------------------------------------------------------

def derivative(volts: np.ndarray, dt: float, mode: str = "diff", n: int = 11) -> np.ndarray:
    """Per-window derivative of a (…, n_samples) volts array.

    - ``diff``: (V[k+1] − V[k])/dt, length n_samples − 1;
    - ``linear``: centered least-squares slope over ``n`` points (odd n
      ≥ 3) — the fixed regression kernel idx/Σidx²/dt convolved over the
      window, so it is the exact moving linear fit; edge points without a
      full neighborhood are dropped (length n_samples − n + 1);
    - ``means``: two boxcar means — the mean of the first half of the
      ``n``-point window minus the mean of the second half, divided by
      the distance between the half centers ((n − n//2)·dt; odd n ≥ 3,
      value reported at the window center like ``linear``).  Unbiased on
      a ramp like the fit, but noisier (equal pair weights instead of
      the optimal linear ones).
    """
    if mode == "diff":
        return np.diff(volts, axis=-1) / dt
    if mode == "linear":
        if n < 3 or n % 2 == 0:
            raise ValueError("the moving linear fit needs an odd n >= 3")
        if volts.shape[-1] < n:
            return np.empty(volts.shape[:-1] + (0,))
        idx = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
        kernel = idx / np.dot(idx, idx) / dt
        windows = np.lib.stride_tricks.sliding_window_view(volts, n, axis=-1)
        return windows @ kernel
    if mode == "means":
        if n < 3 or n % 2 == 0:
            raise ValueError("the two-means slope needs an odd n >= 3")
        if volts.shape[-1] < n:
            return np.empty(volts.shape[:-1] + (0,))
        h = n // 2
        windows = np.lib.stride_tricks.sliding_window_view(volts, n, axis=-1)
        left = windows[..., :h].mean(axis=-1)
        right = windows[..., -h:].mean(axis=-1)
        return (right - left) / ((n - h) * dt)
    raise ValueError(f"unknown derivative mode {mode!r}")


def derivative_grid(n_samples: int, dt: float, mode: str = "diff", n: int = 11) -> np.ndarray:
    """The time axis matching :func:`derivative`'s output (the diff sits
    between samples; the moving fit and the two-means slope are reported
    at their window center)."""
    if mode == "diff":
        return (np.arange(n_samples - 1, dtype=np.float64) + 0.5) * dt
    if mode in ("linear", "means"):
        return (np.arange(n_samples - n + 1, dtype=np.float64) + (n - 1) / 2.0) * dt
    raise ValueError(f"unknown derivative mode {mode!r}")


# ---------------------------------------------------------------------------
# Dataset (a ramp folder)
# ---------------------------------------------------------------------------

class RampDataset:
    """A published ramp folder: one :class:`RampChannel` per ``*.bin``."""

    def __init__(self, folder: str):
        self.folder = folder
        if not os.path.isdir(folder):
            raise ValueError(f"{folder}: ramp folder not found")
        index_path = os.path.join(folder, "index.json")
        self.index = None
        if os.path.isfile(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    self.index = json.load(f)
            except (OSError, ValueError):
                self.index = None

        self.channels = {}
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".bin"):
                continue
            path = os.path.join(folder, name)
            try:
                header, _ = open_ramp_file(path)
            except Exception:
                continue
            label = header["channel_label"] or name
            gaps = ((self.index or {}).get("channels") or {}).get(label, {}).get("gaps")
            self.channels[label] = RampChannel(path, gaps)

        if not self.channels:
            raise ValueError(f"{folder}: no readable AQR1 ramp files")

    @property
    def uuid(self) -> str:
        return (self.index or {}).get("measurement_uuid") or ""
