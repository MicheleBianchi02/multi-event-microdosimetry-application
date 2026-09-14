import logging
import threading
import time
import serial

from hardware.interfaces import IDataAcquisition, ITimingController, IGainController, ActualTiming
from hardware.config.schema import ParameterBounds, RampSamplingConfig, TimingCapability
from core.constants import OVERFLOW_VALUE

logger = logging.getLogger(__name__)

SERIAL_TIMEOUT = 0.1
TOTAL_TIMEOUT = 1.5

class PicoSerialDAQ(IDataAcquisition, ITimingController, IGainController):
    """
    Hardware communication for the Raspberry Pi Pico 2.
    """

    # --- Hardcoded layer-1 limits (enforced on every call; the config's
    # --- declared envelope is validated against these at load time) ---
    DRIVER_CAPABILITIES = {"timing", "gain", "acquisition"}
    GAIN_MAX_PINS = 3  # the capacitor command carries 3 bits
    ADC_BITS = 12
    # Firmware constraint: the ramp-trigger mode and the acquisition loop
    # are mutually exclusive on this device (same uC, one ADC).
    SUPPORTS_RAMP_WHILE_ACQUIRING = False
    HARDWARE_LIMITS = TimingCapability(
        integration_time_us=ParameterBounds(1, 10_000_000, 1),
        reset_time_us=ParameterBounds(1, 10_000_000, 1),
        period_us=None,  # derived by the firmware: period = int + reset
        ramp_sampling=RampSamplingConfig(True, dt_us=ParameterBounds(10, 10_000, 10)),
    )

    def __init__(self, port: str = None, baud_rate: int = 115200):
        self._port = port
        self._baud_rate = baud_rate
        self.serial = serial.Serial()
        self.timeout = TOTAL_TIMEOUT
        self._lock = threading.Lock()
        
        self._stop_event = threading.Event()
        self._thread = None
        self._error = False

    def connect(self) -> bool:
        to_open = False
        if (
            self.serial.port != self._port
            or self.serial.baudrate != self._baud_rate
            or self.serial.timeout != SERIAL_TIMEOUT
        ):
            if self.serial.is_open:
                self.serial.close()

            self.serial.port = self._port
            self.serial.baudrate = self._baud_rate
            self.serial.timeout = SERIAL_TIMEOUT
            to_open = True
        else:
            if not self.serial.is_open:
                to_open = True

        try:
            if to_open and self._port is not None:
                self.serial.open()
            return self.serial.is_open
        except serial.SerialException as e:
            logger.warning("Connection error: %s", e)
            return False

    def disconnect(self) -> bool:
        if self.serial.is_open:
            self.serial.close()
        return True

    def set_connection_params(self, port: str, baud_rate: int) -> None:
        """Update the connection parameters used by the next connect()."""
        self._port = port
        self._baud_rate = baud_rate

    @property
    def is_connected(self) -> bool:
        return self.serial.is_open

    def set_event_mode(self, is_single_event: bool) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            if is_single_event:
                self.serial.write(b"RS\n")
            else:
                self.serial.write(b"RM\n")
        while response not in (b"D", b"E") and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Selecting event mode")

    def set_channels(self, double_ch: bool) -> bool:
        # Pico API does not mention channel configuration.
        logger.debug("set_channels called but not implemented for Pico. Ignoring.")
        return True

    def set_gpio_mask(self, gpio_mask: list[bool]) -> bool:
        if len(gpio_mask) != self.GAIN_MAX_PINS or not all(isinstance(b, bool) for b in gpio_mask):
            logger.error(
                "set_gpio_mask rejected: expected %d boolean pin states, got %r",
                self.GAIN_MAX_PINS, gpio_mask,
            )
            return False
        if not self.connect():
            return False

        state = "".join("1" if b else "0" for b in gpio_mask)
        cmd = f"C{state}\n".encode('ascii')
        
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(cmd)
        while response not in (b"D", b"E") and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Changing capacitors")

    @property
    def supports_ramp_sampling(self) -> bool:
        return self.HARDWARE_LIMITS.ramp_sampling.supported

    def get_adc_settings(self) -> dict:
        return {
            'bits': self.ADC_BITS,
            'vref': 5.0,
            'supports_oversampling': False
        }

    def _validate_timing_us(self, int_time_us: float, reset_time_us: float) -> bool:
        limits = self.HARDWARE_LIMITS
        ok = True
        if not limits.integration_time_us.contains_value(int_time_us):
            logger.error("integration time %s us out of hardware limits %s", int_time_us, limits.integration_time_us)
            ok = False
        if not limits.reset_time_us.contains_value(reset_time_us):
            logger.error("reset time %s us out of hardware limits %s", reset_time_us, limits.reset_time_us)
            ok = False
        return ok

    def calculate_actual_timing(
        self,
        int_time_us: float,
        reset_time_us: float,
        period_us: float = None,
        num_channels: int = None,
    ) -> ActualTiming:
        # The Pico takes timings in physical units directly (microseconds):
        # no quantization, and the channel count does not affect it.
        act_int = float(int_time_us)
        act_rst = float(reset_time_us)
        period = float(period_us) if period_us is not None else (act_int + act_rst)
        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=act_int,
            reset_time_us=act_rst,
            period_us=period,
            sample_frequency_hz=freq,
        )

    def calculate_actual_timing_us(
        self, int_time_us: float, reset_time_us: float, debug_mode: bool = False, multiple_channel: bool = True
    ) -> tuple[float, float]:
        # The Pico takes timings in physical units directly (microseconds)
        # So it does the math internally. We just return the same value.
        return float(int_time_us), float(reset_time_us)

    def configure_noramp(self, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        if not self.connect():
            return False
            
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"XN\n")
        time.sleep(0.01)
        
        cmd = f"T {int_time_us},{reset_time_us}\n".encode('ascii')
        response = bytes()
        operation_timeout = time.time() + self.timeout
        
        with self._lock:
            self.serial.write(cmd)
        while response not in (b"D", b"E") and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Configure noramp")

    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        dt_limits = self.HARDWARE_LIMITS.ramp_sampling.dt_us
        if dt_limits is not None and not dt_limits.contains_value(ramp_dt_us):
            logger.error("ramp dt %s us out of hardware limits %s", ramp_dt_us, dt_limits)
            return False
        if not self.connect():
            return False
            
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"XR\n")
        time.sleep(0.01)
        
        # Send timing configuration
        cmd = f"T {int_time_us},{reset_time_us}\n".encode('ascii')
        response = bytes()
        operation_timeout = time.time() + self.timeout
        
        with self._lock:
            self.serial.write(cmd)
        while response not in (b"D", b"E") and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
                
        if not self._check_response("D", response, "Configure ramp timing"):
            return False
            
        # Send ramp dt
        cmd_p = f"P {ramp_dt_us}\n".encode('ascii')
        response_p = bytes()
        operation_timeout = time.time() + self.timeout
        
        with self._lock:
            self.serial.write(cmd_p)
        while response_p not in (b"D", b"E") and time.time() < operation_timeout:
            with self._lock:
                response_p = self.serial.read(1)
        return self._check_response("D", response_p, "Configure ramp dt")

    def set_mode(self, debug_mode: bool) -> bool:
        # Pico does not have an explicit debug mode command.
        logger.debug("set_mode called but not implemented for Pico. Ignoring.")
        return True

    def arm(self) -> bool:
        # Pico does not require explicit arming.
        logger.debug("arm called but not implemented for Pico. Ignoring.")
        return True

    def start_measurement(self, session_model, data_model) -> bool:
        if not self.connect():
            return False
            
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"S\n")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
                
        if not self._check_response("D", response, "start"):
            return False

        self._stop_event.clear()
        self._error = False
        
        # Decide which thread to spawn based on ramp vs noramp
        # We can look for a flag in session_model or just use the noramp as default for now
        # You mentioned ramp would write to a memmap file and peek. For now we will implement 
        # the conventional ASCII reader for no-ramp.
        self._thread = threading.Thread(target=self._read_loop_binary, args=(session_model, data_model), daemon=True)
        self._thread.start()
        return True

    def stop_measurement(self) -> bool:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            
        if not self.connect():
            return False
            
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"E\n")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "stop")

    def _read_loop_binary(self, session, data_model):
        byte_buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                if self.serial.in_waiting:
                    with self._lock:
                        read_bytes = self.serial.read(self.serial.in_waiting)
                    if not read_bytes:
                        continue
                    byte_buffer.extend(read_bytes)
                    
                    while len(byte_buffer) >= 4:
                        chunk = byte_buffer[:4]
                        payload = int.from_bytes(chunk, byteorder="little")
                        start = payload >> 16
                        stop = payload & 0xFFFF
                        
                        if start > 4095 or stop > 4095:
                            # We lost 4-byte alignment! The ADC is 12-bit so values must be <= 4095.
                            # Drop 1 byte to shift the window and try to resynchronize.
                            del byte_buffer[0:1]
                            continue
                            
                        # Valid frame, remove the 4 bytes
                        del byte_buffer[:4]
                        
                        if session.debug_mode:
                            # Start, Stop for single channel
                            data_model.append_data(((start, stop), (OVERFLOW_VALUE, OVERFLOW_VALUE)))
                        else:
                            diff = stop - start
                            if stop < start:
                                diff = OVERFLOW_VALUE
                            data_model.append_data((diff, OVERFLOW_VALUE))
                else:
                    time.sleep(0.001)
            except Exception:
                logger.exception("Error in binary reader thread")
                self._error = True
                break

    def _check_response(self, expected_str: str, response: bytes, operation: str) -> bool:
        if not response:
            logger.warning("[%s] timed out.", operation)
            return False
        if response == expected_str.encode("ascii"):
            return True
        try:
            text = response.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = response
        logger.warning("[%s] failed. Expected %r but got %r.", operation, expected_str, text)
        return False
