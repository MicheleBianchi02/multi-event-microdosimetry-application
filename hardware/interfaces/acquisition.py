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
    def start_measurement(self, session_model, data_model, ramp_sink=None) -> bool:
        """Start the measurement and data stream.

        `ramp_sink` (see hardware/interfaces/ramp_sink.py) is non-None only
        when the timing runs in ramp/markers mode: the driver then pushes
        blocks of normalized uint32 samples (raw ADC counts) to it from its
        reader thread, instead of — or in addition to — whatever its native
        stream produces.  Drivers that cannot stream ramp data should refuse
        to start (return False) when a sink is provided they cannot feed.
        """
        pass

    def configure_channels(self, channel_plan) -> bool:
        """Tell the acquisition driver which channels it must measure and
        how each signal reaches the device.  `channel_plan` is a list of
        (channel_id, termination) pairs — e.g. [("ch1", "differential"),
        ("ch2", "rse")] — derived from the loaded hardware config by the
        HardwareManager.  Drivers that don't care (µC firmware with fixed
        inputs) keep this no-op; instrument drivers map the pairs onto
        their physical input terminals."""
        return True

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
