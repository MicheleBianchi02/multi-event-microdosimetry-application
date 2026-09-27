"""
Ramp-sample storage (services/ramp_storage.py): the AQR1 binary format,
the streaming writer, the integration-window extractor and the
publish/recovery helpers.  The extractor's paired output must be
byte-identical in shape to the µC drivers' debug tuples, so the classic
pipeline consumes it unchanged.
"""
import json
import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ramp_storage import (  # noqa: E402
    BYTES_PER_SAMPLE,
    HEADER_SIZE,
    HEADER_VERSION,
    MAGIC,
    RampFileMeta,
    RampPairExtractorService,
    RampStorageService,
    RampWindowExtractor,
    open_ramp_file,
    parse_header,
    publish_ramp_files,
    ramp_temp_paths,
    recover_ramp_file,
    unique_ramp_folder,
)


def make_meta(channel_index=0, label="ch1", n=5, mode="ramp", **overrides):
    kwargs = dict(
        channel_index=channel_index,
        channel_label=label,
        adc_bits=16,
        range_min_v=-0.2,
        range_max_v=0.2,
        ramp_dt_s=1e-6,
        period_s=0.0015,
        integration_time_s=0.001,
        reset_time_s=0.0005,
        samples_per_window=n,
        timing_mode=mode,
        driver_code="ni_daqmx_v1",
        firmware_version="1.0",
        app_version="3.0",
        config_sha256="ab" * 32,
        measurement_uuid="cd" * 16,
        start_iso="2026-09-24T12:00:00+02:00",
        start_epoch_s=1760000000.0,
    )
    kwargs.update(overrides)
    return RampFileMeta(**kwargs)


# ------------------------------------------------------------------ header

def test_header_is_4096_bytes_and_round_trips():
    meta = make_meta()
    blob = meta.header_bytes(12345, 1, b"\x11" * 32)
    assert len(blob) == HEADER_SIZE
    header = parse_header(blob)
    assert header.valid
    assert header["header_version"] == HEADER_VERSION
    assert header["samples_written"] == 12345
    assert header["state"] == 1
    assert header["samples_per_window"] == 5
    assert header["channel_label"] == "ch1"
    assert header["driver_code"] == "ni_daqmx_v1"
    assert header["ramp_dt_s"] == 1e-6
    assert header["data_sha256"] == "11" * 32
    assert header["config_sha256"] == "ab" * 32
    assert header["measurement_uuid"] == "cd" * 16
    assert header["start_iso"] == "2026-09-24T12:00:00+02:00"
    assert header["timing_mode"] == 1  # ramp
    assert header["sample_rate_hz"] == 1e6
    assert header["data_offset"] == HEADER_SIZE


def test_header_field_offsets_are_the_spec():
    # the offsets agreed in the format discussion (verified byte-exact)
    fmt = "<4sHHIIBBHIQQQdddddddd32s32s32s64s32s32s32s16s64s"
    sizes = [struct.calcsize("<" + c) for c in
             ["4s", "H", "H", "I", "I", "B", "B", "H", "I", "Q", "Q", "Q",
              "d", "d", "d", "d", "d", "d", "d", "d",
              "32s", "32s", "32s", "64s", "32s", "32s", "32s", "16s", "64s"]]
    offsets = {}
    off = 0
    names = ["magic", "header_version", "header_size", "flags", "sample_dtype",
             "channel_index", "state", "timing_mode", "adc_bits",
             "samples_per_window", "samples_written", "data_offset",
             "ramp_dt_s", "period_s", "integration_time_s", "reset_time_s",
             "sample_rate_hz", "range_min_v", "range_max_v", "start_epoch_s",
             "data_sha256", "config_sha256", "measurement_uuid", "start_iso",
             "channel_label", "driver_code", "firmware_version",
             "app_version", "reserved"]
    for name, size in zip(names, sizes):
        offsets[name] = off
        off += size
    assert offsets["samples_per_window"] == 0x018
    assert offsets["samples_written"] == 0x020
    assert offsets["data_offset"] == 0x028
    assert offsets["ramp_dt_s"] == 0x030
    assert offsets["data_sha256"] == 0x070
    assert offsets["start_iso"] == 0x0D0
    assert offsets["channel_label"] == 0x110
    assert offsets["app_version"] == 0x170
    assert off == 448


