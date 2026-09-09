"""
Virtual acquisition device: fills the `acquisition` role with a no-op driver
so that a configuration validates (and a test measurement can run) while the
real timing/gain interfaces are verified on the bench — e.g. checking the
generated integration time with an oscilloscope.  No data is produced; the
UI notifies the user that the acquisition is virtual.
"""
import logging

from hardware.interfaces.acquisition import IDataAcquisition

logger = logging.getLogger(__name__)


class DummyAcquisition(IDataAcquisition):
    """
    Virtual device for bench tests.  All commands succeed; start_measurement
    succeeds without producing any sample.  Its ADC properties come from the
    hardware config (the registry imposes no constraints on this driver).
    """

    DRIVER_CAPABILITIES = {"acquisition"}  # acquisition only: no dummy timing/gain
    IS_VIRTUAL = True

    def __init__(self, adc_capability=None):
        self._adc = adc_capability

    def connect(self) -> bool:
        return True

    def disconnect(self) -> bool:
        return True

    def arm(self) -> bool:
        return True

    def start_measurement(self, session_model, data_model) -> bool:
        logger.info(
            "Dummy acquisition started: no data will be collected "
            "(virtual device, timing/gain bench test)."
        )
        return True

    def stop_measurement(self) -> bool:
        return True

    def get_adc_settings(self) -> dict:
        if self._adc is None:
            return {}
        return {
            "bits": self._adc.adc.bits,
            "num_channels": None,  # filled by the manager from the config
            "input_range_volts": {
                "min_volts": self._adc.adc.effective_range().min_volts,
                "max_volts": self._adc.adc.effective_range().max_volts,
            },
            "supports_oversampling": self._adc.adc.supports_oversampling,
            "virtual": True,
        }
