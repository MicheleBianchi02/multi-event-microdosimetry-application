"""
Live ramp analysis sink: stream-through averaging of full ramp windows.

Third consumer of the acquisition driver's ramp block stream (after
RampStorageService, which records AQR1 files, and RampPairExtractorService,
which keeps only start/stop pairs).  Long spectral campaigns with ramps of
seconds would fill the disk with samples nobody reads back; this sink keeps
in memory only

  * the LATEST complete window per channel (preview), and
  * the accumulated RESULTS: the power-averaged FFT of a user-selected
    segment of every window (optionally through a 2nd-order Butterworth
    high-pass that removes the ramp slope's 1/f² Fourier tail — see
    set_highpass), and the correlated-double-sampling series (single
    start/stop samples per window, their difference).

The math runs on the sink's own thread (the driver's reader thread only
enqueues blocks); the UI polls lock-guarded snapshots and never receives a
sample it did not ask for.  Channels are processed independently — unlike
RampWindowExtractor there is no cross-channel pairing to wait for.

The FFT normalization is the app-wide rms/dBV convention (see
RampChannel.window_dbv_spectrum and ramp_spectrum_averaged in
services/ramp_analysis.py): per window the segment mean is removed,
ms = 2·(|X|/n')², and the DC and Nyquist lines are dropped at read time;
ms is power-averaged over every absorbed window.  A sine of peak A reads
A/√2 in its line, and e_n = 10^(dBV/20)/sqrt(df) recovers the one-sided
noise density.

Changing the selection (FFT segment or CDS points) resets the accumulators:
an average is only meaningful when every window was analyzed the same way.
"""
import logging
import queue
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

from services.ramp_storage import SAMPLE_DTYPE, _BlockSinkBase

logger = logging.getLogger(__name__)


class _Series:
    """Growable float64 column: appended by the sink thread, copied out by
    snapshots.  A python list would cost ~100 MB/hour of object overhead at
    a 1 kHz window rate; this stays one flat array."""

    __slots__ = ("buf", "size")

    def __init__(self, capacity: int = 1024):
        self.buf = np.empty(capacity, dtype=np.float64)
        self.size = 0

    def append(self, value: float) -> None:
        if self.size == self.buf.size:
            grown = np.empty(self.buf.size * 2, dtype=np.float64)
            grown[: self.size] = self.buf[: self.size]
            self.buf = grown
        self.buf[self.size] = value
        self.size += 1

    def values(self) -> np.ndarray:
        return self.buf[: self.size]

    def clear(self) -> None:
        self.size = 0


class _ChannelState:
    """All per-channel results.  Written by the sink thread, read by the
    UI thread under the sink's state lock."""

    __slots__ = ("ms_sum", "freqs", "n_seg", "count", "start_v", "stop_v",
                 "diff_v", "latest_counts", "latest_gen", "windows_seen")

    def __init__(self):
        self.ms_sum: Optional[np.ndarray] = None   # summed ms per full rfft line
        self.freqs: Optional[np.ndarray] = None    # grid of the current segment
        self.n_seg = 0                             # segment length the grid belongs to
        self.count = 0                             # windows absorbed into ms_sum
        self.start_v = _Series()
        self.stop_v = _Series()
        self.diff_v = _Series()
        self.latest_counts: Optional[np.ndarray] = None  # (n,) uint32, preview only
        self.latest_gen = 0                        # bumped on every new window
        self.windows_seen = 0                      # completed windows, any selection


