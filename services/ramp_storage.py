"""
Ramp-sample streaming storage (file format AQR1, version 1).

The ramp path is deliberately separate from the classic DataModel pipeline:
the acquisition driver (or the simulator) pushes *blocks* of normalized
uint32 ADC samples per channel into a `RampStorageService`; one dedicated
writer thread

  * appends the raw samples to one binary file per channel (fixed 4096-byte
    header, then little-endian uint32; written with plain sequential writes —
    benchmarked at ~1.5 GiB/s against the 8 MiB/s spec),
  * keeps a rolling sha256 of the data section (provenance for free at save
    time — no re-read of a 100 GB file),
  * refreshes `samples_written` in each header about once per second, so a
    crashed file is valid exactly up to the last recorded count,
  * chunks the stream into integration windows (N = T_int/dt + 1 samples,
    an exact integer because the firmware forces T_int to be a multiple of
    dt) and feeds the first/last sample of every window back into the
    classic analysis pipeline — the *only* data the variance-covariance
    estimators ever see.  In markers mode (N = 2) this stream IS the classic
    start/stop stream.

Reading (future monitoring UI): `open_ramp_file()` parses the header and
returns an np.memmap view onto the samples.

The driver-facing surface (`write_block` / `report_gap`) is documented as a
protocol next to the acquisition interface
(hardware/interfaces/ramp_sink.py) and may be called from the driver's
reader thread.
"""
import hashlib
import json
import os
import queue
import shutil
import struct
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

from core.constants import overflow_sentinel

SAMPLE_DTYPE = np.dtype("<u4")
BYTES_PER_SAMPLE = 4

MAGIC = b"AQR1"
HEADER_VERSION = 1
HEADER_SIZE = 4096

# Verified byte-exact layout (offsets asserted in tests/test_ramp_storage.py).
#   0x000 magic, header_version, header_size, flags, sample_dtype,
#   0x010 channel_index, state, timing_mode, adc_bits,
#   0x018 samples_per_window, samples_written, data_offset,
#   0x030 ramp_dt_s, period_s, integration_time_s, reset_time_s,
#        sample_rate_hz, range_min_v, range_max_v, start_epoch_s,
#   0x070 data_sha256, config_sha256, measurement_uuid,
#   0x0D0 start_iso, channel_label, driver_code, firmware_version,
#        app_version, reserved
_HEADER_FORMAT = (
    "<4sHHIIBBHIQQQdddddddd32s32s32s64s32s32s32s16s64s"
)
_HEADER_STRUCT = struct.Struct(_HEADER_FORMAT)
assert _HEADER_STRUCT.size <= HEADER_SIZE

STATE_RUNNING = 0
STATE_FINALIZED = 1
STATE_ABORTED = 2

MODE_CODES = {"internal": 0, "ramp": 1, "markers": 2}
MODE_NAMES = {code: name for name, code in MODE_CODES.items()}  # header -> meta

DTYPES = {"<u4": 0, "uint32": 0}  # dtype.str is endian-explicit; str() may not be

REFRESH_INTERVAL_S = 1.0
# Bounded queue depth, sized for stall tolerance rather than memory (RAM is
# explicitly not a constraint): 16384 blocks x 32 KiB = 512 MiB of slack
# ≈ 64 s of buffer at the 8 MiB/s spec (2 ch x 1 MSPS x 4 B).  The writer
# thread is Python and shares the GIL with the UI, so a GUI freeze or a slow
# disk hiccup must ride out here; the drop timeout (ramp mode) only arms
# once the queue is actually full.
QUEUE_DEPTH = 16384

_MISSING = object()


def _pad(value, size: int) -> bytes:
    if value is None:
        value = ""
    if isinstance(value, str):
        value = value.encode("utf-8")
    return value[:size].ljust(size, b"\x00")


