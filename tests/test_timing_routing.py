"""
Timing-mode routing (HardwareManager.timing_mode_for / configure_timing /
preview_timing): internal, ramp, and window markers (split hardware without
ramp sampling = degenerate ramp with dt = int, 2 pulses per window).
"""
from types import SimpleNamespace

import pytest

from helpers import all_pico_config
from hardware.core.hardware_manager import HardwareManager


class StubTiming:
    """Timing controller stub recording which configure call was routed."""

    supports_ramp_sampling = True

    def __init__(self):
        self.calls = []

    def configure_noramp(self, int_us, reset_us, period_us=None):
        self.calls.append(("noramp", int_us, reset_us))
        return True

    def configure_ramp(self, dt, int_us, reset_us, period_us=None):
        self.calls.append(("ramp", dt, int_us, reset_us))
        return True

    def calculate_ramp_timing(self, dt, int_us, reset_us):
        from hardware.interfaces import ActualTiming

        return ActualTiming(int_us, reset_us, dt + int_us + reset_us, 1e6 / (dt + int_us + reset_us))

    def calculate_actual_timing(self, int_us, reset_us, period_us=None, num_channels=None):
        from hardware.interfaces import ActualTiming

        return ActualTiming(int_us, reset_us, int_us + reset_us, 1e6 / (int_us + reset_us))


class StubAcquisition:
    """A distinct device holding the acquisition role (e.g. the NI)."""


def make_manager(timing, acquisition):
    manager = HardwareManager()
    manager.timing = timing
    manager.acquisition = acquisition
    return manager


def test_all_in_one_without_ramp_routes_internal():
    timing = StubTiming()
    manager = make_manager(timing, timing)  # same device holds both roles
    assert manager.timing_mode_for(False) == "internal"
    assert manager.configure_timing(False, 10.0, 100.0, 20.0) is True
    assert timing.calls == [("noramp", 100.0, 20.0)]


def test_all_in_one_with_ramp_routes_ramp():
    timing = StubTiming()
    manager = make_manager(timing, timing)
    assert manager.timing_mode_for(True) == "ramp"
    assert manager.configure_timing(True, 5.0, 100.0, 20.0) is True
    assert timing.calls == [("ramp", 5.0, 100.0, 20.0)]


def test_split_without_ramp_routes_markers_with_dt_equal_int():
    timing = StubTiming()
    manager = make_manager(timing, StubAcquisition())  # NI acquires
    assert manager.timing_mode_for(False) == "markers"
    assert manager.configure_timing(False, 10.0, 100.0, 20.0) is True
    # dt = int: the ramp generator degenerates to 2 pulses per window
    assert timing.calls == [("ramp", 100.0, 100.0, 20.0)]


def test_split_without_ramp_capability_falls_back_to_internal():
    timing = StubTiming()
    timing.supports_ramp_sampling = False  # e.g. the PIC
    manager = make_manager(timing, StubAcquisition())
    assert manager.timing_mode_for(False) == "internal"
    assert manager.configure_timing(False, 10.0, 100.0, 20.0) is True
    assert timing.calls == [("noramp", 100.0, 20.0)]


def test_preview_routing_matches_configure():
    timing = StubTiming()
    manager = make_manager(timing, StubAcquisition())
    # markers: preview comes from the ramp mirror with dt = int
    actual, mode = manager.preview_timing(False, None, 100.0, 20.0)
    assert mode == "markers"
    assert actual.int_time_us == pytest.approx(100.0)
    # ramp: preview uses the requested dt
    actual, mode = manager.preview_timing(True, 5.0, 100.0, 20.0)
    assert mode == "ramp"
    assert actual.int_time_us == pytest.approx(100.0)
    # internal (all-in-one): plain quantization preview — same device in
    # both roles
    shared = StubTiming()
    all_in_one = make_manager(shared, shared)
    actual, mode = all_in_one.preview_timing(False, None, 100.0, 20.0)
    assert mode == "internal"
    assert actual.int_time_us == pytest.approx(100.0)
    assert actual.reset_time_us == pytest.approx(20.0)


def test_marker_degenerate_ramp_gives_two_pulses():
    """dt = int must yield exactly 2 pulses (edges at 0 and int)."""
    from hardware.pico.pico_daq import PicoSerialDAQ

    d = PicoSerialDAQ()
    actual = d.calculate_ramp_timing(30.0, 30.0, 10.0)
    assert actual.int_time_us == pytest.approx(30.0)  # one interval, 2 edges
    # reset quantizes to the dt/10 tick (3 us at dt=30): 10 -> 9
    assert actual.reset_time_us == pytest.approx(9.0)
