"""
Data-layer tests for the standalone ramp analysis (services/ramp_analysis.py):
gap-aware window mapping over synthetic AQR1 files (the test_ramp_storage
recipe), the counts->volts convention, the ensemble-averaged PSD (equality
with core.calc_stat.one_sided_psd, whose normalization it mirrors), the
derivative modes, ensemble statistics and the folder/archive discovery.
"""
import json
import os
import zipfile

import numpy as np
import pytest

from core.calc_stat import one_sided_psd
from services.ramp_analysis import (
    RampChannel,
    RampDataset,
    derivative,
    derivative_grid,
    ramp_folder_from_zip,
    ramp_measurements_in_workspace,
)
from services.ramp_storage import (
    HEADER_SIZE,
    RampChannelWriter,
    RampFileMeta,
    STATE_FINALIZED,
)

N = 8  # samples per window in the synthetic files


def make_meta(**overrides):
    kwargs = dict(
        channel_index=0,
        channel_label="ch1",
        adc_bits=16,
        range_min_v=-0.2,
        range_max_v=0.2,
        ramp_dt_s=1e-6,
        period_s=1.5e-5,
        integration_time_s=8e-6,
        reset_time_s=5e-6,
        samples_per_window=N,
        timing_mode="ramp",
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


def write_channel(path, counts_rows, meta=None):
    """Write one finalized AQR1 file: one row of N uint32 counts per window."""
    meta = meta or make_meta()
    writer = RampChannelWriter(path, meta)
    for row in counts_rows:
        writer.write(np.asarray(row, dtype=np.uint32))
    writer.finalize(STATE_FINALIZED)
    return meta


def ramp_channel(tmp_path, counts_rows, gaps=None, name="ch1.bin", **meta_kw):
    path = str(tmp_path / name)
    write_channel(path, counts_rows, make_meta(**meta_kw))
    return RampChannel(path, gaps)


def ascending(n_windows, base=0.0):
    """Deterministic windows: window w = base + w*1000 + k."""
    return [
        [int(base + w * 1000 + k) & 0xFFFF for k in range(N)]
        for w in range(n_windows)
    ]


# ------------------------------------------------------------- mapping

def test_no_gaps_maps_to_a_plain_reshape(tmp_path):
    ch = ramp_channel(tmp_path, ascending(5))
    assert ch.n_windows == 5
    assert list(ch.window_ids) == [0, 1, 2, 3, 4]
    assert list(ch.window_offsets) == [0, N, 2 * N, 3 * N, 4 * N]
    win = ch.window(3)
    assert win.shape == (N,)
    # counts 3000..3007 over the 16-bit -0.2..0.2 V range
    slope = 0.4 / (2**16 - 1)
    assert win[0] == pytest.approx(-0.2 + 3000 * slope)
    assert win[-1] == pytest.approx(-0.2 + 3007 * slope)


def test_counts_to_volts_extremes(tmp_path):
    ch = ramp_channel(tmp_path, [[0, 32767, 32768, 65535] + [0] * (N - 4)])
    v = ch.window(0)
    assert v[0] == pytest.approx(-0.2)
    assert v[3] == pytest.approx(0.2)
    # mid-scale count sits a half-LSB above 0 V (full scale = 65535)
    assert v[2] == pytest.approx(-0.2 + 32768 / 65535 * 0.4)


def test_gap_drops_whole_windows_and_reanchors_offsets(tmp_path):
    # windows 0..4 arrive, windows 5-6 are lost (one block), 7..9 arrive:
    # the file holds windows 0..4 then 7..9 contiguously.
    rows = ascending(10)
    kept = rows[:5] + rows[7:10]
    ch = ramp_channel(tmp_path, kept, gaps=[[5 * N, 2 * N]])
    assert list(ch.window_ids) == [0, 1, 2, 3, 4, 7, 8, 9]
    # windows 7-9 sit at the file positions their arrival order implies
    assert list(ch.window_offsets) == [0, N, 2 * N, 3 * N, 4 * N, 5 * N, 6 * N, 7 * N]
    # and the re-anchored windows carry THEIR OWN samples, not window 5's
    win7 = ch.window(7)
    slope = 0.4 / (2**16 - 1)
    assert win7[0] == pytest.approx(-0.2 + 7000 * slope)
    with pytest.raises(KeyError):
        ch.window(5)  # lost to the gap


def test_gap_straddling_a_window_drops_it(tmp_path):
    # windows 0,1 arrive; 5 samples are lost starting inside window 2; then
    # windows 3,4 arrive.  Window 2 straddles the gap, window 4 lacks a full
    # run at the tail -> both invalid.
    rows = ascending(5)
    kept = rows[:2] + rows[3:5]
    ch = ramp_channel(tmp_path, kept, gaps=[[2 * N + 3, 5]])
    assert list(ch.window_ids) == [0, 1, 3]
    assert list(ch.window_offsets) == [0, N, 3 * N - 5]


def test_overlapping_gap_records_are_merged(tmp_path):
    # the index may record the same hole twice (drop + re-anchor): the
    # mapping must not double-count it.
    rows = ascending(4)
    kept = rows[:2] + rows[3:4]
    ch = ramp_channel(tmp_path, kept, gaps=[[2 * N, N], [2 * N, N]])
    assert list(ch.window_ids) == [0, 1, 3]


# ------------------------------------------------------------- spectrum

def test_window_dbv_spectrum_matches_the_oscilloscope_convention(tmp_path):
    """A sine of peak A reads exactly A/sqrt(2) in its line: rms dBV."""
    n = 4096
    fs = 1.0e6
    # ch: dt = 1 us (fs = 1 MHz); counts = volts mapped through the
    # +/-0.2 V, 16-bit range of the fixture meta
    t = np.arange(n) / fs
    a = 0.08            # V peak
    freq = 4 * fs / n   # coherent: bin 4 exactly (df = fs/n = 244.140625)
    volts = a * np.sin(2 * np.pi * freq * t)
    counts = np.rint((volts + 0.2) / 0.4 * (2**16 - 1))
    rows = [counts.astype(int).tolist()]
    ch = ramp_channel(tmp_path, rows, ramp_dt_s=1 / fs,
                      integration_time_s=n / fs, period_s=n / fs + 5e-6,
                      samples_per_window=n)

    freqs, dbv = ch.window_dbv_spectrum(0)
    assert freqs.size == n // 2 - 1                      # DC + Nyquist dropped
    assert freqs[0] == pytest.approx(fs / n)
    peak = float(dbv[int(np.argmax(dbv))])
    assert freqs[int(np.argmax(dbv))] == pytest.approx(freq, rel=1e-9)
    expected = 20 * np.log10(a / np.sqrt(2))
    assert peak == pytest.approx(expected, abs=0.01), (peak, expected)


def test_lag_difference_spectrum_tone_and_one_over_f(tmp_path):
    """Non-overlapping lag-m difference on the Δt = m·dt grid: Nyquist
    shrinks to 1/(2m·dt), a tone reads A·2|sin(πf·Δt)|/√2, the first
    zero sits exactly at 2×Nyquist (never in band), and a pure ramp
    (1/f^2 spectrum) collapses to the floor."""
    n = 4096
    fs = 1.0e6
    m = 5
    n_grid = n // m                      # 819 Δt-spaced samples
    n_diff = n_grid - 1                  # 818 consecutive differences
    dt_eff = m / fs                      # Δt = 5 µs, fs_new = 200 kHz
    c = 200                              # coherent bin of the Δt-grid fft
    freq = c / (n_diff * dt_eff)         # 48.9 kHz < Nyquist 100 kHz
    a = 0.08
    t = np.arange(n) / fs
    rng = np.random.default_rng(4)
    volts = a * np.sin(2 * np.pi * freq * t) + rng.normal(0, 2e-5, n)
    counts = np.rint((volts + 0.2) / 0.4 * (2**16 - 1))
    rows = [counts.astype(int).tolist()] * 4
    ch = ramp_channel(tmp_path, rows, ramp_dt_s=1 / fs,
                      integration_time_s=n / fs, period_s=n / fs + 5e-6,
                      samples_per_window=n)

    freqs, ms, count = ch.lag_difference_spectrum(m)
    assert count == 4
    assert freqs.size == n_diff // 2 - 1                  # DC + Nyquist dropped
    assert np.isclose(freqs[1] - freqs[0], 1.0 / (n_diff * dt_eff))
    # Nyquist (dropped line would be 1/(2Δt)): the last kept bin is one
    # df below it
    assert np.isclose(freqs[-1], 0.5 / dt_eff - 1.0 / (n_diff * dt_eff))
    i = int(np.argmax(ms))
    assert freqs[i] == pytest.approx(freq, rel=1e-9)
    expected = a * 2 * abs(np.sin(np.pi * freq * dt_eff)) / np.sqrt(2)
    assert np.sqrt(ms[i]) == pytest.approx(expected, rel=0.15)  # chi2 scatter
    # no in-band zero of the transfer function: zeros sit at f = 0 (the
    # dropped DC line) and f = 1/Δt = 2×Nyquist (outside the band)
    assert np.all(np.sin(np.pi * freqs * dt_eff) > 0)

    # a pure linear ramp: each difference is the same constant -> after
    # mean removal only the 16-bit quantization residue is left
    ramp = [list(np.rint((np.linspace(-0.1, 0.1, n) + 0.2) / 0.4 * 65535))]
    ch2 = ramp_channel(tmp_path, ramp, ramp_dt_s=1 / fs,
                       integration_time_s=n / fs, period_s=n / fs + 5e-6,
                       samples_per_window=n)
    f2, ms2, _ = ch2.lag_difference_spectrum(m)
    assert float(ms2.max()) < 1e-13
    assert float(ms2.max()) < 1e-9 * float(ms[i])   # the 1/f is gone

    with pytest.raises(ValueError):
        ch.lag_difference_spectrum(0)
    with pytest.raises(ValueError):
        ch.lag_difference_spectrum(n)               # zero difference samples


def test_ramp_spectrum_averaged_matches_single_window_and_tone(tmp_path):
    """The plain ramp FFT uses the oscilloscope's rms convention: a tone
    of amplitude A reads A/sqrt(2), and with ONE window the ensemble
    average IS window_dbv_spectrum."""
    n = 4096
    fs = 1.0e6
    k = 64
    freq = k * fs / n                     # coherent bin
    a = 0.08
    t = np.arange(n) / fs
    volts = a * np.sin(2 * np.pi * freq * t)
    counts = np.rint((volts + 0.2) / 0.4 * (2**16 - 1))
    rows = [counts.astype(int).tolist()]
    ch = ramp_channel(tmp_path, rows, ramp_dt_s=1 / fs,
                      integration_time_s=n / fs, period_s=n / fs + 5e-6,
                      samples_per_window=n)

    freqs, ms, count = ch.ramp_spectrum_averaged()
    assert count == 1
    i = int(np.argmax(ms))
    assert freqs[i] == pytest.approx(freq, rel=1e-9)
    assert np.sqrt(ms[i]) == pytest.approx(a / np.sqrt(2), rel=1e-3)
    # single window: identical to the per-window computation (both sides
    # clamp at the 1e-30 dBV floor — the tone's empty bins are ~zero)
    _f, dbv = ch.window_dbv_spectrum(0)
    np.testing.assert_allclose(
        10.0 * np.log10(np.maximum(ms, 1e-30)), dbv, rtol=1e-12)


def test_averaged_psd_matches_one_sided_psd_per_window(tmp_path):
    rng = np.random.default_rng(42)
    rows = rng.integers(0, 2**16, size=(6, N))
    ch = ramp_channel(tmp_path, rows.tolist())
    fs = 1.0 / ch.dt

    for taper, detrend, remove_mean in (
        ("hann", "mean", True),
        ("rect", "mean", True),
        ("hann", "none", False),
    ):
        freqs, mean, std = ch.averaged_psd(taper=taper, detrend=detrend)
        per_window = [
            one_sided_psd(ch.window(w), fs, window=taper, remove_mean=remove_mean)
            for w in range(6)
        ]
        assert freqs == pytest.approx(np.fft.rfftfreq(N, d=ch.dt))
        assert mean == pytest.approx(np.mean(per_window, axis=0), rel=1e-9)
        assert std == pytest.approx(np.std(per_window, axis=0, ddof=1), rel=1e-9)


def test_averaged_psd_linear_detrend_removes_the_ramp(tmp_path):
    # a perfect linear ramp: after linear detrending every window is the
    # zero signal -> the AC PSD is ~0 (the untrended ramp would dominate).
    t = np.arange(N)
    rows = [(1000 + 3000 * t).astype(int) & 0xFFFF] * 4
    ch = ramp_channel(tmp_path, rows)
    _, mean, _ = ch.averaged_psd(detrend="linear", taper="rect")
    assert np.all(mean[1:] < 1e-15)


def test_averaged_psd_empty_and_invalid_arguments(tmp_path):
    ch = ramp_channel(tmp_path, ascending(1))
    assert ch.averaged_psd() is not None  # single window still works
    with pytest.raises(ValueError):
        ch.averaged_psd(taper="blackman")
    with pytest.raises(ValueError):
        ch.averaged_psd(detrend="quadratic")


# ------------------------------------------------------------- derivative

def test_derivative_diff_of_a_linear_ramp_is_the_slope():
    dt = 2e-6
    v = np.arange(10, dtype=float) * 0.05 * dt  # 0.05 V/s
    d = derivative(v[None, :], dt, mode="diff")[0]
    assert d == pytest.approx(np.full(9, 0.05), abs=1e-15)


def test_derivative_linear_is_the_moving_lsq_slope():
    dt = 1e-6
    t = np.arange(50, dtype=float) * dt
    rng = np.random.default_rng(7)
    v = 0.3 + 0.05 * t + 0.2 * t**2 + rng.normal(0, 1e-9, 50)
    d = derivative(v[None, :], dt, mode="linear", n=9)[0]

    centers = []
    for start in range(50 - 9 + 1):
        sl = slice(start, start + 9)
        centers.append(np.polyfit(t[sl], v[sl], 1)[0])
    assert d == pytest.approx(np.asarray(centers), rel=1e-6, abs=1e-12)

    # symmetric kernel: the quadratic curvature cancels exactly
    quad = (0.2 * t**2)[None, :]
    dq = derivative(quad, dt, mode="linear", n=11)[0]
    expect = 0.4 * derivative_grid(50, dt, "linear", 11)
    assert dq == pytest.approx(expect, rel=1e-9)


def test_derivative_means_is_the_two_boxcar_slope():
    dt = 1e-6
    t = np.arange(50, dtype=float) * dt
    # linear ramp: the two-means slope is exact (unbiased, like the fit)
    v = 0.3 + 0.05 * t
    d = derivative(v[None, :], dt, mode="means", n=9)[0]
    assert d == pytest.approx(np.full(50 - 9 + 1, 0.05), abs=1e-10)
    # reported at the window center, like the moving fit
    assert derivative_grid(50, dt, "means", 9) == pytest.approx(
        derivative_grid(50, dt, "linear", 9)
    )
    # hand check at the first position: window 0..8, halves 0..3 / 5..8,
    # centers 1.5 and 6.5 -> distance 5*dt = (n - n//2)*dt
    left = v[0:4].mean()
    right = v[5:9].mean()
    assert d[0] == pytest.approx((right - left) / (5 * dt), abs=1e-10)
    with pytest.raises(ValueError):
        derivative(np.zeros((1, 10)), 1.0, mode="means", n=4)  # even
    with pytest.raises(ValueError):
        derivative(np.zeros((1, 10)), 1.0, mode="means", n=1)  # too small


def test_derivative_means_noise_is_worse_than_the_linear_fit():
    """Both are unbiased on a line; the fit is BLUE (Gauss–Markov), so the
    two-means estimator is noisier — by sqrt(0.4*n_pts_per_half...) ≈ 1.11x
    in std at n = 11 (theory: var_fit = σ²/(110dt²), var_means =
    0.4σ²/((n-n//2)²dt²) = 0.4σ²/(36dt²))."""
    dt = 1e-6
    rng = np.random.default_rng(11)
    noise = rng.normal(0, 1.0, (200_000, 11))
    d_fit = derivative(noise, dt, mode="linear", n=11)
    d_means = derivative(noise, dt, mode="means", n=11)
    ratio = d_means.std() / d_fit.std()
    assert 1.02 < ratio < 1.2, ratio


def test_derivative_grid_and_shapes():
    assert derivative_grid(N, 1e-6, "diff").shape == (N - 1,)
    assert derivative_grid(N, 1e-6, "linear", 5).shape == (N - 4,)
    assert derivative_grid(10, 2.0, "linear", 5)[0] == pytest.approx(4.0)  # center of samples 0..4
    with pytest.raises(ValueError):
        derivative(np.zeros((1, 10)), 1.0, mode="spline")
    with pytest.raises(ValueError):
        derivative(np.zeros((1, 10)), 1.0, mode="linear", n=4)  # even


# ------------------------------------------------------------- stats

def test_stats_mean_std_and_railed(tmp_path):
    # window 0: counts 1..8; window 1: 8..15; window 2: railed at both ends
    rows = [
        list(range(1, N + 1)),
        list(range(8, 8 + N)),
        [0] + list(range(1, N - 1)) + [65535],
    ]
    ch = ramp_channel(tmp_path, rows)
    stats = ch.stats
    assert stats["count"] == 3
    assert stats["railed_per_window"].tolist() == [0, 0, 2]
    assert stats["railed_total"] == 2
    # the streaming accumulation reproduces the direct computation
    manual = ch.to_volts(np.asarray(rows, dtype=np.float64))
    assert stats["mean"] == pytest.approx(manual.mean(axis=0), rel=1e-12)
    assert stats["std"] == pytest.approx(manual.std(axis=0, ddof=1), rel=1e-9)


# ------------------------------------------------------------- discovery

def make_workspace(tmp_path, with_ramp=True):
    ws = tmp_path / "campaign"
    ws.mkdir()
    stamp = "2026-09-24T12:00:00"
    for i, ramp in enumerate((with_ramp, True)):
        name = f"measurement_{i}"
        settings = {
            "measurement_info": {"timestamp_start": stamp},
        }
        if ramp:
            settings["ramp"] = {"folder": f"ramp/{name}"}
            folder = ws / "ramp" / name
            folder.mkdir(parents=True)
            write_channel(str(folder / "ch1.bin"), ascending(3))
        with zipfile.ZipFile(ws / f"{name}.zip", "w") as zf:
            zf.writestr("settings.json", json.dumps(settings))
    return ws


def test_ramp_folder_from_zip(tmp_path):
    ws = make_workspace(tmp_path)
    found = ramp_folder_from_zip(str(ws / "measurement_0.zip"))
    assert found == str(ws / "ramp" / "measurement_0")
    assert ramp_folder_from_zip(str(ws / "nonexistent.zip")) is None


def test_workspace_scan_lists_only_ramp_measurements(tmp_path):
    ws = make_workspace(tmp_path, with_ramp=False)
    found = ramp_measurements_in_workspace(str(ws))
    assert len(found) == 1  # only measurement_1 has ramp files
    assert found[0]["folder"] == str(ws / "ramp" / "measurement_1")
    assert os.path.isfile(found[0]["zip_path"])


def test_dataset_opens_every_channel(tmp_path):
    folder = tmp_path / "ramp" / "m"
    folder.mkdir(parents=True)
    write_channel(str(folder / "ch1.bin"), ascending(2))
    write_channel(
        str(folder / "ch2.bin"),
        ascending(2, base=5000),
        make_meta(channel_index=1, channel_label="ch2"),
    )
    (folder / "index.json").write_text(json.dumps({"measurement_uuid": "u" * 32}))

    ds = RampDataset(str(folder))
    assert sorted(ds.channels) == ["ch1", "ch2"]
    assert ds.uuid == "u" * 32
    assert ds.channels["ch1"].window(1)[0] != ds.channels["ch2"].window(1)[0]

    with pytest.raises(ValueError):
        RampDataset(str(tmp_path / "empty"))
