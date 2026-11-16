"""
Offscreen UI tests for the standalone ramp analysis window
(views/ramp_analysis_window.py): folder/workspace opening, the facts line,
range selection feeding the overlay plots, the derivative mode switch and
the spectrum cache.  The math itself is covered by test_ramp_analysis.py.
"""
import json
import os
import sys
import zipfile

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from services.ramp_storage import (  # noqa: E402
    RampChannelWriter,
    RampFileMeta,
    STATE_FINALIZED,
)
from views.ramp_analysis_window import PAGE_ROWS, RampAnalysisWindow  # noqa: E402

N = 16
WINDOWS = 40


def make_meta(label, channel_index):
    return RampFileMeta(
        channel_index=channel_index,
        channel_label=label,
        adc_bits=16,
        range_min_v=-5.0,
        range_max_v=5.0,
        ramp_dt_s=1e-5,
        period_s=1e-4,
        integration_time_s=1.5e-4,
        reset_time_s=5e-5,
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


def write_channel(path, label, index, base):
    writer = RampChannelWriter(path, make_meta(label, index))
    for w in range(WINDOWS):
        row = ((base + w * 1000 + np.arange(N)) * 400) & 0xFFFF
        writer.write(row.astype(np.uint32))
    writer.finalize(STATE_FINALIZED)


@pytest.fixture
def ramp_folder(tmp_path):
    folder = tmp_path / "ramp" / "measurement_0"
    folder.mkdir(parents=True)
    write_channel(str(folder / "ch1.bin"), "ch1", 0, base=10)
    write_channel(str(folder / "ch2.bin"), "ch2", 1, base=20)
    (folder / "index.json").write_text(
        json.dumps({"measurement_uuid": "cd" * 16, "channels": {}})
    )
    return folder


@pytest.fixture
def window():
    return RampAnalysisWindow()


@pytest.fixture
def warnings_silenced(monkeypatch):
    """Record modal warnings instead of blocking on them (the empty
    workspace scan now warns loudly — a real dialog would hang the
    offscreen tests)."""
    shown = []
    monkeypatch.setattr(
        "views.ramp_analysis_window.QMessageBox.warning",
        lambda *a, **k: shown.append(a),
    )
    return shown


def _data_items(plot):
    return plot.getPlotItem().listDataItems()


def test_open_folder_populates_channel_facts_and_table(window, ramp_folder):
    window.open_path(str(ramp_folder))
    assert window.channel_combo.count() == 2
    assert window._channel is not None
    assert window._channel.n_windows == WINDOWS
    assert f"{WINDOWS} valid windows" in window.facts_label.text()
    assert "dt = 10 µs" in window.facts_label.text()
    assert window.table.rowCount() == min(PAGE_ROWS, WINDOWS)
    assert window.page_label.text() == "page 1/1"
    # the spectrum is not computed on load (heavy pass: only on the tab)
    assert window._spec_cache == {}


def test_range_selection_feeds_the_overlay(window, ramp_folder):
    window.open_path(str(ramp_folder))
    # loading never runs the whole-file ensemble pass (huge files must
    # open instantly — the Average button does the heavy work on demand)
    assert not window._channel.has_stats
    window.range_edit.setText("2-5, 7")
    window._on_select_ranges()
    assert window._selected == {2, 3, 4, 5, 7}
    assert "5 window(s)" in window.selected_label.text()

    window._replot()
    # one curve per selected window, no ensemble band yet
    assert len(_data_items(window.ramp_plot)) == 5
    # the same selection drives the derivative tab
    assert len(_data_items(window.deriv_plot)) == 5


def test_even_fit_width_snaps_to_odd(window, ramp_folder):
    """Typing an even n bypasses the spinbox's odd-only step; the moving
    linear fit needs an odd window (ValueError otherwise)."""
    window.open_path(str(ramp_folder))
    window.deriv_mode.setCurrentIndex(1)          # moving linear fit
    window.deriv_n.setValue(10)                   # typed even
    assert window.deriv_n.value() == 11
    # switching to the fit mode with an even value in place also snaps
    window.deriv_n.setValue(8)
    window.deriv_mode.setCurrentIndex(0)
    window.deriv_mode.setCurrentIndex(1)
    assert window.deriv_n.value() == 9


def test_export_mean_csv_carries_the_timing_header(window, ramp_folder, tmp_path, monkeypatch):
    """The average ramp exports as CSV; the header must carry the applied
    timing (reset time above all) so several exports plot together."""
    from PyQt6.QtWidgets import QFileDialog

    window.open_path(str(ramp_folder))
    # without the ensemble pass there is nothing to export yet
    window._on_export_mean()
    assert "press Average first" in window.statusBar().currentMessage()

    _run_average(window)
    out = str(tmp_path / "export.csv")
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (out, "CSV (*.csv)")))
    window._on_export_mean()

    lines = open(out, encoding="utf-8").read().splitlines()
    header = [ln for ln in lines if ln.startswith("#")]
    joined = "\n".join(header)
    assert "reset_time_s: 5e-05" in joined
    assert "integration_time_s: 0.00015" in joined
    assert "ramp_dt_s: 1e-05" in joined
    assert f"windows_averaged: {WINDOWS}" in joined
    assert lines[len(header)] == "time_s,mean_v,std_v"
    data = [ln for ln in lines[len(header) + 1:] if ln]
    assert len(data) == N
    first = data[0].split(",")
    assert float(first[0]) == 0.0
    # the exported mean is exactly the channel's ensemble mean
    assert float(first[1]) == pytest.approx(window._channel.stats["mean"][0], rel=1e-9)
    assert float(first[2]) == pytest.approx(window._channel.stats["std"][0], rel=1e-9)


