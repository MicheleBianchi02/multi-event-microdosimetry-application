"""
Offscreen UI tests for the live ramp analysis mode:

- the timing window's "Live Ramp Analysis…" button gating (applied ramp
  mode only) and its signal,
- the live window's start/stop flow against a fake hardware service,
  selection changes restarting the averaging, the unit switch, reset,
  CSV export, and the acquisition guards,
- the measurement controller refusing to start while the window acquires.

The sink math is covered by test_live_ramp.py.
"""
import csv
import os
import sys
import time
import types

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.session_model import SessionModel  # noqa: E402
from models.data_model import DataModel  # noqa: E402
from services.hardware_service import HardwareService  # noqa: E402
from views.time_window import TimeWindow  # noqa: E402
from views.live_ramp_window import LiveRampWindow  # noqa: E402

N = 64
DT = 1e-6
VMIN, VMAX = -5.0, 5.0
FULL = 2 ** 20 - 1


def ramp_session():
    s = SessionModel()
    s.timing_mode = "ramp"
    s.int_time = (N - 1) * DT
    s.reset_time = 1e-4
    s.ramp_dt_s = DT
    s.samples_per_window = N
    s.adc_bits = 20
    s.adc_input_range = (VMIN, VMAX)
    return s


class FakeHardwareService:
    """Records what the window asks for; optionally pushes windows into the
    sink synchronously at start (like the simulator thread would)."""

    is_receiving = False

    def __init__(self, windows=0, fail_start=False):
        self.windows = windows
        self.fail_start = fail_start
        self.calls = []
        self._counter = 0          # driver-style absolute sample counter

    def start_receiving(self, simulate=False, rate=0, ramp_sink=None):
        self.calls.append(("start", simulate, ramp_sink))
        if self.fail_start:
            return False
        self._counter = 0          # the real driver restarts at 0 every run
        for k in range(self.windows):
            ramp_sink.write_block(0, np.full(N, 600000 + 1000 * k, dtype=np.uint32),
                                  self._counter)
            self._counter += N
        # partial window mid-collection, like a real stop mid-ramp
        ramp_sink.write_block(0, np.full(N // 3, 600000, dtype=np.uint32),
                              self._counter)
        self._counter += N // 3
        self.is_receiving = True
        return True

    def stop_receiving(self, send_stop=True):
        self.calls.append(("stop", send_stop))
        self.is_receiving = False
        return True


@pytest.fixture
def dialogs(monkeypatch):
    """Capture modal dialogs instead of blocking the offscreen tests
    (questions answer Yes so closeEvent proceeds)."""
    shown = []
    for name in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(
            f"views.live_ramp_window.QMessageBox.{name}",
            lambda *a, _n=name, **k: (shown.append((_n, a)) or
                                      16384),  # StandardButton.Yes
            raising=True)
    return shown


def make_window(service=None, session=None, is_running=lambda: False,
                simulate=True, hw_mgr=None):
    if service is None:
        service = FakeHardwareService()
    return LiveRampWindow(
        session or ramp_session(), service, hw_mgr,
        simulate=simulate, is_measurement_running=is_running)


def settle(timeout=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _app.processEvents()
        time.sleep(0.02)


def wait_windows(window, count, timeout=2.0):
    """Wait for the sink thread to absorb `count` windows on channel 0."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _app.processEvents()
        if window.sink.snapshot(0)["count"] >= count:
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------ timing window gate

@pytest.fixture
def time_window():
    return TimeWindow(ramp_session(), simulate=True, hardware_manager=None)


def test_live_button_gated_by_applied_ramp_mode(time_window):
    assert not time_window.live_ramp_bnt.isEnabled()

    time_window.on_configuration_result(True, "ok")
    assert time_window.live_ramp_bnt.isEnabled()

    time_window.on_edit_press()
    assert not time_window.live_ramp_bnt.isEnabled()


def test_live_button_disabled_in_markers_mode(time_window):
    time_window.session.timing_mode = "markers"
    time_window.on_configuration_result(True, "ok")
    assert not time_window.live_ramp_bnt.isEnabled()


def test_live_button_emits_signal(time_window):
    time_window.on_configuration_result(True, "ok")  # enable the button
    hits = []
    time_window.live_ramp_requested.connect(lambda: hits.append(1))
    time_window.live_ramp_bnt.click()
    assert hits == [1]


# ---------------------------------------------------------- window basics

def test_start_builds_sink_with_applied_geometry(dialogs):
    service = FakeHardwareService(windows=3)
    window = make_window(service)
    assert window.sink is None
    window._try_start()
    assert service.calls[0][0] == "start"
    assert window._receiving and window.sink is not None
    assert window.sink.n == N and window.sink.dt == DT
    assert window.sink.fft_span == (0, N)        # defaults: full window
    assert window.sink.cds_points == (0, N - 1)  # first/last sample
    assert wait_windows(window, 3)
    window._poll()
    snap = window.sink.snapshot(0)
    assert snap["count"] == 3
    assert window.preview_curve.xData is not None
    assert len(window.preview_curve.xData) > 0
    assert window.spectrum_curve.xData is not None
    assert "3 windows averaged" in window.spectrum_status.text()
    assert "windows: 3" in window.cds_stats_label.text()
    window._stop_receiving()
    assert not window._receiving
    window.close()


def test_stop_keeps_results_restart_continues(dialogs):
    service = FakeHardwareService(windows=2)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 2)
    window._stop_receiving()
    assert window.sink.snapshot(0)["count"] == 2  # kept, like Pause
    window._try_start()                            # same sink continues
    assert window.sink.snapshot(0)["count"] == 2
    service.windows = 1
    assert wait_windows(window, 3)
    window._poll()
    window.close()


def test_reset_button_clears_results(dialogs):
    service = FakeHardwareService(windows=2)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 2)
    assert window.sink.snapshot(0)["count"] == 2
    window._on_reset()
    snap = window.sink.snapshot(0)
    assert snap["count"] == 0 and snap["diff_v"].size == 0
    assert "no spectrum yet" in window.spectrum_status.text()
    window.close()


def test_start_refused_while_measurement_runs(dialogs):
    window = make_window(is_running=lambda: True)
    window._try_start()
    assert window.sink is None and not window._receiving
    assert dialogs and dialogs[0][0] == "warning"


def test_start_refused_when_service_busy(dialogs):
    service = FakeHardwareService()
    service.is_receiving = True
    window = make_window(service)
    window._try_start()
    assert window.sink is None
    assert dialogs and dialogs[0][0] == "warning"


def test_start_refused_for_virtual_acquisition(dialogs):
    hw_mgr = types.SimpleNamespace(acquisition_is_virtual=True, timing=object())
    window = make_window(simulate=False, hw_mgr=hw_mgr)
    window._try_start()
    assert window.sink is None
    assert dialogs and dialogs[0][0] == "information"


def test_failed_start_reports_and_resets_button(dialogs):
    service = FakeHardwareService(fail_start=True)
    window = make_window(service)
    window._try_start()
    assert not window._receiving
    assert window.start_btn.isEnabled() and window.start_btn.text() == "Start"
    assert dialogs and dialogs[0][0] == "critical"


def test_real_hardware_arms_then_starts(dialogs):
    armed = []
    hw_mgr = types.SimpleNamespace(
        acquisition_is_virtual=False,
        is_loaded=False,
        active_channels=[0],
        get_adc_settings=lambda: {},
        timing=object(),
        arm=lambda: armed.append(1) or True,
    )
    service = FakeHardwareService(windows=1)
    window = make_window(service, simulate=False, hw_mgr=hw_mgr)
    window._try_start()
    assert armed == [1]
    assert window._arm_timer is not None       # countdown running
    assert window.is_acquiring                 # arming counts as busy
    window._arm_deadline = time.time() - 1.0   # fast-forward the wait
    window._on_arm_tick()
    assert window._arm_timer is None
    assert window._receiving
    assert service.calls[0][1] is False        # simulate=False honored
    assert service.calls[0][2] is window.sink
    window.close()


# ------------------------------------------------------- selection & units

def test_selection_change_resets_and_moves_markers(dialogs):
    service = FakeHardwareService(windows=2)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 2)
    assert window.sink.snapshot(0)["count"] == 2

    window.fft_from_spin.setValue(10.0)   # µs -> sample 10
    window.fft_to_spin.setValue(50.0)     # µs -> inclusive end -> 51
    window.cds_from_spin.setValue(5.0)
    window.cds_to_spin.setValue(20.0)
    window._on_selection_edited()

    assert window.sink.fft_span == (10, 51)
    assert window.sink.cds_points == (5, 20)
    assert window.sink.snapshot(0)["count"] == 0  # averaging restarted
    region = window.fft_region.getRegion()
    assert region[0] == pytest.approx(10 * DT)
    assert region[1] == pytest.approx(51 * DT)
    window.close()


def test_default_selection_is_full_window(dialogs):
    service = FakeHardwareService(windows=1)
    window = make_window(service)
    window._try_start()
    # the "to" spin shows T_int = (N-1)*dt and resolves to the FULL span
    assert window.fft_to_spin.value() == pytest.approx((N - 1) * DT / 1e-6)
    assert window.sink.fft_span == (0, N)
    window.close()


def test_unit_switch_converts_without_resetting(dialogs):
    service = FakeHardwareService(windows=1)
    window = make_window(service)
    window._try_start()
    window.fft_from_spin.setValue(10.0)   # 10 µs
    window._on_selection_edited()
    window.unit_combo.setCurrentIndex(1)  # µs -> ms
    assert window.fft_from_spin.value() == pytest.approx(0.01)
    assert window.sink.fft_span == (10, N)   # selection untouched
    window.close()


def test_degenerate_segment_not_applied(dialogs):
    service = FakeHardwareService(windows=1)
    window = make_window(service)
    window._try_start()
    window.fft_from_spin.setValue(10.0)
    window.fft_to_spin.setValue(10.5)     # < 2 samples apart at 1 µs dt
    window._on_selection_edited()
    assert window.sink.fft_span == (0, N)  # unchanged
    # the spins fall back to the sink's state
    assert window.fft_from_spin.value() == pytest.approx(0.0)
    window.close()


# ----------------------------------------------------------------- export

def test_export_writes_spectrum_and_cds_csv(dialogs, monkeypatch, tmp_path):
    service = FakeHardwareService(windows=3)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 3)
    base = str(tmp_path / "campaign")
    monkeypatch.setattr(
        "views.live_ramp_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (base, ""))
    window._on_export()

    spec_path = f"{base}_ch1_spectrum.csv"
    cds_path = f"{base}_ch1_cds.csv"
    assert os.path.exists(spec_path) and os.path.exists(cds_path)

    spec_lines = open(spec_path).read().splitlines()
    assert any("# ramp_dt_s: 1e-06" in l for l in spec_lines)
    assert any("# windows_in_spectrum: 3" in l for l in spec_lines)
    assert spec_lines[-(N // 2 - 1)].split(",")[0]  # freq rows present
    rows = [l for l in spec_lines if not l.startswith("#")]
    assert rows[0] == "freq_hz,ms_v2,dbv"
    assert len(rows) - 1 == N // 2 - 1              # DC + Nyquist dropped

    cds_rows = list(csv.reader(open(cds_path)))
    header_i = next(i for i, r in enumerate(cds_rows) if r and r[0] == "window")
    data = cds_rows[header_i + 1:]
    assert len(data) == 3
    assert data[0][0] == "0" and data[2][0] == "2"
    # constant windows: diff exactly 0 V
    assert float(data[0][3]) == pytest.approx(0.0, abs=1e-9)
    window.close()


def test_export_with_no_data_is_a_no_op(dialogs, monkeypatch, tmp_path):
    window = make_window()
    monkeypatch.setattr(
        "views.live_ramp_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (str(tmp_path / "x"), ""))
    window._on_export()
    assert not os.path.exists(str(tmp_path / "x_ch1_spectrum.csv"))


# ------------------------------------------------------ controller guard

def test_measurement_refused_while_live_window_acquires(monkeypatch):
    from controllers.measurement_controller import MeasurementController
    from views.running_window import RunningWindow

    session = ramp_session()
    data = DataModel()
    running = RunningWindow(session, data, True, None)
    controller = MeasurementController(
        session, data,
        HardwareService(types.SimpleNamespace(acquisition=None), data, session),
        types.SimpleNamespace(running_view=running),
        simulate=True,
    )

    shown = []
    monkeypatch.setattr(
        "controllers.measurement_controller.QMessageBox.warning",
        lambda *a, **k: shown.append(a))
    monkeypatch.setattr(
        "controllers.measurement_controller.QMessageBox.critical",
        lambda *a, **k: shown.append(a))

    controller._live_window = types.SimpleNamespace(is_acquiring=True)
    controller.start_device()
    assert shown, "the guard must warn instead of starting"
    assert controller.is_running is False

    # and with the window idle the normal path proceeds past the guard
    # (it stops later at the arming rule, which is the documented behavior)
    controller._live_window = types.SimpleNamespace(is_acquiring=False)
    controller.start_device()
    assert controller.is_running is False  # not started: no arm yet
    assert len(shown) == 2                 # the arming rule, not the guard


def test_close_during_arm_countdown_cancels_start(dialogs):
    hw_mgr = types.SimpleNamespace(
        acquisition_is_virtual=False,
        is_loaded=False,
        active_channels=[0],
        get_adc_settings=lambda: {},
        timing=object(),
        arm=lambda: True,
    )
    service = FakeHardwareService(windows=1)
    window = make_window(service, simulate=False, hw_mgr=hw_mgr)
    window._try_start()
    assert window._arm_timer is not None
    window.close()
    assert window._arm_timer is None
    assert not window._receiving
    assert service.calls == []          # nothing was ever started


# ------------------------------------------------------- stall diagnostics

def test_stall_no_samples_warns(dialogs, caplog):
    service = FakeHardwareService(windows=0)   # started, but nothing flows
    window = make_window(service)
    window._try_start()
    window._poll()   # consume the first progress-key transition
    with caplog.at_level("WARNING", logger="views.live_ramp_window"):
        window._last_progress_t = time.monotonic() - 30  # force the stall
        window._poll()
    assert any("no samples received" in r.message for r in caplog.records)
    assert "no samples received" in window.statusBar().currentMessage()
    window.close()


def test_stall_samples_but_no_window_warns(dialogs, caplog):
    service = FakeHardwareService(windows=0)
    window = make_window(service)
    window._try_start()
    # partial window only: samples arrive but never complete one
    window.sink.write_block(0, np.full(N // 2, 10, dtype=np.uint32))
    wait_windows(window, 1)  # drain the queue (count stays 0)
    window._poll()           # consume the first progress-key transition
    with caplog.at_level("WARNING", logger="views.live_ramp_window"):
        window._last_progress_t = time.monotonic() - 30
        window._poll()
    assert any("no complete window" in r.message for r in caplog.records)
    window.close()


def test_sink_error_surfaced_once(dialogs, caplog):
    service = FakeHardwareService(windows=1)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 1)
    window.sink._error = "synthetic sink failure"
    with caplog.at_level("ERROR", logger="views.live_ramp_window"):
        window._poll()
        window._poll()   # second poll must NOT re-open the dialog
    assert sum("live ramp sink error" in r.message for r in caplog.records) == 1
    assert any(c[0] == "critical" for c in dialogs)
    window.close()


def test_window_heartbeat_logged_every_25_windows(dialogs, caplog):
    service = FakeHardwareService(windows=30)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 30, timeout=3)
    with caplog.at_level("INFO", logger="views.live_ramp_window"):
        window._poll()
    assert any("windows averaged" in r.message for r in caplog.records)
    window.close()


def test_closed_window_polls_again_when_reused(dialogs):
    """Regression: closeEvent stopped the poll timer and a reused window
    never restarted it — data flowed into the sink, nothing reached the
    screen and no diagnostics ran (the 'silent restart' bug)."""
    service = FakeHardwareService(windows=2)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 2)
    window.close()
    assert not window._poll_timer.isActive()
    window.show()                        # belt and braces: reuse must revive
    assert window._poll_timer.isActive()
    window.close()


def test_controller_discards_closed_window():
    """Closing the window resets the analysis: the controller drops it and
    the next open builds a FRESH window (no leftover ramps/results)."""
    from controllers.measurement_controller import MeasurementController
    from PyQt6.QtWidgets import QWidget
    from views.running_window import RunningWindow

    session = ramp_session()
    data = DataModel()
    main = QWidget()
    main.running_view = RunningWindow(session, data, True, None)
    controller = MeasurementController(
        session, data,
        HardwareService(types.SimpleNamespace(acquisition=None), data, session),
        main, simulate=True,
    )
    controller.open_live_ramp()
    first = controller._live_window
    assert first is not None
    first.close()
    _app.processEvents()                 # let deleteLater run
    assert controller._live_window is None
    controller.open_live_ramp()
    assert controller._live_window is not None
    assert controller._live_window is not first
    assert controller._live_window.sink is None     # fresh: no results yet
    controller._live_window.close()


# ------------------------------------------------------- high-pass filter

def test_highpass_controls_reach_sink_and_reset(dialogs):
    service = FakeHardwareService(windows=2)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 2)
    assert window.sink.highpass == (False, 0.0)

    window.hp_check.setChecked(True)          # default fc = 100 Hz
    assert window.sink.highpass == (True, 100.0)
    assert window.sink.snapshot(0)["count"] == 0     # averaging restarted
    assert "high-pass changed" in window.statusBar().currentMessage()

    window.hp_fc_spin.setValue(250.0)
    window._on_hp_changed()                   # editingFinished is a signal
    assert window.sink.highpass == (True, 250.0)
    assert window.sink.snapshot(0)["count"] == 0

    assert "HP 250 Hz" in window.spectrum_status.text()
    window.hp_check.setChecked(False)
    assert window.sink.highpass == (False, 250.0)    # fc remembered
    assert "HP off" in window.spectrum_status.text()
    window.close()


def test_highpass_remembered_before_first_start(dialogs):
    window = make_window()                     # no sink yet
    window.hp_check.setChecked(True)
    window.hp_fc_spin.setValue(500.0)
    window._on_hp_changed()                   # commit the spin value
    assert window.sink is None                 # only remembered so far
    service = window.hardware
    service.windows = 1
    window._try_start()
    assert window.sink.highpass == (True, 500.0)     # applied at build
    window.close()


def test_export_header_records_highpass(dialogs, monkeypatch, tmp_path):
    service = FakeHardwareService(windows=1)
    window = make_window(service)
    window._try_start()
    assert wait_windows(window, 1)
    window.hp_check.setChecked(True)          # resets the averaging...
    window.sink.write_block(0, np.full(N, 600000, dtype=np.uint32))
    assert wait_windows(window, 1)            # ...so feed one window after
    base = str(tmp_path / "hp")
    monkeypatch.setattr(
        "views.live_ramp_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (base, ""))
    window._on_export()
    header = open(f"{base}_ch1_spectrum.csv").read().splitlines()
    assert any("# highpass_hz: 100.0" in l for l in header)
    assert any("Butterworth" in l for l in header)
    window.close()