@dataclass
class RampFileMeta:
    """Everything the fixed header knows at file-creation time."""

    channel_index: int          # 0 = ch1, 1 = ch2
    channel_label: str          # "ch1" / "ch2"
    adc_bits: int               # native ADC resolution
    range_min_v: float
    range_max_v: float
    ramp_dt_s: float
    period_s: float
    integration_time_s: float
    reset_time_s: float
    samples_per_window: int
    timing_mode: str            # "ramp" / "markers" / "internal"
    driver_code: str = "simulation"
    firmware_version: str = ""
    app_version: str = ""
    config_sha256: str = ""     # hex
    measurement_uuid: str = field(default_factory=lambda: uuidlib.uuid4().hex)
    start_iso: str = ""
    start_epoch_s: float = 0.0

    def header_bytes(
        self, samples_written: int, state: int, data_sha256: bytes
    ) -> bytes:
        values = (
            MAGIC,
            HEADER_VERSION,
            HEADER_SIZE,
            0,                              # flags
            DTYPES[SAMPLE_DTYPE.str],
            self.channel_index,
            state,
            MODE_CODES.get(self.timing_mode, 0),
            self.adc_bits,
            self.samples_per_window,
            samples_written,
            HEADER_SIZE,                    # data_offset
            self.ramp_dt_s,
            self.period_s,
            self.integration_time_s,
            self.reset_time_s,
            (1.0 / self.ramp_dt_s) if self.ramp_dt_s > 0 else 0.0,
            self.range_min_v,
            self.range_max_v,
            self.start_epoch_s,
            data_sha256,
            bytes.fromhex(self.config_sha256) if self.config_sha256 else b"\x00" * 32,
            _pad(self.measurement_uuid, 32),  # uuid4().hex: 32 ASCII chars
            _pad(self.start_iso, 64),
            _pad(self.channel_label, 32),
            _pad(self.driver_code, 32),
            _pad(self.firmware_version, 32),
            _pad(self.app_version, 16),
            b"\x00" * 64,
        )
        packed = _HEADER_STRUCT.pack(*values)
        return packed + b"\x00" * (HEADER_SIZE - len(packed))


class RampHeader(dict):
    """Parsed header (dict subclass for convenient field access)."""

    @property
    def valid(self) -> bool:
        return self.get("magic") == MAGIC and self.get("header_version") == HEADER_VERSION


def parse_header(data: bytes) -> RampHeader:
    values = _HEADER_STRUCT.unpack_from(data, 0)
    return RampHeader(
        magic=values[0],
        header_version=values[1],
        header_size=values[2],
        flags=values[3],
        sample_dtype=values[4],
        channel_index=values[5],
        state=values[6],
        timing_mode=values[7],
        adc_bits=values[8],
        samples_per_window=values[9],
        samples_written=values[10],
        data_offset=values[11],
        ramp_dt_s=values[12],
        period_s=values[13],
        integration_time_s=values[14],
        reset_time_s=values[15],
        sample_rate_hz=values[16],
        range_min_v=values[17],
        range_max_v=values[18],
        start_epoch_s=values[19],
        data_sha256=values[20].hex(),
        config_sha256=values[21].hex(),
        measurement_uuid=values[22].rstrip(b"\x00").decode("ascii", "replace"),
        start_iso=values[23].rstrip(b"\x00").decode("utf-8", "replace"),
        channel_label=values[24].rstrip(b"\x00").decode("utf-8", "replace"),
        driver_code=values[25].rstrip(b"\x00").decode("utf-8", "replace"),
        firmware_version=values[26].rstrip(b"\x00").decode("utf-8", "replace"),
        app_version=values[27].rstrip(b"\x00").decode("utf-8", "replace"),
    )


def open_ramp_file(path: str):
    """Read-side accessor for the (future) monitoring UI: returns
    (RampHeader, np.memmap of uint32 samples).  Only `samples_written`
    samples are valid, whatever the file size says."""
    with open(path, "rb") as f:
        header = parse_header(f.read(HEADER_SIZE))
    if not header.valid:
        raise ValueError(f"{path}: not an AQR1 v{HEADER_VERSION} ramp file")
    view = np.memmap(path, dtype=SAMPLE_DTYPE, mode="r", offset=header["data_offset"])
    return header, view[: header["samples_written"]]