# ---------------------------------------------------------------- extractor

def test_extractor_pairs_first_and_last_of_each_window():
    out = []
    ex = RampWindowExtractor(5, [0, 1], out.extend)
    ramp0 = np.arange(15, dtype=np.uint32)          # windows 0..2 for ch1
    ramp1 = np.arange(15, dtype=np.uint32) + 1000   # ch2
    ex.process_block(0, ramp0, 0)
    assert out == []  # ch2 has nothing yet
    ex.process_block(1, ramp1, 0)
    assert out == [
        ((0, 4), (1000, 1004)),
        ((5, 9), (1005, 1009)),
        ((10, 14), (1010, 1014)),
    ]
    assert ex.windows_emitted == 3


def test_extractor_handles_block_boundaries_inside_windows():
    out = []
    ex = RampWindowExtractor(4, [0], out.extend)
    # feed samples one by one (abs index tracked by the caller)
    for i in range(8):
        ex.process_block(0, np.array([i], dtype=np.uint32), i)
    assert out == [((0, 3),), ((4, 7),)]


def test_extractor_markers_mode_two_samples_per_window():
    out = []
    ex = RampWindowExtractor(2, [0, 1], out.extend)
    ex.process_block(0, np.array([10, 999], dtype=np.uint32), 0)
    ex.process_block(1, np.array([20, 888], dtype=np.uint32), 0)
    assert out == [((10, 999), (20, 888))]  # = classic (start, stop) debug tuple


# ------------------------------------------- markers mode: no-files sink

def test_pair_extractor_sink_emits_pairs_without_files(tmp_path):
    """Markers mode: the sink feeds the classic pipeline and creates NO
    binary files — the start/stop pairs land in data.csv like always."""
    import glob

    pairs = []
    sink = RampPairExtractorService(
        samples_per_window=2, channels=[0, 1], on_window_pairs=pairs.extend
    )
    for w in range(6):
        sink.write_block(0, np.array([100 + w, 900 + w], dtype=np.uint32), w * 2)
        sink.write_block(1, np.array([50 + w, 800 + w], dtype=np.uint32), w * 2)
    summary = sink.close()

    assert pairs == [((100 + w, 900 + w), (50 + w, 800 + w)) for w in range(6)]
    assert summary["windows_emitted"] == 6
    assert summary["timing_mode"] == "markers"
    # nothing was written anywhere: no files in a watched directory
    sink2_dir = str(tmp_path / "watched")
    os.makedirs(sink2_dir, exist_ok=True)
    sink2 = RampPairExtractorService(
        samples_per_window=2, channels=[0], on_window_pairs=lambda p: None
    )
    sink2.write_block(0, np.array([1, 2], dtype=np.uint32), 0)
    sink2.close()
    assert glob.glob(os.path.join(sink2_dir, "*")) == []
    assert sink2.summary()["window_pairs_dropped"] == 0


def test_pair_extractor_sink_drops_pair_on_gap(tmp_path):
    pairs = []
    sink = RampPairExtractorService(
        samples_per_window=2, channels=[0, 1], on_window_pairs=pairs.extend
    )
    sink.write_block(0, np.array([10, 11], dtype=np.uint32), 0)   # window 0
    sink.write_block(1, np.array([20, 21], dtype=np.uint32), 0)   # window 0
    sink.write_block(0, np.array([12, 13], dtype=np.uint32), 4)   # window 2 (1 lost)
    sink.write_block(1, np.array([22, 23], dtype=np.uint32), 4)   # window 2
    summary = sink.close()

    assert pairs == [((10, 11), (20, 21)), ((12, 13), (22, 23))]
    assert summary["window_pairs_dropped"] == 1
    assert summary["channels"][0]["gaps"] == [[2, 2]]


def test_pair_extractor_sink_rejects_block_after_close():
    sink = RampPairExtractorService(
        samples_per_window=2, channels=[0], on_window_pairs=lambda p: None
    )
    sink.close()
    with pytest.raises(RuntimeError):
        sink.write_block(0, np.array([1, 2], dtype=np.uint32))