class LiveRampSink(_BlockSinkBase):
    """Ramp sink that keeps results, not samples (see module docstring).

    Overflow policy "drop": like ramp-mode monitoring data, a stalled
    consumer loses whole blocks (recorded as gaps) instead of back-pressuring
    the NI reader into a buffer overrun; a lost window only slows the average.
    """

    overflow_policy = "drop"

    def __init__(
        self,
        samples_per_window: int,
        channels: List[int],
        ramp_dt_s: float,
        adc_bits: int,
        volts_min: float,
        volts_max: float,
    ):
        if samples_per_window < 2:
            raise ValueError("samples_per_window must be >= 2")
        if not channels:
            raise ValueError("at least one channel is required")
        if ramp_dt_s <= 0:
            raise ValueError("ramp_dt_s must be > 0")
        if adc_bits < 1:
            raise ValueError("adc_bits must be >= 1")

        self.n = int(samples_per_window)
        self.channels = list(channels)
        self.dt = float(ramp_dt_s)
        self.adc_bits = int(adc_bits)
        self.volts_min = float(volts_min)
        self.volts_max = float(volts_max)

        self._sink_init(self.channels)

        # Assembly (sink thread only): one partial window per channel.  A
        # block may straddle window boundaries; _partial_w remembers which
        # window the partial fill belongs to so a gap can abandon it.
        self._assembly = {ch: np.zeros(self.n, dtype=SAMPLE_DTYPE)
                          for ch in self.channels}
        self._filled = {ch: 0 for ch in self.channels}
        self._partial_w = {ch: -1 for ch in self.channels}

        # Results + selection (written by either thread, guarded together).
        self._state_lock = threading.Lock()
        self._state: Dict[int, _ChannelState] = {
            ch: _ChannelState() for ch in self.channels
        }
        self._sel_fft: Tuple[int, int] = (0, self.n)
        self._sel_cds: Tuple[int, int] = (0, self.n - 1)
        # optional high-pass on the FFT path (see set_highpass)
        self._hp_enabled = False
        self._hp_fc = 0.0
        self._hp_sos_cache: Dict[float, np.ndarray] = {}   # fc -> sos (fs fixed)

        self._gap_windows = {ch: 0 for ch in self.channels}
        # input counters (sink thread): raw blocks/samples received, before
        # any window logic — the difference between "no data at all" (dead
        # CONV_START) and "data but no complete window" (geometry/alignment
        # mismatch) is the first fork of every empty-screen diagnosis
        self._blocks_in = {ch: 0 for ch in self.channels}
        self._samples_in = {ch: 0 for ch in self.channels}
        self._error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="live-ramp-sink")
        self._thread.start()
        logger.info(
            "live ramp sink started: n=%d samples/window, dt=%.6g s, "
            "channels=%s, %d bits over [%.3g, %.3g] V",
            self.n, self.dt, self.channels, self.adc_bits,
            self.volts_min, self.volts_max,
        )

    # ------------------------------------------------------------- selection

    @property
    def fft_span(self) -> Tuple[int, int]:
        with self._state_lock:
            return self._sel_fft

    @property
    def cds_points(self) -> Tuple[int, int]:
        with self._state_lock:
            return self._sel_cds

    def set_selection(
        self,
        fft_span: Optional[Tuple[int, int]] = None,
        cds_points: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Set the FFT segment [i0, i1) and/or the CDS sample points
        (i_start, i_stop), as sample indices.  If anything actually changes,
        ALL accumulators are reset: every window in an average must have
        been analyzed under the same selection.  Returns True if a change
        (and therefore a reset) happened."""
        if fft_span is not None:
            i0, i1 = int(fft_span[0]), int(fft_span[1])
            if not (0 <= i0 < i1 <= self.n):
                raise ValueError(f"invalid FFT segment [{i0}, {i1}) "
                                 f"for a {self.n}-sample window")
            fft_span = (i0, i1)
        if cds_points is not None:
            a, b = int(cds_points[0]), int(cds_points[1])
            if not (0 <= a < self.n and 0 <= b < self.n):
                raise ValueError(f"invalid CDS points ({a}, {b}) "
                                 f"for a {self.n}-sample window")
            cds_points = (a, b)

        with self._state_lock:
            changed = (fft_span is not None and fft_span != self._sel_fft) or \
                      (cds_points is not None and cds_points != self._sel_cds)
            if changed:
                if fft_span is not None:
                    self._sel_fft = fft_span
                if cds_points is not None:
                    self._sel_cds = cds_points
                self._reset_locked()
        return changed

    @property
    def highpass(self) -> Tuple[bool, float]:
        """(enabled, fc_hz) of the FFT-path high-pass."""
        with self._state_lock:
            return self._hp_enabled, self._hp_fc

    def set_highpass(self, enabled: bool, fc_hz: float = 0.0) -> bool:
        """Enable/disable the 2nd-order Butterworth high-pass applied to the
        FFT segment before the transform (the CDS path is never filtered —
        it measures the real start/stop values).

        Why: the deterministic ramp slope buries the parallel-current noise
        under its 1/f² Fourier tail at EVERY frequency (both are 1/f through
        the integrator; the ratio is fixed by I_dc/i_n), so the plain ramp
        FFT can never show it.  A 2nd-order high-pass removes the linear
        trend (1st order would not) and the noise's 1/f region becomes
        visible between fc and the white voltage floor.

        Changing anything resets the accumulators, exactly like a selection
        change: an average is only meaningful under one filter state.
        Returns True if a change (and therefore a reset) happened."""
        fc = float(fc_hz or 0.0)
        if enabled and not (0.0 < fc < 0.5 / self.dt):
            raise ValueError(
                f"invalid high-pass cutoff {fc:g} Hz for fs = {1.0 / self.dt:g} Hz")
        with self._state_lock:
            changed = (bool(enabled) != self._hp_enabled) or \
                      (enabled and fc != self._hp_fc)
            if changed:
                self._hp_enabled = bool(enabled)
                if enabled:
                    self._hp_fc = fc
                self._reset_locked()
        return changed

    def _highpass_segment(self, seg: np.ndarray) -> np.ndarray:
        """Filter the segment with the cached 2nd-order high-pass, using
        reflection padding of ~one period of fc on both sides so the
        one-pass transient has decayed (e^{-2pi} ~ -54 dB) inside the
        retained region.  The ~50 ms pre-roll the timing leaves before the
        segment would settle the filter for fc >~ 20 Hz by itself; the
        padding removes that limit."""
        from scipy.signal import butter, sosfilt

        fc = self._hp_fc
        sos = self._hp_sos_cache.get(fc)
        if sos is None:
            sos = butter(2, fc, btype="highpass", fs=1.0 / self.dt, output="sos")
            self._hp_sos_cache[fc] = sos
        pad = min(seg.size - 1, int((1.0 / self.dt) / fc))
        if pad < 1:
            return sosfilt(sos, seg)
        padded = np.concatenate((seg[pad:0:-1], seg, seg[-2:-pad - 2:-1]))
        return sosfilt(sos, padded)[pad:pad + seg.size]

    def reset(self) -> None:
        """Discard all accumulated results (the latest-window preview stays).
        The Reset button; also the consequence of any selection change."""
        with self._state_lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        for st in self._state.values():
            st.ms_sum = None
            st.freqs = None
            st.n_seg = 0
            st.count = 0
            st.windows_seen = 0
            st.start_v.clear()
            st.stop_v.clear()
            st.diff_v.clear()
            # latest_counts/latest_gen stay: the preview survives a reset

    # -------------------------------------------------------------- snapshots

    def snapshot(self, ch: int) -> dict:
        """Thread-safe copy of channel `ch`'s results.  `ms` is the averaged
        mean-square spectrum with the DC and (for even n') Nyquist lines
        dropped, matching ramp_spectrum_averaged."""
        with self._state_lock:
            st = self._state[ch]
            freqs = ms = None
            if st.ms_sum is not None and st.count > 0:
                ms = st.ms_sum / st.count
                freqs = st.freqs
                if st.n_seg % 2 == 0:
                    ms, freqs = ms[1:-1], freqs[1:-1]  # drop DC and Nyquist
                else:
                    ms, freqs = ms[1:], freqs[1:]      # odd n': no Nyquist line
            snap = {
                "freqs": None if freqs is None else freqs.copy(),
                "ms": None if ms is None else ms.copy(),
                "count": st.count,
                "start_v": st.start_v.values().copy(),
                "stop_v": st.stop_v.values().copy(),
                "diff_v": st.diff_v.values().copy(),
                "latest_gen": st.latest_gen,
                "windows_seen": st.windows_seen,
                "blocks_in": self._blocks_in[ch],
                "samples_in": self._samples_in[ch],
                "gap_windows": self._gap_windows[ch],
            }
        return snap

    def filled(self, ch: int) -> int:
        """Samples assembled into the in-progress window of `ch` so far
        (diagnostics: samples arriving but windows never completing points
        at a geometry/alignment mismatch)."""
        return self._filled.get(ch, 0)

    def latest_volts(self, ch: int) -> Tuple[int, Optional[np.ndarray]]:
        """Convert the latest complete window to volts.  The UI calls this
        only when the generation counter moved, so the (potentially large)
        conversion happens once per window, never per redraw."""
        with self._state_lock:
            st = self._state[ch]
            gen = st.latest_gen
            counts = st.latest_counts
        if counts is None:
            return gen, None
        return gen, self.to_volts(counts)

    def to_volts(self, counts: np.ndarray) -> np.ndarray:
        """Counts to volts, the RampChannel.to_volts formula:
        V = V_min + counts/(2^bits − 1)·span."""
        full = float(2 ** self.adc_bits - 1)
        span = self.volts_max - self.volts_min
        return self.volts_min + np.asarray(counts, dtype=np.float64) / full * span

    # -------------------------------------------------------------- lifecycle

    def close(self) -> dict:
        """Drain the queue, stop the sink thread.  Results stay readable."""
        if self._closed:
            return self.summary()
        self._closed = True
        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            self._error = self._error or "live ramp sink did not drain the queue"
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            self._error = "live ramp sink did not finish within 10 s"
        summary = self.summary()
        logger.info("live ramp sink closed: %s", summary)
        return summary

    def summary(self) -> dict:
        with self._state_lock:
            channels = {
                ch: {
                    "count": self._state[ch].count,
                    "windows_seen": self._state[ch].windows_seen,
                    "latest_gen": self._state[ch].latest_gen,
                }
                for ch in self.channels
            }
        return {
            "format": "atomiqa-live-ramp/1",
            "samples_per_window": self.n,
            "channels": channels,
            "gap_windows": dict(self._gap_windows),
            "blocks_in": dict(self._blocks_in),
            "samples_in": dict(self._samples_in),
            "dropped_blocks": {ch: self._dropped_blocks[ch] for ch in self.channels},
            "error": self._error,
        }

    # ------------------------------------------------------------ sink thread

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                kind = item[0]
                if kind == "block":
                    _, ch, arr, i0 = item
                    self._blocks_in[ch] += 1
                    self._samples_in[ch] += arr.size
                    self._process_block(ch, arr, i0)
                elif kind == "gap":
                    _, ch, i0, missing = item
                    self._gap_windows[ch] += 1
                    logger.warning(
                        "live ramp sink: gap on ch%d at sample %d (%d missing)",
                        ch, i0, missing,
                    )
                    self._abandon_partial(ch, i0, missing)
                elif kind == "restart":
                    # the driver re-anchored its counters (a new run on a
                    # reused sink): the partially assembled window belongs
                    # to the PREVIOUS run — keeping it makes the fill
                    # counter skip the exact N boundary and wedge assembly
                    _, ch = item
                    if self._filled[ch]:
                        logger.info(
                            "live ramp sink: stream restarted on ch%d — "
                            "dropping the stale partial window (%d samples)",
                            ch, self._filled[ch],
                        )
                    self._filled[ch] = 0
                    self._partial_w[ch] = -1
        except Exception as exc:  # pragma: no cover - defensive
            self._error = f"live ramp sink failed: {exc}"
            logger.exception("live ramp sink thread DIED: %s", exc)

    def _process_block(self, ch: int, arr: np.ndarray, abs_index: int) -> None:
        """Add one contiguous block to the assembly; a block may straddle
        window boundaries, exactly like RampWindowExtractor.process_block."""
        n = self.n
        buf = self._assembly[ch]
        filled = self._filled[ch]
        w0 = abs_index // n
        w1 = (abs_index + arr.size - 1) // n
        for w in range(w0, w1 + 1):
            lo = max(0, w * n - abs_index)
            hi = min(arr.size, (w + 1) * n - abs_index)
            if hi <= lo:
                continue
            off = (abs_index + lo) - w * n
            buf[off:off + (hi - lo)] = arr[lo:hi]
            if filled == 0:
                self._partial_w[ch] = w
            filled += hi - lo
            if filled == n:
                filled = 0
                self._absorb(ch, buf)
        self._filled[ch] = filled

    def _abandon_partial(self, ch: int, abs_index: int, missing: int) -> None:
        """A hole in the stream invalidates any partially assembled window
        it straddles: the completed-window contract is an untouched window."""
        if self._filled[ch] == 0:
            return
        n = self.n
        w0 = abs_index // n
        w1 = (abs_index + missing - 1) // n
        if w0 <= self._partial_w[ch] <= w1:
            self._filled[ch] = 0

    def _absorb(self, ch: int, counts_buf: np.ndarray) -> None:
        """One complete window: analyze, store the results, discard the rest.
        `counts_buf` is the reusable assembly buffer — everything kept beyond
        this call must be copied."""
        with self._state_lock:
            fft_span = self._sel_fft
            cds_points = self._sel_cds
            hp_enabled = self._hp_enabled

        volts = self.to_volts(counts_buf)

        # CDS: two single samples, exactly like the classic start/stop pair
        i_start, i_stop = cds_points
        start_v = float(volts[i_start])
        stop_v = float(volts[i_stop])

        # FFT of the selected segment, app-wide rms/dBV convention;
        # optionally high-passed first (the ramp slope's 1/f² tail removed,
        # the parallel-current noise's 1/f region exposed)
        i0, i1 = fft_span
        seg = volts[i0:i1]
        ms = None
        if seg.size >= 2:
            if hp_enabled:
                seg = self._highpass_segment(seg)
            spec = np.abs(np.fft.rfft(seg - seg.mean()))
            ms = 2.0 * (spec / seg.size) ** 2

        # copy while still on the sink thread; only reference swaps happen
        # under the lock
        latest = counts_buf.copy()

        with self._state_lock:
            st = self._state[ch]
            st.latest_counts = latest
            st.latest_gen += 1
            st.windows_seen += 1
            if ms is not None:
                if st.ms_sum is None or st.ms_sum.shape != ms.shape:
                    # a fresh grid always follows a reset; defensive anyway
                    st.ms_sum = np.zeros_like(ms)
                    st.freqs = np.fft.rfftfreq(seg.size, d=self.dt)
                    st.n_seg = int(seg.size)
                    st.count = 0
                st.ms_sum += ms
                st.count += 1
            st.start_v.append(start_v)
            st.stop_v.append(stop_v)
            st.diff_v.append(stop_v - start_v)