class RampChannelWriter:
    """One channel's binary file: append-only data + header bookkeeping."""

    def __init__(self, path: str, meta: RampFileMeta):
        self.path = path
        self.meta = meta
        self._samples = 0
        self._sha = hashlib.sha256()
        self._file = open(path, "wb", buffering=0)
        self._file.write(meta.header_bytes(0, STATE_RUNNING, b"\x00" * 32))

    @property
    def samples_written(self) -> int:
        return self._samples

    @property
    def data_sha256_hex(self) -> str:
        return self._sha.hexdigest()

    def write(self, arr: np.ndarray) -> None:
        self._file.write(memoryview(arr).cast("B"))
        self._sha.update(arr)
        self._samples += arr.size

    def refresh_header(self) -> None:
        """Update samples_written in place (crash-recovery watermark)."""
        pos = self._file.tell()
        self._file.seek(0)
        self._file.write(
            self.meta.header_bytes(self._samples, STATE_RUNNING, b"\x00" * 32)
        )
        self._file.seek(pos)

    def finalize(self, state: int) -> None:
        self._file.seek(0)
        self._file.write(
            self.meta.header_bytes(
                self._samples,
                state,
                bytes.fromhex(self._sha.hexdigest()) if state == STATE_FINALIZED else b"\x00" * 32,
            )
        )
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()


class RampWindowExtractor:
    """Chunks a per-channel sample stream into integration windows and emits
    paired debug tuples ((ch1_start, ch1_stop), (ch2_start, ch2_stop)) — the
    exact shape the classic pipeline receives from the µC drivers.

    Window w covers absolute sample indices [w*N, (w+1)*N).  A window is
    complete when all N samples were seen; any window touched by a gap is
    abandoned.  A window pair is emitted only when *every* channel completed
    that window (a window lost on one channel drops the pair — recorded,
    never silently misaligned).

    With `pad_sentinel` (the overflow/missing-channel marker of the active
    ADC), the emitted tuples always carry BOTH channel slots — a
    single-detector setup fills the absent channel with the sentinel,
    exactly like the µC drivers do — so `convert_data_debug` sees the same
    shape regardless of the channel count.
    """

    def __init__(self, samples_per_window: int, channels: List[int],
                 on_pairs: Callable[[list], None],
                 pad_sentinel: Optional[int] = None):
        self.n = samples_per_window
        self.channels = list(channels)
        self.on_pairs = on_pairs
        self.pad_sentinel = pad_sentinel
        self._completed = {ch: {} for ch in self.channels}  # w -> (first, last)
        self._partial = {ch: {} for ch in self.channels}    # w -> [first, last, count]
        self._cursor = 0
        self.windows_emitted = 0
        self.window_pairs_dropped = 0

    def process_block(self, ch: int, arr: np.ndarray, abs_index: int) -> None:
        n = self.n
        w0 = abs_index // n
        w1 = (abs_index + arr.size - 1) // n
        for w in range(w0, w1 + 1):
            lo = max(0, w * n - abs_index)
            hi = min(arr.size, (w + 1) * n - abs_index)
            if hi <= lo:
                continue
            entry = self._partial[ch].get(w)
            if entry is None:
                entry = [int(arr[lo]), int(arr[hi - 1]), 0]
                self._partial[ch][w] = entry
            else:
                entry[1] = int(arr[hi - 1])
            entry[2] += hi - lo
            if entry[2] == n:
                self._completed[ch][w] = (entry[0], entry[1])
                del self._partial[ch][w]
        self._drain()

    def note_gap(self, ch: int, abs_index: int, missing: int) -> None:
        n = self.n
        w0 = abs_index // n
        w1 = (abs_index + missing - 1) // n
        for w in range(w0, w1 + 1):
            self._partial[ch].pop(w, None)
            self._completed[ch].pop(w, None)  # pair accounting happens in _drain
        self._drain()

    def flush(self) -> None:
        """End of stream: abandon everything not yet complete and pair out
        whatever remains (windows completed after their partner's gap)."""
        for ch in self.channels:
            self._partial[ch].clear()
        self._drain()
        # remaining completed windows can never pair -> drop
        for ch in self.channels:
            self.window_pairs_dropped += len(self._completed[ch])
            self._completed[ch].clear()

    def _emit_pair(self, pair_by_channel) -> None:
        if self.pad_sentinel is None:
            self.on_pairs([tuple(pair_by_channel[ch] for ch in self.channels)])
            return
        fill = (self.pad_sentinel, self.pad_sentinel)
        self.on_pairs([tuple(pair_by_channel.get(ch, fill) for ch in (0, 1))])

    def _drain(self) -> None:
        while True:
            states = {ch: self._completed[ch].get(self._cursor, _MISSING)
                      for ch in self.channels}
            if _MISSING not in states.values():
                # pop, don't peek: entries left behind would look like
                # "later data" to the drop rule below and spin it forever
                pair_by_channel = {
                    ch: self._completed[ch].pop(self._cursor)
                    for ch in self.channels
                }
                self._emit_pair(pair_by_channel)
                self.windows_emitted += 1
                self._cursor += 1
                continue
            # Some channel lacks the cursor window.  If that channel already
            # has strictly later windows the cursor window is lost on it ->
            # drop the pair.  If it simply has nothing more, wait for data.
            lost = any(
                st is _MISSING and self._completed[ch]
                for ch, st in states.items()
            )
            if not lost:
                return
            for ch in self.channels:
                self._completed[ch].pop(self._cursor, None)
            self.window_pairs_dropped += 1
            self._cursor += 1