def test_average_worker_failure_does_not_wedge_the_ui(window, ramp_folder, monkeypatch):
    """An exception in the background pass must surface as a message and
    re-enable the button — never a dead thread with a stuck progress bar."""
    import time

    window.open_path(str(ramp_folder))
    window.deriv_source.setCurrentIndex(1)   # per-window band: pass 2 runs

    def boom(mode, n, progress=None):
        raise ValueError("the moving linear fit needs an odd n >= 3")

    monkeypatch.setattr(window, "_derivative_band", boom)
    window.avg_btn.click()
    assert window.avg_btn.isEnabled() is False
    deadline = time.time() + 10
    while window._avg_thread is not None and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()
    assert window._avg_thread is None            # thread finished, not dead-locked
    assert window.avg_btn.isEnabled() is True    # button recovered
    assert window.avg_progress.isHidden()
    assert "averaging failed" in window.statusBar().currentMessage()
    assert window._channel.has_stats            # pass 1 still landed before the failure


def _run_average(window):
    import time

    window.avg_btn.click()
    deadline = time.time() + 10
    while window._avg_thread is not None and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()
    assert window._avg_thread is None


def test_average_button_computes_bands_in_background(window, ramp_folder):
    window.open_path(str(ramp_folder))
    window.range_edit.setText("0-4")
    window._on_select_ranges()

    # default source is "of the mean": only the statistics pass runs
    _run_average(window)
    assert window._channel.has_stats
    assert "railed samples" in window.facts_label.text()
    assert window._deriv_cache == {}                      # band pass skipped
    # ramps tab: band polygon + dashed mean line + 5 ramps
    assert len(_data_items(window.ramp_plot)) == 2 + 5
    # derivative tab: the dashed d(mean) line + 5 ramps
    assert len(_data_items(window.deriv_plot)) == 1 + 5

    # per-window source: pass 2 runs, the band appears, and its central
    # line equals the derivative of the mean (linear modes)
    window.deriv_source.setCurrentIndex(1)
    _run_average(window)
    key = (window.deriv_mode.currentData(), window.deriv_n.value())
    assert key in window._deriv_cache
    assert len(_data_items(window.deriv_plot)) == 2 + 5
    window.deriv_source.setCurrentIndex(0)   # back to d(mean)
    window._draw_derivative()
    items = _data_items(window.deriv_plot)
    d_mean = np.asarray(items[0].yData, float)
    _x, band_mean, _std = window._deriv_cache[key]
    np.testing.assert_allclose(d_mean, band_mean, rtol=1e-9, atol=1e-12)


def test_selection_outside_valid_windows_is_ignored(window, ramp_folder):
    window.open_path(str(ramp_folder))
    window.range_edit.setText("0, 999999")
    window._on_select_ranges()
    assert window._selected == {0}


def test_table_checkbox_toggles_selection(window, ramp_folder):
    window.open_path(str(ramp_folder))
    window.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert 0 in window._selected
    window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert 0 not in window._selected


def test_derivative_mode_switches_output_length(window, ramp_folder):
    window.open_path(str(ramp_folder))
    window._selected = {3}
    window._deriv_cache.clear()

    window.deriv_mode.setCurrentIndex(0)  # consecutive difference
    window._draw_derivative()
    diff_curve = _data_items(window.deriv_plot)[-1]
    assert diff_curve.yData.shape[0] == N - 1

    window.deriv_mode.setCurrentIndex(1)  # moving linear fit
    window._draw_derivative()
    fit_curve = _data_items(window.deriv_plot)[-1]
    assert fit_curve.yData.shape[0] == N - window.deriv_n.value() + 1