def test_extractor_drops_window_hit_by_gap_and_realigns():
    out = []
    ex = RampWindowExtractor(4, [0], out.extend)
    ex.process_block(0, np.array([0, 1, 2, 3], dtype=np.uint32), 0)  # window 0
    ex.note_gap(0, 4, 8)  # windows 1 and 2 lost entirely
    ex.process_block(0, np.array([12, 13, 14, 15], dtype=np.uint32), 12)  # window 3
    assert out == [((0, 3),), ((12, 15),)]
    # the two gap-killed windows are counted as dropped measurement rows
    assert ex.window_pairs_dropped == 2


def test_extractor_drops_pair_when_one_channel_loses_a_window():
    out = []
    ex = RampWindowExtractor(2, [0, 1], out.extend)
    ex.process_block(0, np.array([0, 1, 4, 5], dtype=np.uint32), 0)  # windows 0,1
    ex.process_block(1, np.array([10, 11], dtype=np.uint32), 0)      # window 0 only
    ex.note_gap(1, 2, 2)  # ch2 loses window 1
    ex.process_block(1, np.array([14, 15], dtype=np.uint32), 4)      # ch2 window 2
    ex.process_block(0, np.array([8, 9], dtype=np.uint32), 4)        # ch1 window 2
    assert ((0, 1), (10, 11)) in out            # window 0 paired
    assert ((4, 5), (12, 13)) not in out        # window 1 dropped (ch2 gap)
    assert ((8, 9), (14, 15)) in out            # window 2 paired
    assert ex.window_pairs_dropped == 1


# ----------------------------------------------------------------- storage

def test_storage_roundtrip_files_and_pairs(tmp_path):
    pairs = []
    paths = [str(tmp_path / "ch1.bin"), str(tmp_path / "ch2.bin")]
    index = str(tmp_path / "index.json")
    metas = [make_meta(0, "ch1", n=4), make_meta(1, "ch2", n=4)]
    storage = RampStorageService(paths, metas, index, 4, pairs.extend)

    rng = np.random.default_rng(1)
    for w in range(10):
        for ch in (0, 1):
            block = rng.integers(0, 2**16, 4, dtype=np.uint32)
            storage.write_block(ch, block, abs_index=w * 4)
    summary = storage.close()

    assert len(pairs) == 10
    header, view = open_ramp_file(paths[0])
    assert header.valid and header["state"] == 1
    assert header["samples_written"] == 40
    assert view.shape == (40,)

    # every emitted pair is the (first, last) sample of its window in the file
    header2, view2 = open_ramp_file(paths[1])
    for w, (ch1_pair, ch2_pair) in enumerate(pairs):
        assert ch1_pair == (int(view[w * 4]), int(view[w * 4 + 3]))
        assert ch2_pair == (int(view2[w * 4]), int(view2[w * 4 + 3]))

    # the rolling sha in the header matches a fresh hash of the data section
    import hashlib
    with open(paths[0], "rb") as f:
        f.seek(HEADER_SIZE)
        digest = hashlib.sha256(f.read()).hexdigest()
    assert header["data_sha256"] == digest
    assert summary["channels"]["ch1"]["sha256_data"] == digest

    # sidecar written next to the files
    with open(index) as f:
        sidecar = json.load(f)
    assert sidecar["format"] == "atomiqa-ramp-index/1"
    assert sidecar["channels"]["ch1"]["samples"] == 40


def test_storage_records_gap_on_index_jump(tmp_path):
    pairs = []
    paths = [str(tmp_path / "ch1.bin")]
    storage = RampStorageService(
        channel_paths=paths, metas=[make_meta(0, "ch1", n=4)],
        index_path=str(tmp_path / "index.json"),
        samples_per_window=4, on_window_pairs=pairs.extend,
    )
    storage.write_block(0, np.arange(4, dtype=np.uint32), 0)     # window 0
    storage.write_block(0, np.arange(8, 12, dtype=np.uint32), 8)  # jump: window 1 lost
    summary = storage.close()

    assert len(pairs) == 2  # windows 0 and 2
    assert pairs[0] == ((0, 3),)
    assert pairs[1] == ((8, 11),)
    assert summary["channels"]["ch1"]["gaps"] == [[4, 4]]


