from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ActualTiming:
    """
    Quantized timing parameters returned by the timing hardware.
    All times are in microseconds; sample frequency is in Hertz.

    Supports tuple unpacking `(int_time_us, reset_time_us)` for backward compatibility.

    Ramp-specific geometry (None outside ramp/markers timing): the values
    below are the *applied* window geometry computed by the driver's exact
    mirror — the single source of truth the whole ramp path consumes (the
    session, the ramp sink's window size, the AQR1 header, the NI reader's
    expected rate).  They must never be re-derived downstream by dividing
    the quantized integration time by the requested dt: the division is
    lossy and the drift silently misaligns the window grid.

    ``samples_per_window`` is the number of CONV_START edges (samples) per
    integration window the generator will emit — N = int/dt + 1 evaluated
    with the applied quantization; ``ramp_dt_eff_us`` is the effective
    pulse spacing actually generated (dt as quantized by the generator).
    """

    int_time_us: float
    reset_time_us: float
    period_us: float
    sample_frequency_hz: float
    samples_per_window: Optional[int] = None
    ramp_dt_eff_us: Optional[float] = None

    def __iter__(self):
        return iter((self.int_time_us, self.reset_time_us))

    def __getitem__(self, index):
        return (self.int_time_us, self.reset_time_us)[index]


class ITimingController(ABC):
    """
    Interface for hardware that controls the timing of the experiment.
    Handles integration times, reset times, period, arming, channel count,
    mode, and ramp sampling configuration.

    All times are microseconds. Concrete implementations must validate
    every argument against their hardcoded limits (HARDWARE_LIMITS) and
    fail loudly — an out-of-bounds value must never reach the hardware.
    The declared envelope in hardware_config.json (validated at load time
    to lie within the hardcoded limits) drives the UI; the relationship
    between integration, reset and period (e.g. period = int + reset, or
    a fixed reset) is firmware behaviour and lives in the driver.
    """

    @property
    @abstractmethod
    def supports_ramp_sampling(self) -> bool:
        """Indicates if this timing controller can generate ramp sampling triggers."""
        pass

    @abstractmethod
    def arm(self) -> bool:
        """
        Arm the timing controller / integrator for measurement.
        Acts as an integrator reset before starting acquisition.
        """
        pass

    def start_timing(self) -> bool:
        """
        Start the timing engine WITHOUT the acquisition stream.

        Needed when the device holding the timing role does not also hold
        the acquisition role (e.g. a virtual acquisition on the bench, or a
        split setup): the measurement start button must still run the real
        timing signals, only nothing is recorded.  Devices whose start is
        owned by their acquisition half (start_measurement sends it) leave
        this as a no-op — the HardwareManager never calls it in that case.
        """
        return True

    def stop_timing(self) -> bool:
        """
        Stop the timing engine started by start_timing().  Default no-op,
        symmetric to start_timing().
        """
        return True

    @abstractmethod
    def set_channels(self, multiple_channel: bool) -> bool:
        """
        Inform the timing controller whether 1 or 2 channels are active.
        """
        pass

    @abstractmethod
    def set_mode(self, debug_mode: bool) -> bool:
        """
        Set operational mode (debug vs operational). The app operates in debug mode.
        """
        pass

    @abstractmethod
    def configure_noramp(self, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        """
        Configure the timing controller for standard Start/Stop measurements.
        period_us is None on devices where the period is derived by the firmware.
        """
        pass

    @abstractmethod
    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        """
        Configure the timing controller to generate ramp sampling triggers at
        interval dt. period_us is None on devices where the period is derived.
        """
        pass

    @abstractmethod
    def calculate_actual_timing(
        self,
        int_time_us: float,
        reset_time_us: float,
        period_us: Optional[float] = None,
        num_channels: Optional[int] = None,
    ) -> ActualTiming:
        """
        Return the actual (quantized) integration, reset, period, and sample frequency
        the hardware will use for a target set of times.

        This must be pure computation (no serial traffic): the UI calls it on
        every input change.  num_channels is the active channel count of the
        loaded hardware config; firmware whose quantization depends on it
        (e.g. the PIC transmission delay) must honour it.  When None, the
        driver falls back to its device-side state (see set_channels).
        """
        pass

    def calculate_actual_timing_us(
        self, int_time_us: float, reset_time_us: float, debug_mode: bool = False, multiple_channel: bool = True
    ) -> tuple[float, float]:
        """
        Legacy method returning (actual_int_us, actual_rst_us).
        Default implementation delegates to calculate_actual_timing; the
        debug_mode argument is ignored (the app always acquires in debug mode).
        """
        actual = self.calculate_actual_timing(
            int_time_us, reset_time_us, num_channels=2 if multiple_channel else 1
        )
        return actual.int_time_us, actual.reset_time_us