class _BlockSinkBase:
    """Producer side shared by every ramp sink (called from the acquisition
    driver's reader thread): bounded queue, per-channel contiguity/gap
    bookkeeping against absolute sample indices, and the overflow policy.

    Policy "block" (markers mode — the stream IS the measurement) applies
    backpressure to the driver; "drop" (ramp monitoring) drops whole blocks
    after the fill timeout and records them as gaps — the next block's
    abs_index re-anchors the alignment automatically."""

    overflow_policy: str = "block"

    def _sink_init(self, channels: List[int]) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self._expected = {ch: 0 for ch in channels}
        self._gaps = {ch: [] for ch in channels}
        self._dropped_blocks = {ch: 0 for ch in channels}
        self._closed = False

    def write_block(self, channel: int, samples, abs_index: Optional[int] = None) -> None:
        """Append one block of uint32 samples of `channel`.  `abs_index` is
        the absolute sample index of the block's first sample (None =
        continues contiguously after the previous block); a jump enqueues a
        gap so window alignment never silently shifts."""
        if self._closed:
            raise RuntimeError("ramp sink is closed")
        arr = np.ascontiguousarray(samples, dtype=SAMPLE_DTYPE)
        if arr.ndim != 1:
            raise ValueError("ramp samples must be a 1-D block")
        expected = self._expected[channel]
        if abs_index is None:
            abs_index = expected
        if abs_index > expected:
            self._enqueue_gap(channel, expected, abs_index - expected)
        self._expected[channel] = abs_index + arr.size
        item = ("block", channel, arr, abs_index)
        try:
            if self.overflow_policy == "block":
                self._queue.put(item)  # backpressure: never lose analysis data
            else:
                self._queue.put(item, timeout=5.0)
        except queue.Full:
            self._dropped_blocks[channel] += 1
            self._enqueue_gap(channel, abs_index, arr.size)
            self._expected[channel] = abs_index  # next block reports its own index

    def report_gap(self, channel: int, abs_index: int, missing: int) -> None:
        """The driver detected a hole in `channel`'s stream (e.g. a transport
        resync): `missing` samples are lost starting at `abs_index`."""
        if self._closed:
            return
        self._enqueue_gap(channel, abs_index, missing)
        if abs_index + missing > self._expected[channel]:
            self._expected[channel] = abs_index + missing

    def _enqueue_gap(self, channel: int, abs_index: int, missing: int) -> None:
        try:
            self._queue.put(("gap", channel, abs_index, missing))
        except queue.Full:
            # gap bookkeeping must never block the driver; coalesce roughly
            self._gaps[channel].append([abs_index, missing])


