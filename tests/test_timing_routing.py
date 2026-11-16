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

    transient_test_supported = True

    def configure_transient_reset_test(self, int_us, reset_us):
        self.calls.append(("transreset", int_us, reset_us))
        return True

    def calculate_transient_reset_timing(self, int_us, reset_us):
        from hardware.interfaces import ActualTiming

        if int_us < 1.0:
            raise ValueError("pulsed reset phase below the transient minimum")
        period = int_us + reset_us
        return ActualTiming(
            int_us, reset_us, period, 1e6 / period,
            samples_per_window=int(int_us), ramp_dt_eff_us=1.0,
        )

    def configure_transient_test(self, int_us, reset_us):
        self.calls.append(("transient", int_us, reset_us))
        return True

    def calculate_transient_timing(self, int_us, reset_us):
        from hardware.interfaces import ActualTiming

        if reset_us < 1.0:
            raise ValueError("reset below the transient test minimum")
        period = int_us + reset_us + 0.1
        return ActualTiming(
            int_us, reset_us, period, 1e6 / period,
            samples_per_window=int(int_us) + 1, ramp_dt_eff_us=1.0,
        )


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
    # one interval, 2 edges; the divider rounds one step up (dt strictly
    # > requested), so the interval spans dt_eff, not exactly 30 us
    assert actual.samples_per_window == 2
    assert actual.int_time_us == pytest.approx(actual.ramp_dt_eff_us)
    # reset quantizes to the dt/10 tick (dt_eff/10 at dt=30): 10 -> 3 ticks
    assert actual.reset_time_us == pytest.approx(3 * actual.ramp_dt_eff_us / 10)


def test_preview_rejection_is_raised_not_swallowed():
    """A mirror that rejects the request (e.g. markers mode with T_int
    beyond the deployed firmware's dt cap) must surface the reason: the
    manager used to fall back to the no-ramp numbers, showing values that
    can never be applied (generic failure at Apply time)."""

    class LimitedTiming(StubTiming):
        def calculate_ramp_timing(self, dt, int_us, reset_us):
            if dt > 4369.0:
                raise ValueError("ramp dt ... out of range")
            return super().calculate_ramp_timing(dt, int_us, reset_us)

    timing = LimitedTiming()
    manager = make_manager(timing, StubAcquisition())

    # markers mode with T_int = 100 ms -> dt = 100 ms -> rejected, raised
    with pytest.raises(ValueError, match="out of range"):
        manager.preview_timing(False, None, 100_000.0, 500.0)

    # ramp mode with the same dt: raised too
    with pytest.raises(ValueError):
        manager.preview_timing(True, 100_000.0, 100_000.0, 500.0)

    # and a representable markers request still previews normally
    actual, mode = manager.preview_timing(False, None, 100.0, 20.0)
    assert mode == "markers" and actual is not None


def test_time_window_failure_message_is_backward_compatible():
    """on_configuration_result gained an optional reason message; existing
    callers (Apply path) keep working without it."""
    import inspect

    from views.time_window import TimeWindow

    params = inspect.signature(TimeWindow.on_configuration_result).parameters
    assert "message" in params and params["message"].default is None


def test_ramp_request_on_ramp_incapable_timing_raises():
    """Ramp sampling requested from a timing device without ramp triggers
    (e.g. the PIC) must raise with the reason — not fall back to a
    no-ramp run whose data would mean something else than requested."""
    timing = StubTiming()
    timing.supports_ramp_sampling = False
    manager = make_manager(timing, StubAcquisition())
    with pytest.raises(ValueError, match="does not support ramp"):
        manager.timing_mode_for(True)
    with pytest.raises(ValueError, match="does not support ramp"):
        manager.configure_timing(True, 10.0, 100.0, 20.0)
    with pytest.raises(ValueError, match="does not support ramp"):
        manager.validate_timing(True, 10.0, 100.0, 20.0)
    with pytest.raises(ValueError, match="does not support ramp"):
        manager.preview_timing(True, 10.0, 100.0, 20.0)
    # without the request the same hardware routes to markers/internal as before
    assert manager.timing_mode_for(False) == "internal"


def test_preview_carries_the_applied_window_geometry():
    """The routed preview returns the driver mirror's window geometry —
    what the session (and through it the ramp sink) chunks on."""
    from hardware.pico.pico_daq import PicoSerialDAQ

    manager = make_manager(PicoSerialDAQ(), StubAcquisition())
    actual, mode = manager.preview_timing(True, 7.31, 100.0066, 50.0)
    assert mode == "ramp"
    assert actual.samples_per_window == 14
    assert actual.ramp_dt_eff_us == pytest.approx(28071 / 3840.0)  # round-up

    # markers (no ramp requested on split hardware): exactly 2 edges/window
    actual, mode = manager.preview_timing(False, None, 99.9934, 5.0)
    assert mode == "markers"
    assert actual.samples_per_window == 2


def test_transient_test_routes_to_the_test_generator():
    """The transient flag bypasses the normal generator selection: the
    driver's test mirror drives preview and configure, and the mode is
    'ramp' downstream (window geometry from the mirror verbatim)."""
    timing = StubTiming()
    manager = make_manager(timing, StubAcquisition())
    assert manager.transient_test_supported is True

    actual, mode = manager.preview_timing(
        True, 1.0, 100.0, 1.0, transient_test=True
    )
    assert mode == "ramp"
    assert actual.samples_per_window == 101

    assert manager.configure_timing(
        True, 1.0, 100.0, 1.0, transient_test=True
    ) is True
    assert timing.calls[-1] == ("transient", 100.0, 1.0)

    # validation runs through the test mirror too (raises with the reason)
    with pytest.raises(ValueError, match="transient test minimum"):
        manager.validate_timing(True, 1.0, 100.0, 0.5, transient_test=True)


def test_transient_test_flag_off_keeps_normal_routing():
    """Without the flag nothing changes: the ordinary generator selection
    applies (regression guard for the new kwarg)."""
    timing = StubTiming()
    manager = make_manager(timing, timing)  # all-in-one: no-ramp routes internal
    assert manager.configure_timing(False, 10.0, 100.0, 20.0) is True
    assert timing.calls == [("noramp", 100.0, 20.0)]


def test_transient_test_unsupported_driver_reports_false():
    class NoTestTiming(StubTiming):
        transient_test_supported = False

        def calculate_transient_timing(self, int_us, reset_us):
            raise NotImplementedError

        def configure_transient_test(self, int_us, reset_us):
            raise NotImplementedError

    manager = make_manager(NoTestTiming(), StubAcquisition())
    assert manager.transient_test_supported is False


def test_transient_reset_routes_swapped():
    """The reset-sampling flag routes preview/configure through the
    swapped mirror; the app's int field becomes the pulsed phase."""
    timing = StubTiming()
    manager = make_manager(timing, StubAcquisition())
    actual, mode = manager.preview_timing(True, 1.0, 3.0, 20000.0,
                                          transient_reset=True)
    assert mode == "ramp"
    assert actual.samples_per_window == 3
    assert manager.configure_timing(True, 1.0, 3.0, 20000.0,
                                    transient_reset=True) is True
    assert timing.calls[-1] == ("transreset", 3.0, 20000.0)
    with pytest.raises(ValueError, match="transient minimum"):
        manager.validate_timing(True, 1.0, 0.5, 20000.0, transient_reset=True)
