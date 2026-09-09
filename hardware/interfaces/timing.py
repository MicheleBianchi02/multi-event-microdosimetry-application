from abc import ABC, abstractmethod


class ITimingController(ABC):
    """
    Interface for hardware that controls the timing of the experiment.
    Handles integration times, reset times, and ramp sampling configuration.

    All times are microseconds.  Concrete implementations must validate
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
        interval dt.  period_us is None on devices where the period is derived.
        """
        pass

    @abstractmethod
    def calculate_actual_timing_us(
        self, int_time_us: float, reset_time_us: float, debug_mode: bool = False, multiple_channel: bool = True
    ) -> tuple[float, float]:
        """
        Return the actual (quantized) integration and reset times the hardware
        will use for a target pair of times, in microseconds.
        """
        pass
