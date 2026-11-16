"""
Regression tests for the time chart's x-axis scale (review finding, 2026-10):
the axis used to re-derive the sample spacing as int + reset, which is only
exact in internal timing — in ramp/markers mode the applied period is
reset + settle + N·dt (markers: N = 2, dt = T_int; the PIO generator's
settle adds 2·dt more), so the displayed times drifted up to ~4x against
the archived data.csv timestamps (index / sample_freq).

The axis must use the applied sample period — SessionModel.sample_period_s
— exactly the basis the archived timestamps use.
"""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.data_model import DataModel  # noqa: E402
from models.session_model import SessionModel  # noqa: E402

# markers mode on split hardware: T_int = 100 us, reset = 5 us, software
# generator (settle 2 us): period = 5 us + 2 us + 2 x 100 us = 207 us,
# while int + reset = 105 us — the drift the user observed.
MARKERS_INT_S = 100e-6
MARKERS_RESET_S = 5e-6
MARKERS_PERIOD_S = 207e-6


def _markers_session() -> SessionModel:
    s = SessionModel()
    s.int_time = MARKERS_INT_S
    s.reset_time = MARKERS_RESET_S
    s.period_s = MARKERS_PERIOD_S
    s.sample_freq = 1.0 / MARKERS_PERIOD_S  # what the mirror reports
    s.timing_mode = "markers"
    return s


# ------------------------------------------------------ session property

def test_sample_period_is_the_applied_period_not_int_plus_reset():
    s = _markers_session()
    assert s.sample_period_s == pytest.approx(MARKERS_PERIOD_S)
    assert s.sample_period_s != pytest.approx(MARKERS_INT_S + MARKERS_RESET_S)


def test_sample_period_internal_mode_matches_int_plus_reset():
    s = SessionModel()
    s.int_time = 0.01
    s.reset_time = 0.001
    s.period_s = 0.011
    s.sample_freq = 1.0 / 0.011
    assert s.sample_period_s == pytest.approx(0.011)


def test_sample_period_fallbacks_for_legacy_sessions():
    # applied period known, frequency not (very old archives)
    s = SessionModel()
    s.sample_freq = 0.0
    s.int_time = 2.0
    s.reset_time = 1.0
    s.period_s = 3.5
    assert s.sample_period_s == pytest.approx(3.5)
    # neither: the classic int + reset of the pre-mirror era
    s.period_s = 0.0
    assert s.sample_period_s == pytest.approx(3.0)


# ------------------------------------------------------ the axis call site

def test_time_chart_axis_uses_the_applied_period():
    from views.running_window import RunningWindow

    session = _markers_session()
    view = RunningWindow(session, DataModel(), simulate=False)
    view.init_time_chart()
    scale = view.plot_widget.getAxis("bottom").scale
    assert scale == pytest.approx(MARKERS_PERIOD_S)
    assert scale != pytest.approx(MARKERS_INT_S + MARKERS_RESET_S)


def test_time_chart_axis_internal_mode_unchanged():
    from views.running_window import RunningWindow

    session = SessionModel()
    session.int_time = 0.01
    session.reset_time = 0.001
    session.period_s = 0.011
    session.sample_freq = 1.0 / 0.011
    view = RunningWindow(session, DataModel(), simulate=False)
    view.init_time_chart()
    assert view.plot_widget.getAxis("bottom").scale == pytest.approx(0.011)
