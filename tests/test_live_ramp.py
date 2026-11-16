"""
Live ramp sink tests (services/live_ramp.py): full-window assembly from
interleaved/straddling blocks, gap handling, the FFT/CDS results, the
selection-reset contract, and the simulator-driven end-to-end path through
HardwareService (including the is_receiving mutual-exclusion flag).
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.hardware_service import HardwareService  # noqa: E402
from services.live_ramp import LiveRampSink  # noqa: E402
from services.ramp_storage import (  # noqa: E402
    RampChannelWriter,
    RampFileMeta,
    STATE_FINALIZED,
)
from services.ramp_analysis import RampChannel  # noqa: E402
from models.data_model import DataModel  # noqa: E402
from models.session_model import SessionModel  # noqa: E402

N = 64
DT = 1e-6
BITS = 20
VMIN, VMAX = -5.0, 5.0
FULL = 2 ** BITS - 1


def to_counts(volts):
    return np.clip(
        np.rint((np.asarray(volts) - VMIN) / (VMAX - VMIN) * FULL),
        0, FULL).astype(np.uint32)


def to_volts(counts):
    return VMIN + np.asarray(counts, dtype=np.float64) / FULL * (VMAX - VMIN)


def settle(sink, timeout=2.0):
    """Wait for the sink thread to drain the queue."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sink._queue.empty():
            time.sleep(0.05)
            return
        time.sleep(0.02)


def make_sink(channels=(0, 1), n=N):
    return LiveRampSink(n, list(channels), DT, BITS, VMIN, VMAX)


# --------------------------------------------------------------- assembly

def test_window_assembled_from_straddling_blocks():
    sink = make_sink(channels=(0,))
    rng = np.random.default_rng(7)
    counts = to_counts(np.linspace(0.1, 1.0, N)) + rng.integers(0, 4, N)
    # feed in uneven chunks that straddle window boundaries — the sink has
    # exactly one window, so every chunk boundary is a partial fill
    pos = 0
    for size in (7, 30, 1, 26):
        sink.write_block(0, counts[pos:pos + size])
        pos += size
    sink.close()
    snap = sink.snapshot(0)
    assert snap["count"] == 1
    assert snap["windows_seen"] == 1
    gen, volts = sink.latest_volts(0)
    assert gen == 1
    np.testing.assert_allclose(volts, to_volts(counts), rtol=1e-12)


def test_consecutive_windows_accumulate():
    sink = make_sink(channels=(0,))
    for k in range(5):
        sink.write_block(0, np.full(N, 1000 * (k + 1), dtype=np.uint32))
    sink.close()
    snap = sink.snapshot(0)
    assert snap["count"] == 5 and snap["windows_seen"] == 5
    assert snap["start_v"].size == 5 and snap["diff_v"].size == 5


