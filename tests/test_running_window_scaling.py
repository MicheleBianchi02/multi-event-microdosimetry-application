"""
Qt tests for the running window's counts→volt/charge scaling (B6 + B23):

- the per-count factor is the EXACT inverse of the acquisition drivers'
  volts→counts conversion (zero-based counts over 0..2^bits−1 of the
  applied input range — e.g. the NI driver's
  counts = rint((V − Vmin) · (2^bits−1)/span));
- the applied input range (hardware config at Apply Configuration /
  hardware_settings.adc of a loaded archive) drives the "ADC Range" the
  window scales with — not the 0-5 V spinbox default that silently halved
  every volt/charge value on a ±5 V run;
- Voltage mode adds the range minimum to the ABSOLUTE start/stop curves
  (0 counts = Vmin on a bipolar range); differences stay offset-free;
- Voltage and Charge stay mutually consistent (V = Q / C).
"""

import os
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.session_model import SessionModel  # noqa: E402
from models.data_model import DataModel  # noqa: E402
from views.running_window import RunningWindow  # noqa: E402


@pytest.fixture()
def bench():
    session = SessionModel()
    data = DataModel()
    data.configure_adc(20)                     # the NI USB-6451
    win = RunningWindow(session, data, simulate=True)
    session.capacitance_pf_ch1 = 100.0
    session.capacitance_pf_ch2 = 100.0
    yield session, data, win
    win.close()


def test_applied_range_drives_the_scaling_context(bench):
    """B23: the session's applied range replaces the 5 V default — the
    span (10 V for the NI ±5 V range, minimum −5 V) feeds every
    counts→volt/charge conversion."""
    session, _data, win = bench
    assert win.setting_tab.adc_range == 5.0        # fallback default first
    assert win.setting_tab.adc_min_volts == 0.0

    session.adc_input_range = (-5.0, 5.0)
    assert win.setting_tab.adc_range == 10.0
    assert win.setting_tab.adc_min_volts == -5.0

    # setting the same range again must not fire (the signal is change-gated)
    session.adc_input_range = (-5.0, 5.0)


def test_full_scale_count_is_one_below_the_bin_count(bench):
    _session, _data, win = bench
    assert win.num_bins == 2**20
    assert win.counts_full_scale == 2**20 - 1   # the drivers' denominator


def test_voltage_round_trips_the_drivers_conversion(bench):
    """counts = rint((V − Vmin)·(2^20−1)/span) — the NI driver's exact
    formula — must display back as V on both the difference and the
    absolute start/stop curves."""
    session, data, win = bench
    session.adc_input_range = (-5.0, 5.0)
    win.setting_tab.type_box.setCurrentText("Voltage")
    session.debug_mode = True

    full, vmin, vmax = 2**20 - 1, -5.0, 5.0

    def to_counts(volts):
        return int(np.rint((volts - vmin) * full / (vmax - vmin)))

    start_v, stop_v = 0.0, 2.5                 # ramp from 0 V up
    c0, c1 = to_counts(start_v), to_counts(stop_v)
    data.start_data = np.array([[c0, c0]], float)
    data.stop_data = np.array([[c1, c1]], float)
    data.filtered_data = np.array([[c1 - c0, c1 - c0]], float)
    data.ptr = 1

    win.time_chart_type = "all"
    win.show_time()
    assert win.line_item_ch1.getData()[1][0] == pytest.approx(
        stop_v - start_v, abs=1e-4)            # difference: offset cancels
    assert win.start_line_item_ch1.getData()[1][0] == pytest.approx(
        start_v, abs=1e-4)                     # absolute: Vmin added back
    assert win.stop_line_item_ch1.getData()[1][0] == pytest.approx(
        stop_v, abs=1e-4)


def test_channel_mode_stays_raw_and_offset_free(bench):
    session, data, win = bench
    session.adc_input_range = (-5.0, 5.0)
    win.setting_tab.type_box.setCurrentText("Channel")
    session.debug_mode = True
    data.start_data = np.array([[524288, 524288]], float)
    data.stop_data = np.array([[524288 + 262144, 524288 + 262144]], float)
    data.filtered_data = np.array([[262144, 262144]], float)
    data.ptr = 1
    win.time_chart_type = "all"
    win.show_time()
    assert win.line_item_ch1.getData()[1][0] == pytest.approx(262144)
    assert win.start_line_item_ch1.getData()[1][0] == pytest.approx(524288)


def test_voltage_and_charge_factors_stay_consistent(bench):
    """V = Q / C across the unit selector: the same count scales to
    C·span/full pC and span/full V."""
    session, _data, win = bench
    session.adc_input_range = (-5.0, 5.0)
    full = 2**20 - 1

    win.setting_tab.type_box.setCurrentText("Voltage")
    v_factor = win._get_channel_roi_factor(1)
    assert v_factor == pytest.approx(10.0 / full)

    win.setting_tab.type_box.setCurrentText("Charge")
    q_factor = win._get_channel_roi_factor(1)
    assert q_factor == pytest.approx(100.0 * 10.0 / full)
    assert q_factor == pytest.approx(v_factor * session.capacitance_pf_ch1)
