from abc import ABC, abstractmethod

class IDataAcquisition(ABC):
    """
    Interface for data acquisition hardware (e.g., ADCs).
    """

    @abstractmethod
    def arm(self) -> bool:
        """Arm the device for measurement."""
        pass

    @abstractmethod
    def start_measurement(self, session_model, data_model) -> bool:
        """Start the measurement and data stream."""
        pass

    @abstractmethod
    def stop_measurement(self) -> bool:
        """Stop the measurement."""
        pass

    @abstractmethod
    def get_adc_settings(self) -> dict:
        """
        Return constraints and properties of the ADC.
        Example: {'bits': 12, 'vref': 5.0, 'supports_oversampling': False}
        """
        pass
