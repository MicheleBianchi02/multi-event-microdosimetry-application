from abc import ABC, abstractmethod

class ITimingController(ABC):
    """
    Interface for hardware that controls the timing of the experiment.
    Handles integration times, reset times, and ramp sampling configuration.
    """

    @property
    @abstractmethod
    def supports_ramp_sampling(self) -> bool:
        """Indicates if this timing controller can generate ramp sampling triggers."""
        pass

    @abstractmethod
    def configure_noramp(self, int_time_us: float, reset_time_us: float) -> bool:
        """
        Configure the timing controller for standard Start/Stop measurements.
        """
        pass

    @abstractmethod
    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float) -> bool:
        """
        Configure the timing controller to generate ramp sampling triggers at interval dt.
        """
        pass

    @abstractmethod
    def calculate_actual_timing(self, int_time_s: float, reset_time_s: float, debug_mode: bool = False, multiple_channel: bool = True) -> tuple[float, float]:
        """
        Calculate and return the actual integration and reset times the hardware will use.
        """
        pass

    @abstractmethod
    def get_timing_limits(self) -> dict:
        """
        Return the physical limits of the timing hardware.
        Example: {'min_int_us': 1, 'max_int_us': 1000000, 'min_dt_us': 10}
        """
        pass
