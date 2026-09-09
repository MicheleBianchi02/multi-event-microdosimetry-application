import logging
import threading
import time
import serial
import serial.tools.list_ports
import numpy as np

from hardware.interfaces import IDataAcquisition, ITimingController, IGainController
from hardware.config.schema import ParameterBounds, RampSamplingConfig, TimingCapability
from core.constants import OVERFLOW_VALUE

logger = logging.getLogger(__name__)

SERIAL_TIMEOUT = 0.1
TOTAL_TIMEOUT = 1.5

# PIC Specific Constants
OSC_FREQ = 2**15  # 32768 Hz
TMREG = 2**16  # timerH * timerL
UREG = 2**8  # timerU

TX_DELAY_PRO_DOUBLE = 13  # ticks
TX_DELAY_DEBUG_DOUBLE = 24  # ticks
TX_DELAY_PRO_SINGLE = 7  # ticks
TX_DELAY_DEBUG_SINGLE = 13  # ticks

class PICSerialDAQ(IDataAcquisition, ITimingController, IGainController):
    """
    Hardware communication using a Serial Port for the PIC microcontroller.
    """

    # --- Hardcoded layer-1 limits (enforced on every call; the config's
    # --- declared envelope is validated against these at load time) ---
    DRIVER_CAPABILITIES = {"timing", "gain", "acquisition"}
    GAIN_MAX_PINS = 1  # single 'C' Y/N capacitor command
    ADC_BITS = 12
    SUPPORTS_RAMP_WHILE_ACQUIRING = True  # no ramp support at all
    HARDWARE_LIMITS = TimingCapability(
        integration_time_us=ParameterBounds(1, 512_000_000, 1),
        reset_time_us=ParameterBounds(1, 512_000_000, 1),
        period_us=None,  # derived by the firmware: period = int + reset
        ramp_sampling=RampSamplingConfig(False),  # no ramp trigger support
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
        
        # We store these when calculate_actual_timing is called so we can just use them in configure_noramp
        self._last_timer_params = None

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

    def verify_connection(self) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"V")
        while not response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("V", response, "Connection verification")

    def set_event_mode(self, is_single_event: bool) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"R")
            if is_single_event:
                self.serial.write(b"S")
            else:
                self.serial.write(b"M")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Selecting operation mode")

    def set_channels(self, double_ch: bool) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"N")
            if double_ch:
                self.serial.write(b"Y")
            else:
                self.serial.write(b"N")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Changing channels")

    def set_gpio_mask(self, gpio_mask: list[bool]) -> bool:
        if len(gpio_mask) != self.GAIN_MAX_PINS or not all(isinstance(b, bool) for b in gpio_mask):
            logger.error(
                "set_gpio_mask rejected: expected %d boolean pin state(s), got %r",
                self.GAIN_MAX_PINS, gpio_mask,
            )
            return False
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout

        # PIC drives a single GPIO via the 'C' 'Y'/'N' command.
        cap0 = gpio_mask[0]

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"C")
            if cap0:
                self.serial.write(b"Y")
            else:
                self.serial.write(b"N")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "Changing gain")

    def _get_tx_delay(self, debug_mode: bool, multiple_channel: bool) -> int:
        if multiple_channel:
            return TX_DELAY_DEBUG_DOUBLE if debug_mode else TX_DELAY_PRO_DOUBLE
        else:
            return TX_DELAY_DEBUG_SINGLE if debug_mode else TX_DELAY_PRO_SINGLE
    @property
    def supports_ramp_sampling(self) -> bool:
        return self.HARDWARE_LIMITS.ramp_sampling.supported

    def get_adc_settings(self) -> dict:
        return {
            'bits': self.ADC_BITS,
            'vref': 5.0,
            'supports_oversampling': False
        }

    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        logger.warning("PIC does not support ramp sampling")
        return False

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

    def calculate_actual_timing_us(
        self, int_time_us: float, reset_time_us: float, debug_mode: bool = False, multiple_channel: bool = True
    ) -> tuple[float, float]:
        """PIC quantization math (timer search over u/tmr bins); µs in, µs out."""
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return float(int_time_us), float(reset_time_us)
        return self._quantize_timing(int_time_us, reset_time_us, debug_mode, multiple_channel)

    def _quantize_timing(self, int_time_us: float, reset_time_us: float, debug_mode: bool, multiple_channel: bool) -> tuple[float, float]:
        int_time_s = int_time_us / 1e6
        reset_time_s = reset_time_us / 1e6
        if int_time_s <= 0:
            int_time_s = 1e-6
            
        tmr_bin, u_bin, error, act_int_time = 0, 0, -1, 0
        for u_bin_tmp in range(UREG, 0, -1):
            tmr_bin_tmp = round(int_time_s * OSC_FREQ / u_bin_tmp)
            if tmr_bin_tmp == 0:
                tmr_bin_tmp = 1
            int_time_tmp = tmr_bin_tmp * u_bin_tmp / OSC_FREQ
            error_tmp = (int_time_tmp - int_time_s) ** 2
            if (error_tmp <= error and tmr_bin_tmp <= TMREG) or error < 0:
                tmr_bin = tmr_bin_tmp
                u_bin = u_bin_tmp
                error = error_tmp
                act_int_time = int_time_tmp
                
        if tmr_bin > TMREG:
            tmr_bin, u_bin = TMREG, UREG
            act_int_time = tmr_bin * u_bin / OSC_FREQ
            
        tmr_reg = TMREG - tmr_bin
        u_reg = UREG - u_bin
        
        timerU_int = bytes([u_reg & 0xFF])
        timerH_int = bytes([(tmr_reg >> 8) & 0xFF])
        timerL_int = bytes([tmr_reg & 0xFF])

        # Reset time
        if reset_time_s < 0:
            reset_time_s = 0
            
        tx_delay = self._get_tx_delay(debug_mode, multiple_channel)
        bin_raw = reset_time_s * OSC_FREQ
        if bin_raw > TMREG:
            bin_value = TMREG + tx_delay
            act_rst_time = bin_value / OSC_FREQ
            timerH_rst = bytes([0])
            timerL_rst = bytes([0])
        else:
            bin_value = round(bin_raw) - tx_delay
            if bin_value <= 0:
                bin_value = 1
            act_rst_time = (tx_delay + bin_value) / OSC_FREQ
            bin_value = TMREG - bin_value
            timerH_rst = bytes([(bin_value >> 8) & 0xFF])
            timerL_rst = bytes([bin_value & 0xFF])

        self._last_timer_params = (timerU_int, timerH_int, timerL_int, timerH_rst, timerL_rst)
        return act_int_time * 1e6, act_rst_time * 1e6

    def configure_noramp(self, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        if not self.connect():
            return False

        if self._last_timer_params is None:
            self._quantize_timing(int_time_us, reset_time_us, False, True)
            
        timerU_int, timerH_int, timerL_int, timerH_reset, timerL_reset = self._last_timer_params

        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"T")
        while response != b"W" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        if not self._check_response("W", response, "start of timer config"):
            return False

        with self._lock:
            self.serial.write(timerU_int)
            self.serial.write(timerH_int)
            time.sleep(0.0002)
            self.serial.write(timerL_int)
            self.serial.write(timerH_reset)
            time.sleep(0.0002)
            self.serial.write(timerL_reset)

        response = bytes()
        operation_timeout = time.time() + self.timeout
        while not response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "end of timer config")

    def set_mode(self, debug_mode: bool) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"M")
            if debug_mode:
                self.serial.write(b"D")
            else:
                self.serial.write(b"P")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "set mode")

    def arm(self) -> bool:
        if not self.connect():
            return False
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"A")
        while not response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "arm")

    def start_measurement(self, session_model, data_model) -> bool:
        if not self.connect():
            return False
            
        response = bytes()
        operation_timeout = time.time() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"S")
        while not response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
                
        if not self._check_response("D", response, "start"):
            return False

        self._stop_event.clear()
        self._error = False
        self._thread = threading.Thread(target=self._read_loop, args=(session_model, data_model), daemon=True)
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
            self.serial.write(b"E")
        while response != b"D" and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)
        return self._check_response("D", response, "stop")

    def _read_loop(self, session, data_model):
        byte_buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    read_byte = self.serial.read(1)
                if not read_byte:
                    continue
                byte_buffer.extend(read_byte)
                
                if session.debug_mode:
                    if len(byte_buffer) == 8 and session.multiple_channel:
                        self._parse_debug_double(byte_buffer, data_model)
                    elif len(byte_buffer) == 4 and not session.multiple_channel:
                        self._parse_debug_single(byte_buffer, data_model)
                else:
                    if len(byte_buffer) == 4 and session.multiple_channel:
                        self._parse_pro_double(byte_buffer, data_model)
                    elif len(byte_buffer) == 2 and not session.multiple_channel:
                        self._parse_pro_single(byte_buffer, data_model)
            except Exception:
                logger.exception("Error in reader thread")
                self._error = True
                break

    def _parse_debug_double(self, byte_buffer, data_model):
        start_value_ch1 = int.from_bytes(byte_buffer[0:2], byteorder="little")
        stop_value_ch1 = int.from_bytes(byte_buffer[2:4], byteorder="little")
        start_value_ch2 = int.from_bytes(byte_buffer[4:6], byteorder="little")
        stop_value_ch2 = int.from_bytes(byte_buffer[6:8], byteorder="little")
        data_model.append_data(((start_value_ch1, stop_value_ch1), (start_value_ch2, stop_value_ch2)))
        byte_buffer.clear()

    def _parse_debug_single(self, byte_buffer, data_model):
        start_value_ch1 = int.from_bytes(byte_buffer[0:2], byteorder="little")
        stop_value_ch1 = int.from_bytes(byte_buffer[2:4], byteorder="little")
        data_model.append_data(((start_value_ch1, stop_value_ch1), (OVERFLOW_VALUE, OVERFLOW_VALUE)))
        byte_buffer.clear()

    def _parse_pro_double(self, byte_buffer, data_model):
        value_ch1 = int.from_bytes(byte_buffer[0:2], byteorder="little")
        value_ch2 = int.from_bytes(byte_buffer[2:4], byteorder="little")
        data_model.append_data((value_ch1, value_ch2))
        byte_buffer.clear()

    def _parse_pro_single(self, byte_buffer, data_model):
        value_ch1 = int.from_bytes(byte_buffer[0:2], byteorder="little")
        value_ch2 = OVERFLOW_VALUE
        data_model.append_data((value_ch1, value_ch2))
        byte_buffer.clear()

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