def _run_spec_pass(window):
    """Wait for the background lag-m spectrum pass to finish."""
    import time

    deadline = time.time() + 10
    while window._spec_thread is not None and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.01)
    _app.processEvents()
    assert window._spec_thread is None


def test_spectrum_tab_lag_difference_averaged(window, ramp_folder, tmp_path):
    """Entering the Spectrum tab computes the ensemble-averaged lag-m
    difference spectrum in the background; Δt = m·dt and the SHRINKING
    Nyquist = 1/(2Δt) are in the status line."""
    window.open_path(str(ramp_folder))
    window.tabs.setCurrentIndex(2)          # Spectrum tab: starts the pass
    _run_spec_pass(window)
    assert window.spec_progress.isHidden()
    assert 1 in window._spec_cache
    item = _data_items(window.spectrum_plot)[0]
    x = 10.0 ** np.asarray(item.getData()[0], float)   # log-x: getData is log10
    # m = 1: 16 grid points -> 15 differences (odd) -> 8 lines, DC dropped
    # -> 7 points, df = 1/(15*dt) = 6666.7 Hz
    assert x.size == 7
    assert np.isclose(x[0], 1.0 / (15 * 1e-5))
    assert "Δt = m·dt = 1e-05 s" in window.spectrum_status.text()
    assert "Nyquist = 1/(2Δt) = 50000 Hz" in window.spectrum_status.text()
    assert f"{WINDOWS} windows" in window.spectrum_status.text()

    # the plotted curve equals the service computation for m = 1.
    # (Compare the item's SOURCE data: getData() returns pyqtgraph's
    # clipToView DISPLAY cache, which can still hold a stale slice.)
    freqs, ms, count = window._channel.lag_difference_spectrum(1)
    np.testing.assert_allclose(
        10.0 * np.log10(ms),
        np.asarray(item.yData, float),
        rtol=1e-9,
    )
    assert count == WINDOWS

    # m > 1 needs more samples per window: open a 64-sample channel and
    # check that Δt and the Nyquist SHRINK with m (the fixture's N=16
    # caps the spin at m = 1 — the guard itself)
    assert window.lag_spin.maximum() == 1   # 16 // 9

    from services.ramp_storage import RampChannelWriter, RampFileMeta, STATE_FINALIZED

    big = tmp_path / "ramp" / "big"
    big.mkdir(parents=True)
    meta = RampFileMeta(
        channel_index=0, channel_label="ch1", adc_bits=16,
        range_min_v=-5.0, range_max_v=5.0, ramp_dt_s=1e-5,
        period_s=1e-3, integration_time_s=6.4e-4, reset_time_s=5e-5,
        samples_per_window=64, timing_mode="ramp", driver_code="ni_daqmx_v1",
        firmware_version="1.0", app_version="3.0", config_sha256="ab" * 32,
        measurement_uuid="cd" * 16, start_iso="2026-09-24T12:00:00",
        start_epoch_s=1760000000.0)
    writer = RampChannelWriter(str(big / "ch1.bin"), meta)
    for w in range(20):
        writer.write(((100 + w + np.arange(64)) & 0xFFFF).astype(np.uint32))
    writer.finalize(STATE_FINALIZED)
    (big / "index.json").write_text(json.dumps({"channels": {}}))

    window.open_path(str(big))
    assert window._spec_cache == {}         # channel switch cleared it
    assert window.lag_spin.maximum() == 7   # 64 // 9
    window.tabs.setCurrentIndex(2)
    _run_spec_pass(window)
    window.lag_spin.setValue(2)
    window._spec_timer.stop()
    window._spec_start()
    _run_spec_pass(window)
    item = _data_items(window.spectrum_plot)[0]
    x2 = 10.0 ** np.asarray(item.getData()[0], float)
    # m = 2: 32 grid points -> 31 differences (odd) -> 16 lines -> 15 pts
    assert x2.size == 15
    assert np.isclose(x2[-1], 15.0 / (31 * 2e-5))       # Nyquist - df/2
    assert "Δt = m·dt = 2e-05 s" in window.spectrum_status.text()
    assert "Nyquist = 1/(2Δt) = 25000 Hz" in window.spectrum_status.text()