class RampStorageService(_BlockSinkBase):
    """Ramp-mode sink: bounded queue + one writer thread owning the
    per-channel files, the window extractor and the index sidecar."""

    def __init__(
        self,
        channel_paths: List[str],
        metas: List[RampFileMeta],
        index_path: str,
        samples_per_window: int,
        on_window_pairs: Callable[[list], None],
        overflow_policy: str = "drop",   # monitoring data: drop with gap record
    ):
        assert len(channel_paths) == len(metas)
        self.channel_paths = list(channel_paths)
        self.metas = list(metas)
        self.index_path = index_path
        self.samples_per_window = samples_per_window
        self.overflow_policy = overflow_policy
        self.channels = [m.channel_index for m in metas]
        self._labels = [m.channel_label for m in metas]

        self._sink_init(self.channels)
        self._extractor = RampWindowExtractor(
            samples_per_window, self.channels, on_window_pairs,
            # single-detector setups: pad the absent channel with the
            # overflow sentinel, exactly like the µC drivers do
            pad_sentinel=overflow_sentinel(metas[0].adc_bits),
        )
        self._writers = [
            RampChannelWriter(path, meta)
            for path, meta in zip(channel_paths, metas)
        ]
        self._error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ramp-storage-writer")
        self._thread.start()

    # ------------------------------------------------------------- lifecycle

    def close(self, state: int = STATE_FINALIZED) -> dict:
        """Stop the writer thread after draining the queue, finalize the
        files and write the index sidecar.  Returns the summary dict (also
        embedded in the sidecar)."""
        if self._closed:
            return self.summary(state)
        self._closed = True
        # Bounded sentinel put: if the writer thread already died (its
        # catch-all handler exits without draining), an unbounded put on a
        # full queue would block forever — the join timeout below would
        # never even be reached and the UI thread would hang.
        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            self._error = self._error or "writer thread did not drain the queue"
        self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            self._error = "writer thread did not finish within 30 s"
        for writer in self._writers:
            if not writer._file.closed:
                writer.finalize(state)
        summary = self.summary(state)
        self._write_index(summary)
        return summary

    def abort(self) -> dict:
        return self.close(STATE_ABORTED)

    def summary(self, state: int = STATE_RUNNING) -> dict:
        channels = {}
        for writer, label in zip(self._writers, self._labels):
            channels[label] = {
                "file": os.path.basename(writer.path),
                "samples": writer.samples_written,
                "sha256_data": writer.data_sha256_hex,
                "dropped_blocks": self._dropped_blocks[writer.meta.channel_index],
                "gaps": list(self._gaps[writer.meta.channel_index]),
                "windows_completed": self._extractor.windows_emitted,
                "window_pairs_dropped": self._extractor.window_pairs_dropped,
            }
        return {
            "format": "atomiqa-ramp-index/1",
            "measurement_uuid": self.metas[0].measurement_uuid,
            "timing_mode": self.metas[0].timing_mode,
            "ramp_dt_s": self.metas[0].ramp_dt_s,
            "period_s": self.metas[0].period_s,
            "samples_per_window": self.samples_per_window,
            "state": state,
            "error": self._error,
            "channels": channels,
        }

    def _write_index(self, summary: dict) -> None:
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
        except OSError:
            pass  # sidecar is diagnostics; the bin headers carry the truth

    # ------------------------------------------------------------- writer

    def _run(self) -> None:
        writers = {w.meta.channel_index: w for w in self._writers}
        last_refresh = time.monotonic()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    item = ("tick",)
                if item is None:
                    break
                kind = item[0]
                if kind == "block":
                    _, ch, arr, i0 = item
                    writers[ch].write(arr)
                    self._extractor.process_block(ch, arr, i0)
                elif kind == "gap":
                    _, ch, i0, missing = item
                    self._gaps[ch].append([int(i0), int(missing)])
                    self._extractor.note_gap(ch, i0, missing)
                if time.monotonic() - last_refresh > REFRESH_INTERVAL_S:
                    for w in writers.values():
                        w.refresh_header()
                    last_refresh = time.monotonic()
        except Exception as exc:  # pragma: no cover - defensive
            self._error = f"ramp writer failed: {exc}"
        self._extractor.flush()