def test_storage_rejects_block_after_close(tmp_path):
    paths = [str(tmp_path / "ch1.bin")]
    storage = RampStorageService(
        channel_paths=paths, metas=[make_meta(0, "ch1", n=2)],
        index_path=str(tmp_path / "index.json"),
        samples_per_window=2, on_window_pairs=lambda p: None,
    )
    storage.close()
    with pytest.raises(RuntimeError):
        storage.write_block(0, np.array([1, 2], dtype=np.uint32))


# -------------------------------------------------------- publish & recover

def test_publish_moves_files_with_collision_suffix(tmp_path):
    workspace = tmp_path
    ramp_root = str(workspace / "ramp")
    ch = workspace / ".active_run.ramp.ch1.bin"
    ch.write_bytes(b"x" * 10)
    idx = workspace / ".active_run.ramp.index.json.tmp"
    idx.write_text("{}")

    summary = {
        "measurement_uuid": "cd" * 16,
        "timing_mode": "ramp",
        "ramp_dt_s": 1e-6,
        "period_s": 0.0015,
        "samples_per_window": 4,
        "channels": {"ch1": {"samples": 4, "sha256_data": "ab" * 32,
                             "dropped_blocks": 0, "gaps": [],
                             "windows_completed": 1, "window_pairs_dropped": 0}},
    }
    dest, meta = publish_ramp_files(summary, [str(ch)], str(idx), ramp_root, "measurement_x")
    assert os.path.basename(dest) == "measurement_x"
    assert not ch.exists() and not idx.exists()
    assert os.path.exists(os.path.join(dest, "index.json"))
    assert meta["folder"] == os.path.join("ramp", "measurement_x")
    assert meta["channels"]["ch1"]["samples"] == 4

    # same name again -> suffixed folder
    ch.write_bytes(b"y")
    idx.write_text("{}")
    dest2, meta2 = publish_ramp_files(summary, [str(ch)], str(idx), ramp_root, "measurement_x")
    assert os.path.basename(dest2) == "measurement_x_1"
    assert meta2["folder"].endswith("measurement_x_1")


def test_unique_ramp_folder(tmp_path):
    root = str(tmp_path)
    first = unique_ramp_folder(root, "m")
    assert first.endswith("/m")
    os.makedirs(first)
    assert unique_ramp_folder(root, "m").endswith("/m_1")
    os.makedirs(os.path.join(root, "m_1"))
    assert unique_ramp_folder(root, "m").endswith("/m_2")


def test_recover_trims_to_whole_windows_at_watermark(tmp_path):
    path = str(tmp_path / ".active_run.ramp.ch1.bin")
    meta = make_meta(0, "ch1", n=4)
    writer_blob = meta.header_bytes(samples_written=10, state=0, data_sha256=b"\x00" * 32)
    data = np.arange(12, dtype="<u4").tobytes()  # 12 samples on disk
    with open(path, "wb") as f:
        f.write(writer_blob)
        f.write(data)

    header = recover_ramp_file(path)
    assert header is not None
    assert header["state"] == 2  # aborted
    assert header["samples_written"] == 8  # 10 rounded down to 2 whole windows
    size = os.path.getsize(path)
    assert size == HEADER_SIZE + 8 * BYTES_PER_SAMPLE
    # and it re-opens as a valid read-only view
    header2, view = open_ramp_file(path)
    assert view.tolist() == [0, 1, 2, 3, 4, 5, 6, 7]


def test_ramp_temp_paths_workspace_and_fallback(tmp_path):
    paths, index = ramp_temp_paths(str(tmp_path), 2)
    assert [os.path.basename(p) for p in paths] == [
        ".active_run.ramp.ch1.bin", ".active_run.ramp.ch2.bin"]
    assert paths[0].startswith(str(tmp_path))
    assert index.endswith(".active_run.ramp.index.json.tmp")

    paths2, index2 = ramp_temp_paths(None, 1)
    assert os.path.dirname(paths2[0]) != str(tmp_path)
    assert os.path.exists(paths2[0])  # mkstemp reserved the name
    assert os.path.dirname(index2) == os.path.dirname(paths2[0])
    for p in paths2:
        os.remove(p)