def test_gap_abandons_partial_window_only():
    sink = make_sink(channels=(0,))
    sink.write_block(0, np.full(N, 5, dtype=np.uint32))       # complete w0
    sink.write_block(0, np.full(N // 2, 6, dtype=np.uint32))  # half of w1
    sink.report_gap(0, N + N // 2, 3)                          # hole in w1
    sink.write_block(0, np.full(N, 7, dtype=np.uint32))       # complete w2
    sink.close()
    snap = sink.snapshot(0)
    assert snap["windows_seen"] == 2
    assert sink.summary()["gap_windows"][0] == 1
    # w1's stale half-fill must not leak into w2: all values identical
    np.testing.assert_allclose(snap["start_v"], to_volts(np.array([5, 7])))


# ------------------------------------------------------------------- FFT

def test_spectrum_tone_reads_half_peak_squared():
    sink = make_sink(channels=(0,))
    j = np.arange(N)
    k = 12  # exact FFT line: v[j] = A sin(2 pi k j / N)
    tone = 0.8 * np.sin(2 * np.pi * k * j / N)
    sink.write_block(0, to_counts(0.4 + tone))
    sink.write_block(0, to_counts(0.4 + tone))
    sink.close()
    snap = sink.snapshot(0)
    assert snap["count"] == 2
    assert snap["freqs"][k - 1] == pytest.approx(k / (N * DT))  # DC dropped
    assert snap["ms"][k - 1] == pytest.approx(0.8 ** 2 / 2, rel=1e-3)


def test_spectrum_identity_with_aqr1_ramp_channel(tmp_path):
    """The sink's averaged spectrum must equal the offline
    ramp_spectrum_averaged / window_dbv_spectrum convention bit for bit."""
    rng = np.random.default_rng(3)
    windows = [rng.normal(0.3, 0.05, N) for _ in range(3)]

    sink = make_sink(channels=(0,))
    for v in windows:
        sink.write_block(0, to_counts(v))
    sink.close()
    freqs, ms, count = (lambda s: (s["freqs"], s["ms"], s["count"]))(sink.snapshot(0))
    assert count == 3

    # same counts into an AQR1 file, read back through RampChannel
    path = str(tmp_path / "ch1.bin")
    meta = RampFileMeta(
        channel_index=0, channel_label="ch1", adc_bits=BITS,
        range_min_v=VMIN, range_max_v=VMAX, ramp_dt_s=DT,
        period_s=1e-3, integration_time_s=(N - 1) * DT, reset_time_s=1e-4,
        samples_per_window=N, timing_mode="ramp", driver_code="test",
        firmware_version="1.0", app_version="3.0", config_sha256="ab" * 32,
        measurement_uuid="cd" * 16, start_iso="2026-11-03T12:00:00+01:00",
        start_epoch_s=1760000000.0,
    )
    writer = RampChannelWriter(path, meta)
    for v in windows:
        writer.write(to_counts(v))
    writer.finalize(STATE_FINALIZED)

    ch = RampChannel(path)
    rf, rms, rcount = ch.ramp_spectrum_averaged()
    assert rcount == 3
    np.testing.assert_allclose(freqs, rf, rtol=1e-12)
    np.testing.assert_allclose(ms, rms, rtol=1e-12)
    # and one window against the single-window dBV helper
    _, dbv = ch.window_dbv_spectrum(0)
    np.testing.assert_allclose(
        10 * np.log10(np.maximum(ms, 1e-30)),
        10 * np.log10(np.maximum(rms, 1e-30)),
        atol=1e-9)


def test_fft_segment_selection_averages_that_segment_only():
    sink = make_sink(channels=(0,))
    assert sink.set_selection(fft_span=(10, N - 10)) is True
    t = np.arange(N) * DT
    k = 4
    tone = 0.2 * np.sin(2 * np.pi * k * t / (N * DT) * DT)
    sink.write_block(0, to_counts(0.5 + tone))
    sink.close()
    snap = sink.snapshot(0)
    n_seg = N - 20
    assert snap["freqs"].size == n_seg // 2 - 1  # even n': DC+Nyquist dropped
    assert snap["freqs"][0] == pytest.approx(1.0 / (n_seg * DT))
    # the tone spreads over the new grid; only the resolution is checked here


# ------------------------------------------------------------------- CDS

def test_cds_values_are_the_chosen_single_samples():
    sink = make_sink(channels=(0,))
    i_start, i_stop = 5, N - 5
    assert sink.set_selection(cds_points=(i_start, i_stop)) is True
    counts = np.arange(N, dtype=np.uint32) * 1000
    sink.write_block(0, counts)
    sink.write_block(0, counts + 500)
    sink.close()
    snap = sink.snapshot(0)
    np.testing.assert_allclose(
        snap["start_v"], [to_volts(counts[i_start]),
                          to_volts(counts[i_start] + 500)], rtol=1e-12)
    np.testing.assert_allclose(
        snap["stop_v"], [to_volts(counts[i_stop]),
                         to_volts(counts[i_stop] + 500)], rtol=1e-12)
    np.testing.assert_allclose(
        snap["diff_v"], snap["stop_v"] - snap["start_v"], rtol=1e-12)


# ------------------------------------------------------ selection & reset

def test_selection_change_resets_unchanged_does_not():
    sink = make_sink(channels=(0,))
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    settle(sink)
    assert sink.snapshot(0)["count"] == 1
    assert sink.set_selection(cds_points=(3, 9)) is True
    snap = sink.snapshot(0)
    assert snap["count"] == 0 and snap["diff_v"].size == 0
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    settle(sink)
    assert sink.snapshot(0)["count"] == 1
    # re-setting the SAME selection must not restart anything
    assert sink.set_selection(cds_points=(3, 9)) is False
    assert sink.set_selection(fft_span=(0, N), cds_points=(3, 9)) is False
    assert sink.snapshot(0)["count"] == 1
    sink.close()


def test_reset_keeps_latest_window_clears_results():
    sink = make_sink(channels=(0,))
    sink.write_block(0, np.arange(N, dtype=np.uint32))
    settle(sink)
    gen_before = sink.snapshot(0)["latest_gen"]
    sink.reset()
    snap = sink.snapshot(0)
    assert snap["count"] == 0 and snap["diff_v"].size == 0
    assert snap["windows_seen"] == 0
    # the preview survives the reset
    gen, volts = sink.latest_volts(0)
    assert gen == gen_before and volts is not None
    sink.close()


def test_invalid_selection_rejected():
    sink = make_sink(channels=(0,))
    with pytest.raises(ValueError):
        sink.set_selection(fft_span=(0, N + 1))
    with pytest.raises(ValueError):
        sink.set_selection(fft_span=(5, 5))
    with pytest.raises(ValueError):
        sink.set_selection(cds_points=(0, N))
    sink.close()


def test_snapshot_returns_independent_copies():
    sink = make_sink(channels=(0,))
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    settle(sink)
    snap = sink.snapshot(0)
    snap["ms"][:] = 123.0
    snap["diff_v"][:] = 9.0
    again = sink.snapshot(0)
    assert not np.any(again["ms"] == 123.0)
    assert not np.any(again["diff_v"] == 9.0)
    sink.close()


def test_close_is_idempotent_and_closes_the_stream():
    sink = make_sink(channels=(0,))
    sink.close()
    sink.close()
    with pytest.raises(RuntimeError):
        sink.write_block(0, np.full(N, 1, dtype=np.uint32))


def test_channel_independence():
    sink = make_sink(channels=(0, 1))
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    sink.write_block(1, np.full(N, 20, dtype=np.uint32))
    sink.write_block(1, np.full(N, 20, dtype=np.uint32))
    settle(sink)
    assert sink.snapshot(0)["count"] == 1
    assert sink.snapshot(1)["count"] == 2
    sink.close()


# ------------------------------------------- simulator end-to-end + flag

def sim_session():
    s = SessionModel()
    s.timing_mode = "ramp"
    s.int_time = (N - 1) * DT
    s.reset_time = 1e-4
    s.ramp_dt_s = DT
    s.samples_per_window = N
    s.adc_bits = BITS
    s.adc_input_range = (VMIN, VMAX)
    return s


def test_simulator_feeds_the_sink_and_is_receiving_flag():
    session = sim_session()  # multiple_channel defaults True -> ch0 and ch1
    data_model = DataModel()
    service = HardwareService(None, data_model, session)
    assert service.is_receiving is False

    sink = make_sink(channels=(0, 1))
    ok = service.start_receiving(simulate=True, rate=500, ramp_sink=sink)
    assert ok is True
    assert service.is_receiving is True
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if sink.snapshot(0)["count"] >= 3 and sink.snapshot(1)["count"] >= 3:
                break
            time.sleep(0.05)
    finally:
        assert service.stop_receiving(send_stop=False) is True
        assert service.is_receiving is False
        sink.close()
    assert sink.snapshot(0)["count"] >= 3
    assert sink.snapshot(0)["windows_seen"] >= 3
    assert sink.snapshot(1)["count"] >= 3


def test_is_receiving_stays_false_when_real_start_fails():
    import types

    session = sim_session()
    manager = types.SimpleNamespace(acquisition=None)  # no acquisition device
    service = HardwareService(manager, DataModel(), session)
    ok = service.start_receiving(simulate=False, ramp_sink=None)
    assert ok is False
    assert service.is_receiving is False


# ------------------------------------------------------------ diagnostics

def test_input_counters_exposed():
    sink = make_sink(channels=(0,))
    for k in range(3):
        sink.write_block(0, np.full(N // 2, 5 + k, dtype=np.uint32))
    sink.close()
    snap = sink.snapshot(0)
    assert snap["blocks_in"] == 3
    assert snap["samples_in"] == 3 * (N // 2)
    summary = sink.summary()
    assert summary["blocks_in"][0] == 3
    assert summary["samples_in"][0] == 3 * (N // 2)
    # gap accounting rides along (int per channel in the snapshot)
    assert snap["gap_windows"] == 0


def test_stream_restart_drops_stale_partial():
    """Regression (live finding): a reused sink (stop -> start WITHOUT
    closing) receives a stream re-anchored at abs 0 while a partial window
    from the previous run is still in progress.  Keeping the stale partial
    makes the fill counter skip the exact N boundary — assembly wedges and
    millions of samples arrive with ZERO windows completing."""
    sink = make_sink(channels=(0,))
    # run 1: one full window + a partial tail, explicit driver counters
    sink.write_block(0, np.full(N, 10, dtype=np.uint32), 0)
    sink.write_block(0, np.full(30, 10, dtype=np.uint32), N)
    settle(sink)
    assert sink.snapshot(0)["count"] == 1

    # run 2: the driver's counters restart at 0
    for k in range(3):
        sink.write_block(0, np.full(N, 20 + k, dtype=np.uint32), k * N)
    sink.close()
    snap = sink.snapshot(0)
    assert snap["count"] == 4            # 1 from run 1 + 3 from run 2
    assert snap["windows_seen"] == 4
    assert snap["latest_gen"] == 4
    # the new windows are pure (no stale-partial leakage into the values)
    np.testing.assert_allclose(
        snap["start_v"][1:], [to_volts(np.array([20 + k]))[0] for k in range(3)],
        rtol=1e-12)


# ------------------------------------------------------- high-pass filter

# realistic geometry for filter tests: 8192 samples at fs = 100 kHz
# (82 ms window) — several periods of fc fit inside one window
HP_N, HP_DT = 8192, 1e-5


def hp_sink():
    return LiveRampSink(HP_N, [0], HP_DT, BITS, VMIN, VMAX)


def test_highpass_removes_the_ramp_tail():
    """A pure linear ramp's 1/f^2 tail must collapse with the HP on (why it
    exists: the tail buries the parallel-current noise at every f)."""
    def run(hp):
        sink = hp_sink()
        if hp:
            assert sink.set_highpass(True, 1e3) is True
        sink.write_block(0, to_counts(np.linspace(0.2, 0.7, HP_N)))
        settle(sink)
        sink.close()
        return sink.snapshot(0)["ms"][:8].sum()

    plain = run(False)
    filtered = run(True)
    assert 10 * np.log10(plain / filtered) > 40.0


def test_highpass_tone_pass_and_cut():
    fs = 1.0 / HP_DT
    for freq, should_pass in ((fs / 8, True), (fs / 800, False)):  # vs fc = 1 kHz
        sink = hp_sink()
        sink.set_highpass(True, 1e3)
        j = np.arange(HP_N)
        sink.write_block(0, to_counts(0.5 + 0.1 * np.sin(2 * np.pi * freq * j / fs)))
        settle(sink)
        sink.close()
        peak = 10 * np.log10(sink.snapshot(0)["ms"].max())
        if should_pass:
            assert peak > -28.0        # ~ -23 dBV tone, unattenuated
        else:
            assert peak < -50.0        # deep in the stopband


def test_set_highpass_state_contract():
    sink = make_sink(channels=(0,))
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    settle(sink)
    assert sink.highpass == (False, 0.0)
    assert sink.set_highpass(True, 5e3) is True
    snap = sink.snapshot(0)
    assert snap["count"] == 0 and snap["diff_v"].size == 0   # reset
    sink.write_block(0, np.full(N, 10, dtype=np.uint32))
    settle(sink)
    assert sink.snapshot(0)["count"] == 1
    assert sink.set_highpass(True, 5e3) is False             # same -> no reset
    assert sink.set_highpass(False) is True                  # off resets too
    with pytest.raises(ValueError):
        sink.set_highpass(True, 0.0)
    with pytest.raises(ValueError):
        sink.set_highpass(True, 0.6 / DT)                    # above Nyquist
    sink.close()
    assert sink.snapshot(0)["count"] == 0


def test_highpass_leaves_cds_unfiltered():
    """CDS measures the REAL start/stop values — filtering must not touch."""
    sink = make_sink(channels=(0,))
    sink.set_highpass(True, 10e3)
    counts = np.arange(N, dtype=np.uint32) * 1000
    sink.write_block(0, counts)
    settle(sink)
    sink.close()
    snap = sink.snapshot(0)
    np.testing.assert_allclose(snap["start_v"], [to_volts(0)], rtol=1e-9)
    np.testing.assert_allclose(snap["stop_v"], [to_volts((N - 1) * 1000)],
                               rtol=1e-9)