def test_spectrum_ramp_overlay(window, ramp_folder):
    """The 'with raw ramp FFT' checkbox overlays the ensemble-averaged
    plain ramp spectrum (full band, 1/(2dt)) next to the difference —
    computed in the same background pass machinery."""
    window.open_path(str(ramp_folder))
    window.tabs.setCurrentIndex(2)
    _run_spec_pass(window)
    assert len(_data_items(window.spectrum_plot)) == 1   # difference only
    assert window._ramp_cache is None

    window.ramp_check.setChecked(True)     # triggers its own pass
    _run_spec_pass(window)
    assert window._ramp_cache is not None
    items = _data_items(window.spectrum_plot)
    assert len(items) == 2                 # difference + ramp overlay
    # the ramp curve spans the FULL band to 1/(2dt) = 50 kHz
    xr = np.asarray(items[1].yData, float) * 0 + 10.0 ** np.asarray(
        items[1].getData()[0], float)
    assert np.isclose(xr[-1], 0.5 / 1e-5 - 1.0 / (16 * 1e-5))  # Nyq - df
    assert "ramp FFT: Nyquist = 1/(2dt) = 50000 Hz" in window.spectrum_status.text()
    # the ramp overlay is cached: toggling off/on draws without a pass
    window.ramp_check.setChecked(False)
    assert len(_data_items(window.spectrum_plot)) == 1
    window.ramp_check.setChecked(True)
    assert len(_data_items(window.spectrum_plot)) == 2
    assert window._spec_thread is None


def test_open_workspace_lists_measurements(window, tmp_path, ramp_folder):
    ws = tmp_path / "campaign"
    ws.mkdir()
    (ws / "ramp").symlink_to(ramp_folder.parent, target_is_directory=True)
    settings = {
        "measurement_info": {"timestamp_start": "2026-09-24T12:00:00"},
        "ramp": {"folder": "ramp/measurement_0"},
    }
    with zipfile.ZipFile(ws / "measurement.zip", "w") as zf:
        zf.writestr("settings.json", json.dumps(settings))

    window.open_path(str(ws))
    assert window.measurement_combo.count() == 1
    assert window._channel is not None  # auto-opened the newest


def test_workspace_scan_understands_windows_backslash_paths(window, tmp_path, ramp_folder):
    """Archives saved on Windows record the ramp folder with backslashes
    ('ramp\\test_3'): on Linux the naive join looks for one folder
    LITERALLY named 'ramp\\test_3' and the scan finds nothing — the
    user's 'I select the workspace and nothing happens' (live finding)."""
    ws = tmp_path / "campaign"
    ws.mkdir()
    (ws / "ramp").symlink_to(ramp_folder.parent, target_is_directory=True)
    settings = {
        "measurement_info": {"timestamp_start": "2026-09-24T12:00:00"},
        "ramp": {"folder": "ramp\\measurement_0"},   # backslash, as Windows writes it
    }
    with zipfile.ZipFile(ws / "measurement.zip", "w") as zf:
        zf.writestr("settings.json", json.dumps(settings))

    window.open_path(str(ws))
    assert window.measurement_combo.count() == 1
    assert window._channel is not None

    # the resolver itself, both separator styles
    from services.ramp_analysis import _recorded_ramp_folder

    assert _recorded_ramp_folder(str(ws), "ramp\\measurement_0") is not None
    assert _recorded_ramp_folder(str(ws), "ramp/measurement_0") is not None
    assert _recorded_ramp_folder(str(ws), "ramp\\missing") is None


def test_workspace_root_with_crash_leftovers_is_not_a_ramp_folder(
    window, tmp_path, ramp_folder, warnings_silenced
):
    """A crashed run leaves hidden .active_run.ramp.*.bin files in the
    workspace ROOT: they must not make open_path treat the workspace as
    a ramp folder."""
    ws = tmp_path / "campaign"
    ws.mkdir()
    (ws / ".active_run.ramp.ch1.bin").write_bytes(b"x")   # crash leftover
    window.open_path(str(ws))
    # still routed to the workspace scan (empty -> the loud warning path,
    # no channels loaded)
    assert window._channel is None
    assert warnings_silenced, "the empty scan must warn, not sit silent"


def test_open_errors_are_reported_not_raised(window, tmp_path, warnings_silenced):
    window.open_path(str(tmp_path / "nope"))
    assert "does not exist" in window.facts_label.text()

    empty_ws = tmp_path / "ws"
    empty_ws.mkdir()
    (empty_ws / "unrelated.zip").write_bytes(b"not a zip")
    window.open_path(str(empty_ws))
    assert "no measurement with ramp files" in warnings_silenced[0][2]
