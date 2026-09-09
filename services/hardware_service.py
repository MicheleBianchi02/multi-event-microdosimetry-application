import logging
import threading
import time
import numpy as np

from core.constants import OVERFLOW_VALUE
from models.data_model import DataModel
from models.session_model import SessionModel
from hardware.core.hardware_manager import HardwareManager

logger = logging.getLogger(__name__)

class HardwareService:
    """
    Manages the hardware device and simulation logic.
    Delegates the actual serial communication and background threads
    to the specific HardwareManager's acquisition capability.
    """

    def __init__(
        self, hardware_manager: HardwareManager, data_model: DataModel, session_model: SessionModel
    ):
        self.hardware_manager = hardware_manager
        self.data_model = data_model
        self.session = session_model

        self._stop_event = threading.Event()
        self._stop_event.set()
        self._error = False
        self._thread = None

    def start_receiving(self, simulate: bool = False, rate: int = 5) -> bool:
        self._stop_event.clear()
        self._error = False
        
        if not simulate:
            if not self.hardware_manager.acquisition:
                logger.error("No acquisition hardware assigned")
                self._error = True
                return False
                
            success = self.hardware_manager.acquisition.start_measurement(self.session, self.data_model)
            if not success:
                self._error = True
                return False
        else:
            if self._thread is not None and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=2.0)
            self._thread = threading.Thread(
                target=self._simulate_data, args=(rate,), daemon=True
            )
            self._thread.start()
            
        return True

    def stop_receiving(self, send_stop: bool = True) -> bool:
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        if send_stop:
            if self.hardware_manager.acquisition:
                return self.hardware_manager.acquisition.stop_measurement()
            else:
                return False
        return True

    def has_error(self) -> bool:
        return self._error

    def clear_error(self):
        self._error = False

    def _simulate_data(self, rate: int):
        while not self._stop_event.is_set():
            start = time.perf_counter()
            start_value, stop_value, diff_value = self._generate_random_val()

            if not self.session.debug_mode:
                if self.session.multiple_channel:
                    self.data_model.append_data((diff_value[0], diff_value[1]))
                else:
                    self.data_model.append_data((diff_value[0], OVERFLOW_VALUE))
            else:
                if self.session.multiple_channel:
                    self.data_model.append_data(
                        (
                            (start_value[0], stop_value[0]),
                            (start_value[1], stop_value[1]),
                        )
                    )
                else:
                    self.data_model.append_data(
                        (
                            (start_value[0], stop_value[0]),
                            (OVERFLOW_VALUE, OVERFLOW_VALUE),
                        )
                    )

            tm_sleep = 10**-3 / rate
            elapsed = time.perf_counter() - start
            if tm_sleep - elapsed > 0:
                time.sleep(tm_sleep - elapsed)

    def _generate_random_val(self):
        start_value_ch1 = int(np.random.normal(loc=1000, scale=50))
        stop_value_ch1 = int(np.random.normal(loc=3700, scale=150))
        start_value_ch2 = int(np.random.normal(loc=300, scale=50))
        stop_value_ch2 = int(np.random.normal(loc=3500, scale=150))

        start_value = np.array([start_value_ch1, start_value_ch2])
        stop_value = np.array([stop_value_ch1, stop_value_ch2])
        diff_val = stop_value - start_value

        if stop_value_ch1 < start_value_ch1 or stop_value_ch1 >= 2**12 - 1:
            stop_value[0] = OVERFLOW_VALUE
            diff_val[0] = OVERFLOW_VALUE

        if stop_value_ch2 < start_value_ch2 or stop_value_ch2 >= 2**12 - 1:
            stop_value[1] = OVERFLOW_VALUE
            diff_val[1] = OVERFLOW_VALUE

        return start_value, stop_value, diff_val

class SimulatedDevice:
    """Placeholder to check if device is simulated when sending stop command."""
    pass