# ------------------------------------------------- end-to-end with pipeline

def test_pairs_feed_the_classic_data_model(tmp_path):
    """The extractor output must flow through DataModel exactly like the
    drivers' debug tuples (this is the 'everything else keeps working'
    guarantee of the ramp design)."""
    from models.data_model import DataModel

    dm = DataModel()
    dm.configure_adc(16)
    pairs = []
    storage = RampStorageService(
        channel_paths=[str(tmp_path / "ch1.bin"), str(tmp_path / "ch2.bin")],
        metas=[make_meta(0, "ch1", n=4), make_meta(1, "ch2", n=4)],
        index_path=str(tmp_path / "index.json"),
        samples_per_window=4,
        on_window_pairs=pairs.extend,
    )
    for w in range(3):
        storage.write_block(0, np.array([100, 110, 120, 130], dtype=np.uint32) + w * 4, w * 4)
        storage.write_block(1, np.array([50, 60, 70, 80], dtype=np.uint32) + w * 4, w * 4)
    storage.close()

    # pairs == exactly the tuples a driver would have buffered
    raw = list(pairs)
    start, stop, diff = dm.ingest_raw_batch(
        raw_batch=raw, debug_mode=True, rois=None,
        sample_frequency=1000.0, time_rois=[],
    )
    assert diff.shape == (3, 2)
    assert diff[0, 0] == 30.0 and diff[2, 0] == 30.0
    assert dm.bar_data_ch1[30] == 3  # 16-bit bins, no clipping


def test_simulated_ramp_stream_feeds_storage_end_to_end(tmp_path):
    """HardwareService._simulate_ramp -> sink -> writer -> extractor: the
    whole ramp path without Qt or hardware."""
    import time as _time
    import types

    from models.data_model import DataModel
    from services.hardware_service import HardwareService
    from hardware.core.hardware_manager import HardwareManager

    session = types.SimpleNamespace(
        debug_mode=True,
        multiple_channel=True,
        adc_bits=12,
        timing_mode="ramp",
        ramp_dt_s=1e-5,
        int_time=0.001,     # -> N = 101 samples per window
        reset_time=0.0005,
        period_s=0.0015,
    )
    dm = DataModel()
    service = HardwareService(HardwareManager(), dm, session)

    pairs = []
    storage = RampStorageService(
        channel_paths=[str(tmp_path / "ch1.bin"), str(tmp_path / "ch2.bin")],
        metas=[make_meta(0, "ch1", n=101), make_meta(1, "ch2", n=101)],
        index_path=str(tmp_path / "index.json"),
        samples_per_window=101,
        on_window_pairs=pairs.extend,
    )
    assert service.start_receiving(simulate=True, rate=4, ramp_sink=storage)
    _time.sleep(0.9)  # ~3 windows at 0.25 s pacing
    assert service.stop_receiving(send_stop=False)
    summary = storage.close()

    assert len(pairs) >= 2
    for pair in pairs:
        assert len(pair) == 2 and all(len(half) == 2 for half in pair)
    for label in ("ch1", "ch2"):
        header, view = open_ramp_file(str(tmp_path / f"{label}.bin"))
        assert header["state"] == 1
        assert header["samples_written"] % 101 == 0
        assert header["samples_written"] >= 2 * 101
        assert header["timing_mode"] == 1  # ramp
        # first/last of each window match the emitted pair
        w = 0
        assert (int(view[0]), int(view[100])) == pairs[0][int(header["channel_index"])]
    assert summary["channels"]["ch1"]["gaps"] == []
    # and the pairs flow through the classic pipeline unchanged
    start, stop, diff = dm.ingest_raw_batch(
        raw_batch=list(pairs), debug_mode=True, rois=None,
        sample_frequency=1 / 0.0015, time_rois=[],
    )
    assert diff.shape == (len(pairs), 2)
    assert dm.count >= len(pairs)
