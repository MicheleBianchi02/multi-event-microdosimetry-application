from abc import ABC, abstractmethod


class IDataAcquisition(ABC):
    """
    Interface for data acquisition hardware (e.g., ADCs).
    """

    def arm(self) -> bool:
        """
        Optional / legacy arm hook for the acquisition device.
        Primary arming is owned by ITimingController.
        """
        return True

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

    def set_input_range(self, min_volts: float, max_volts: float) -> bool:
        """
        Configure the active voltage span for the ADC.
        Returns True if successful or if range is fixed.
        """
        return True
