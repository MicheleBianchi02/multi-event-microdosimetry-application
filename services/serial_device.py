"""
Serial Device Communication Protocol Summary

This module handles communication with the microcontroller. Below is a summary of the ASCII
byte commands sent to the device and the expected responses.

Commands Sent:
- 'V' : Verify Connection.
        Expects: 'V'
- 'R' : Select operation mode. Followed by 'S' (single-event mode) or 'M' (multi event mode).
        Expects: 'D' (Done)
- 'T' : Configure Timing.
        Expects: 'W' (Waiting), then sends 5 binary timer bytes, then expects 'D' (Done)
- 'N' : Set Channels mode. Followed by 'Y' (double channel) or 'N' (single channel).
        Expects: 'D' (Done)
- 'M' : Set Mode. Followed by 'D' (debug mode) or 'P' (production mode).
        Expects: 'D' (Done)
- 'C' : Increase gain. Followed by 'Y' (use higher capacitance) or 'N' (reduce capacitance).
        Expects: 'D' (Done)
- 'A' : Arm Device. Prepares for measurement.
        Expects: 'D' (Done)
- 'S' : Start Measurement. Begins continuous data acquisition.
        Expects: 'D' (Done)
- 'E' : End/Stop Measurement. Halts data acquisition.
        Expects: 'D' (Done)

Standard Responses:
- 'V' : Connection verified.
- 'W' : Waiting for multi-byte payload.
- 'D' : Done / Acknowledge success.
"""

import logging
import threading
import time

import serial
import serial.tools.list_ports

SERIAL_TIMEOUT = 0.1
TOTAL_TIMEOUT = 1.5

logger = logging.getLogger(__name__)


def get_port() -> list[dict[str, str]]:
    serial_ports = serial.tools.list_ports.comports()
    port_list = []
    for port in serial_ports:
        port_list.append(
            {
                "name": port.name,
                "description": port.description,
                "manufacturer": port.manufacturer,
                "device": port.device,
            }
        )
    return port_list


class SerialDevice:
    """
    Hardware communication using a Serial Port.
    """

    def __init__(self, port: str = None, baud_rate: int = 115200):
        self._port = port
        self._baud_rate = baud_rate
        self.serial = serial.Serial()
        self.timeout = TOTAL_TIMEOUT
        self._lock = threading.Lock()

    def set_connection_params(self, port: str, baud_rate: int) -> None:
        """Update the connection parameters used by connect()."""
        self._port = port
        self._baud_rate = baud_rate

    def connect(self) -> bool:
        """Establish connection with the device."""
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
        """Close the connection with the device."""
        if self.serial.is_open:
            self.serial.close()
        return True

    def is_connected(self) -> bool:
        """Return True if the device is currently connected."""
        return self.serial.is_open

    def verify_connection(self) -> bool:
        """Perform a handshake to verify the device is responding."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write("V".encode("ascii"))

        while not response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("V", response, "Connection verification")

    def set_operation_mode(self, event_mode: bool) -> bool:
        """Configure the device single-event mode or multi-event mode (ie integrtation)."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"D"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"R")
            if event_mode:
                self.serial.write(b"S")
            else:
                self.serial.write(b"M")

        while response != expected_response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "Selecting operation mode")

    def configure_timing(
        self, timerU_int, timerH_int, timerL_int, timerH_reset, timerL_reset
    ) -> bool:
        """Send timing configurations to the device."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"W"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"T")

        while response != expected_response and time.time() < operation_timeout:
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

    def set_channels(self, double_ch: bool) -> bool:
        """Configure the device for single or double channel mode."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"D"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"N")
            if double_ch:
                self.serial.write(b"Y")
            else:
                self.serial.write(b"N")

        while response != expected_response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "Changing channels")

    def set_mode(self, debug_mode: bool) -> bool:
        """Configure the device for debug or production mode."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"D"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"M")
            if debug_mode:
                self.serial.write(b"D")
            else:
                self.serial.write(b"P")

        while response != expected_response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "set mode")

    def change_gain(self, increase_gain: bool) -> bool:
        """Increase gain by changin integration capacitor"""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"D"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"C")
            if increase_gain:
                self.serial.write(b"Y")
            else:
                self.serial.write(b"N")

        while response != expected_response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "Changing gain")

    def arm(self) -> bool:
        """Arm the device for measurement."""
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

    def start_measurement(self) -> bool:
        """Start the continuous measurement."""
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

        return self._check_response("D", response, "start")

    def stop_measurement(self) -> bool:
        """Stop the continuous measurement."""
        if not self.connect():
            return False

        response = bytes()
        operation_timeout = time.time() + self.timeout
        expected_response = b"D"

        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write(b"E")

        while response != expected_response and time.time() < operation_timeout:
            with self._lock:
                response = self.serial.read(1)

        return self._check_response("D", response, "stop")

    def read_data(self, num_bytes: int = 1) -> bytes:
        """Read data from the device. Should be non-blocking or have a short timeout."""
        with self._lock:
            return self.serial.read(num_bytes)

    def _check_response(
        self, expected_str: str, response: bytes, operation: str
    ) -> bool:
        if not response:
            logger.warning("[%s] timed out.", operation)
            return False

        if response == expected_str.encode("ascii"):
            return True

        try:
            text = response.decode("utf-8").strip()
        except UnicodeDecodeError:
            text = response

        logger.warning(
            "[%s] failed. Expected %r but got %r.", operation, expected_str, text
        )
        return False