class RampPairExtractorService(_BlockSinkBase):
    """Markers-mode sink: the same block -> window extraction feeding the
    classic start/stop pipeline, WITHOUT any disk storage.

    In markers mode (no ramp sampling requested on split hardware) the
    timing device still emits the two integration-boundary CONV_START edges
    per window (dt = T_int), and those two samples ARE the measurement:
    they reach data.csv through the classic pipeline exactly like the µC's
    own start/stop stream.  Binary ramp files would only duplicate data.csv,
    so none are created, no temp files appear, and no "ramp" section is
    written to settings.json.  The overflow policy is backpressure ("block"):
    a lost window here is a lost measurement row."""

    def __init__(
        self,
        samples_per_window: int,
        channels: List[int],
        on_window_pairs: Callable[[list], None],
        overflow_policy: str = "block",
        adc_bits: Optional[int] = None,
    ):
        self.samples_per_window = samples_per_window
        self.channels = list(channels)
        self.overflow_policy = overflow_policy

        self._sink_init(self.channels)
        self._extractor = RampWindowExtractor(
            samples_per_window, self.channels, on_window_pairs,
            # absent channel sentinel-filled like the drivers (None = raw
            # configured-channel tuples, for extractor-level unit tests)
            pad_sentinel=overflow_sentinel(adc_bits) if adc_bits else None,
        )
        self._error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ramp-pair-extractor")
        self._thread.start()

    def close(self, state: int = STATE_FINALIZED) -> dict:
        """Drain and stop (the `state` argument is accepted for symmetry
        with RampStorageService; there are no files to finalize)."""
        if self._closed:
            return self.summary()
        self._closed = True
        # Bounded like RampStorageService.close: a dead extractor thread
        # must not turn the sentinel put into a permanent UI hang.
        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            self._error = self._error or "pair extractor did not drain the queue"
        self._thread.join(timeout=10.0)
        if self._thread.is_alive():
            self._error = "pair extractor did not finish within 10 s"
        return self.summary()

    def abort(self) -> dict:
        return self.close()

    def summary(self, state: int = STATE_RUNNING) -> dict:
        return {
            "format": "atomiqa-ramp-index/1",
            "timing_mode": "markers",
            "samples_per_window": self.samples_per_window,
            "state": state,
            "error": self._error,
            "channels": {
                ch: {
                    "gaps": list(self._gaps[ch]),
                    "dropped_blocks": self._dropped_blocks[ch],
                }
                for ch in self.channels
            },
            "windows_emitted": self._extractor.windows_emitted,
            "window_pairs_dropped": self._extractor.window_pairs_dropped,
        }

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
                    self._extractor.process_block(ch, arr, i0)
                elif kind == "gap":
                    _, ch, i0, missing = item
                    self._gaps[ch].append([int(i0), int(missing)])
                    self._extractor.note_gap(ch, i0, missing)
        except Exception as exc:  # pragma: no cover - defensive
            self._error = f"pair extractor failed: {exc}"
        self._extractor.flush()


# ---------------------------------------------------------------------------
# Paths, publication (save) and crash recovery
# ---------------------------------------------------------------------------

def ramp_temp_paths(workspace_dir: Optional[str], num_channels: int) -> tuple:
    """Temp file paths for a running measurement: inside the workspace
    (crash recovery, like .active_run.csv.tmp) or in the system temp dir
    when no workspace is active."""
    names = [f".active_run.ramp.ch{i + 1}.bin" for i in range(num_channels)]
    if workspace_dir:
        return (
            [os.path.join(workspace_dir, n) for n in names],
            os.path.join(workspace_dir, ".active_run.ramp.index.json.tmp"),
        )
    import tempfile

    paths = []
    for _ in names:
        fd, p = tempfile.mkstemp(prefix="atomiqa_ramp_", suffix=".bin")
        os.close(fd)
        paths.append(p)
    index = os.path.join(os.path.dirname(paths[0]), "atomiqa_ramp_index.json")
    return paths, index


