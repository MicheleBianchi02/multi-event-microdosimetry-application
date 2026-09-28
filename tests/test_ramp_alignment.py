"""
Regression tests for the ramp-sampling alignment chain (bugs B35/B36 in
BUG.md): the timing driver's exact mirror is the SINGLE source of the
window geometry — Apply feeds the mirror the same TYPED values the
preview used (a second quantization pass loses up to one integration
interval on the PIO generator), the session carries the applied geometry
(``samples_per_window`` / the effective dt), and the ramp sink chunks on
exactly that number instead of re-deriving it as round(int/dt)+1 (the
lossy re-derivation drifts the window grid against the hardware pulse
train).

The tests drive the real wizard/controller/manager stack offscreen with a
split configuration (Pico timing, NI acquisition) shipped in examples/ — no hardware: the Pico mirror is pure
computation and the NI driver only validates its channel plan.
"""
import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# the shipped split configuration: Pico 2 (timing + gains) + NI USB-6451
# (acquisition) with two differential channels — the setup the whole ramp
# path is built for (examples/hardware_config_pico_ni.json)
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "hardware_config_pico_ni.json",
)

_app = QApplication.instance() or QApplication([])

# every popup is stubbed before anything can block the offscreen run
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.critical = staticmethod(lambda *a, **k: None)


@pytest.fixture
def app_controller(tmp_path):
    """The full app stack (wizard + controllers + manager) with the split
    Pico/NI configuration loaded — the ramp path's real wiring."""
    from app import AppController

    controller = AppController(simulate=False, baud_rate=115200,
                               single_channel=False)
    controller.hardware_manager.load_config(EXAMPLE)
    return controller


def _wizard(controller):
    return controller.setup_controller.time_window


def _show_wizard(tw):
    """Make the timing page actually visible: the preview only reads the
    ramp checkbox when it is on screen (the wizard hides it for
    ramp-incapable timing devices)."""
    mw = tw.window()
    mw.show()
    mw.windows_stack.setCurrentIndex(0)
    mw.setup_view.stack_view.setCurrentIndex(2)  # the run-config page
    tw.stacked_widget.setCurrentIndex(1)         # the manual timing page


def _type_timing(tw, int_us_text, rst_us_text, dt_us_text=None):
    """Set the wizard fields (µs magnitude) and run the preview."""
    _show_wizard(tw)
    tw.magnitude_int.setCurrentIndex(2)   # µs
    tw.magnitude_rst.setCurrentIndex(2)   # µs
    tw.int_time.setText(int_us_text)
    tw.rst_time.setText(rst_us_text)
    if dt_us_text is None:
        tw.sample_ramp_cb.setChecked(False)
    else:
        tw.sample_ramp_cb.setChecked(True)
        tw.ramp_dt_input.setText(dt_us_text)
    tw.on_manual_edit()  # -> preview -> session refresh


# ---------------------------------------------------------------------------
# Bug 1: Apply must send the TYPED values (the preview's mirror inputs)
# ---------------------------------------------------------------------------

def test_apply_sends_the_typed_values_not_the_previewed_session(app_controller):
    """The wizard preview quantizes int 100.0066 µs with dt 7.31 µs down to
    ~95.03 µs; Apply must still send 100.0066/7.31 — the SAME mirror
    inputs the preview used.  Re-sending the session's quantized values
    made the mirror floor a second time: the hardware then ran one
    integration interval less than the wizard had reported."""
    tw = _wizard(app_controller)
    # select gains so Apply is enabled (both channels of the split config)
    if tw.capacitance_combo_ch1.count():
        tw.capacitance_combo_ch1.setCurrentIndex(0)
    if tw.capacitance_combo_ch2.count():
        tw.capacitance_combo_ch2.setCurrentIndex(0)

    _type_timing(tw, "100.0066", "50", dt_us_text="7.31")

    captured = []
    tw.apply_configuration_requested.connect(lambda cfg: captured.append(cfg))
    tw.on_send_press()
    assert captured, "Apply must emit the configuration"
    assert captured[0]["int_time_us"] == pytest.approx(100.0066)
    assert captured[0]["reset_time_us"] == pytest.approx(50.0)
    assert captured[0]["ramp_dt_us"] == pytest.approx(7.31)


# ---------------------------------------------------------------------------
# Bug 2: the session carries the applied window geometry from the mirror
# ---------------------------------------------------------------------------

