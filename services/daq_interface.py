import queue
from abc import ABC, abstractmethod

class DAQInterface(ABC):
    """
    Abstract Base Class for Data Acquisition Hardware.
    """
    def __init__(self):
        self.is_measuring = False

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection with the device."""
        pass

    @abstractmethod
    def disconnect(self) -> bool:
        """Close connection with the device."""
        pass

    @abstractmethod
    def verify_connection(self) -> bool:
        """Verify the connection is alive."""
        pass

    @abstractmethod
    def set_event_mode(self, is_single_event: bool) -> bool:
        """Set operation mode to single-event or multi-event (integration)."""
        pass

    @abstractmethod
    def set_channels(self, double_ch: bool) -> bool:
        """Configure the device for single or double channel mode."""
        pass

    @abstractmethod
    def set_capacitors(self, cap0: bool, cap1: bool = False, cap2: bool = False) -> bool:
        """
        Set integration capacitors.
        For PIC, only cap0 is used (as gain increase).
        For Pico, cap0, cap1, cap2 map to 3 bits.
        """
        pass

    @abstractmethod
    def calculate_actual_timing(self, int_time_s: float, reset_time_s: float, debug_mode: bool = False, multiple_channel: bool = True) -> tuple[float, float]:
        """
        Returns the actual quantized integration and reset times (in seconds) the hardware will use.
        """
        pass

    @abstractmethod
    def configure_noramp(self, int_time_us: float, reset_time_us: float) -> bool:
        """Configure integration and reset timing for conventional (no-ramp) measurement."""
        pass

    @abstractmethod
    def configure_ramp(self, ramp_dt_us: float) -> bool:
        """Configure ramp mode timing. If not supported, ignore gracefully."""
        pass
        
    @abstractmethod
    def set_mode(self, debug_mode: bool) -> bool:
        """Configure debug or production mode. Can do nothing if unsupported."""
        pass
        
    @abstractmethod
    def arm(self) -> bool:
        """Arm the device. Can do nothing if unsupported."""
        pass

    @abstractmethod
    def start_measurement(self, session_model, data_model) -> bool:
        """
        Start continuous measurement.
        The driver should spawn its own thread here, formatting data according to
        session_model and appending it directly to data_model.
        """
        pass

    @abstractmethod
    def stop_measurement(self) -> bool:
        """Stop the continuous measurement and join any background threads."""
        pass