def unique_ramp_folder(ramp_root: str, measurement_name: str) -> str:
    """`<ramp_root>/<name>` with a numeric suffix when the folder exists."""
    candidate = os.path.join(ramp_root, measurement_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(ramp_root, f"{measurement_name}_{counter}")
        counter += 1
    return candidate


def publish_ramp_files(
    summary: dict,
    ch_paths: List[str],
    index_path: str,
    ramp_root: str,
    measurement_name: str,
) -> tuple:
    """Move the temp files of a finished run into
    `<ramp_root>/<measurement_name>/` (existence-checked, suffixed on
    collision) and return (dest_dir, settings.json "ramp" section)."""
    dest = unique_ramp_folder(ramp_root, measurement_name)
    os.makedirs(dest, exist_ok=True)
    for path in ch_paths:
        if os.path.exists(path):
            shutil.move(path, os.path.join(dest, os.path.basename(path)))
    if os.path.exists(index_path):
        shutil.move(index_path, os.path.join(dest, "index.json"))

    # reference relative to the workspace root (ramp_root is <workspace>/ramp)
    rel = os.path.relpath(dest, os.path.dirname(os.path.normpath(ramp_root)))
    ramp_meta = {
        "folder": rel,
        "uuid": summary.get("measurement_uuid"),
        "timing_mode": summary.get("timing_mode"),
        "ramp_dt_s": summary.get("ramp_dt_s"),
        "samples_per_window": summary.get("samples_per_window"),
        "channels": {
            label: {
                "file": os.path.basename(path),
                "samples": summary["channels"][label]["samples"],
                "sha256_data": summary["channels"][label]["sha256_data"],
            }
            for label, path in zip(summary["channels"].keys(), ch_paths)
        },
        "index": "index.json",
    }
    return dest, ramp_meta


def recover_ramp_file(path: str) -> Optional[dict]:
    """Crash recovery for one temp ramp file: trust the header watermark
    (`samples_written`, refreshed ~1 s during the run), trim the file to it
    and mark it aborted.  Windows partially written are dropped; the value
    is rounded down to whole windows.  Returns the parsed header."""
    try:
        with open(path, "r+b", buffering=0) as f:
            header = parse_header(f.read(HEADER_SIZE))
            if not header.valid:
                return None
            n = header["samples_per_window"]
            if n > 0:
                samples = (header["samples_written"] // n) * n
            else:
                samples = header["samples_written"]
            size = HEADER_SIZE + samples * BYTES_PER_SAMPLE
            f.seek(size)
            f.truncate()
            # rewrite state/samples_written via a fresh header block
            # (parse_header yields the timing-mode CODE; the meta wants
            # the name — writing the code back would relabel the file
            # "internal" whatever mode it was recorded in)
            meta = RampFileMeta(
                channel_index=header["channel_index"],
                channel_label=header["channel_label"],
                adc_bits=header["adc_bits"],
                range_min_v=header["range_min_v"],
                range_max_v=header["range_max_v"],
                ramp_dt_s=header["ramp_dt_s"],
                period_s=header["period_s"],
                integration_time_s=header["integration_time_s"],
                reset_time_s=header["reset_time_s"],
                samples_per_window=n,
                timing_mode=MODE_NAMES.get(header["timing_mode"], "internal"),
                driver_code=header["driver_code"],
                firmware_version=header["firmware_version"],
                app_version=header["app_version"],
                config_sha256=header["config_sha256"],
                measurement_uuid=header["measurement_uuid"],
                start_iso=header["start_iso"],
                start_epoch_s=header["start_epoch_s"],
            )
            f.seek(0)
            f.write(meta.header_bytes(samples, STATE_ABORTED, b"\x00" * 32))
            f.flush()
            os.fsync(f.fileno())
            header["samples_written"] = samples
            header["state"] = STATE_ABORTED
            return header
    except OSError:
        return None