def test_preview_threads_the_applied_geometry_into_the_session(app_controller):
    from hardware.pico.pico_daq import PicoSerialDAQ

    tw = _wizard(app_controller)
    _type_timing(tw, "100.0066", "50", dt_us_text="7.31")

    session = app_controller.session_model
    mirror = PicoSerialDAQ().calculate_ramp_timing(7.31, 100.0066, 50.0)

    assert session.timing_mode == "ramp"
    assert session.int_time == pytest.approx(mirror.int_time_us / 1e6)
    assert session.samples_per_window == mirror.samples_per_window
    # the APPLIED (quantized) dt, not the requested 7.31 µs
    assert session.ramp_dt_s == pytest.approx(mirror.ramp_dt_eff_us / 1e6)


def test_long_integration_drift_case_matches_the_hardware_pulse_count(app_controller):
    """The sharp drift case: dt = 7.31 µs with T_int = 10 s.  The lossy
    round(int/dt)+1 re-derivation lands 18 samples per window below the
    hardware's pulse count; the mirror's geometry must be exact."""
    from hardware.pico.pico_daq import PicoSerialDAQ

    tw = _wizard(app_controller)
    _show_wizard(tw)
    tw.magnitude_int.setCurrentIndex(0)   # s
    tw.magnitude_rst.setCurrentIndex(2)   # µs
    tw.int_time.setText("10")
    tw.rst_time.setText("1000")
    tw.sample_ramp_cb.setChecked(True)
    tw.ramp_dt_input.setText("7.31")
    tw.on_manual_edit()

    mirror = PicoSerialDAQ().calculate_ramp_timing(7.31, 10_000_000.0, 1000.0)
    session = app_controller.session_model
    assert session.samples_per_window == mirror.samples_per_window
    # pin the exact number: the old derivation gave 1_367_972
    assert session.samples_per_window == 1_367_990

    # and the ramp sink chunks on exactly that geometry (ramp mode: temp
    # .bin files inside the workspace)
    session.active_workspace_dir = None  # system temp for the .bin files
    mc = app_controller.measurement_controller
    try:
        sink = mc._start_ramp_storage()
        assert sink._extractor.n == mirror.samples_per_window
        assert sink.metas[0].samples_per_window == mirror.samples_per_window
        # the header records the APPLIED dt (dt_eff), not the requested one
        assert sink.metas[0].ramp_dt_s == pytest.approx(mirror.ramp_dt_eff_us / 1e6)
    finally:
        mc._ramp_storage = sink
        mc._stop_ramp_storage(clean=False)


def test_markers_preview_sets_two_samples_per_window(app_controller):
    """Markers mode (ramp unchecked on split hardware): non-grid T_int on
    the PIO range must be representable (the firmware clamps the floored
    zero interval to one) and yield exactly the two boundary edges."""
    from hardware.pico.pico_daq import PicoSerialDAQ

    tw = _wizard(app_controller)
    _type_timing(tw, "99.9934", "5")   # no ramp -> markers on split hardware

    session = app_controller.session_model
    assert session.timing_mode == "markers"
    assert session.samples_per_window == 2
    assert session.ramp_dt_s == 0.0
    # the mirror agrees and does not raise on the non-grid dt = int
    mirror = PicoSerialDAQ().calculate_ramp_timing(99.9934, 99.9934, 5.0)
    assert mirror.samples_per_window == 2


def test_markers_sink_chunks_on_the_session_geometry(app_controller):
    tw = _wizard(app_controller)
    _type_timing(tw, "99.9934", "5")
    mc = app_controller.measurement_controller
    sink = mc._start_ramp_storage()
    try:
        assert sink._extractor.n == 2
    finally:
        mc._ramp_storage = sink
        mc._stop_ramp_storage(clean=False)


# ---------------------------------------------------------------------------
# Bug 1 (controller side): the session is refreshed from the same mirror
# after a successful Apply — guaranteed even if the preview never ran
# ---------------------------------------------------------------------------

def test_session_refresh_helper_maps_the_geometry():
    from controllers.setup_controller import SetupController
    from hardware.interfaces import ActualTiming

    session = types.SimpleNamespace(
        timing_mode="internal", int_time=1.0, reset_time=1.0,
        period_s=0.0, sample_freq=1.0, ramp_dt_s=0.0, samples_per_window=0,
    )
    fake_self = types.SimpleNamespace(session=session)
    actual = ActualTiming(
        int_time_us=95.028645833, reset_time_us=49.7072916,
        period_us=166.665625, sample_frequency_hz=1e6 / 166.665625,
        samples_per_window=14, ramp_dt_eff_us=7.309895833,
    )
    SetupController._refresh_session_timing(fake_self, actual, "ramp")
    assert session.timing_mode == "ramp"
    assert session.samples_per_window == 14
    assert session.ramp_dt_s == pytest.approx(7.309895833e-6)

    SetupController._refresh_session_timing(fake_self, actual, "internal")
    assert session.timing_mode == "internal"
    assert session.samples_per_window == 0
    assert session.ramp_dt_s == 0.0
