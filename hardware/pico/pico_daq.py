"""Concrete driver for the Raspberry Pi Pico 2 DAQ board (firmware PICO2DAQ >= 2.0).

The firmware protocol, pin map and validation limits are documented in
``raspberry_copy/README.md`` (the firmware source folder); this module is
their host-side mirror:

- ASCII commands terminated by ``\\n``; single-byte replies ``D`` (ok),
  ``E`` (error / parameter rejected), ``?`` (unknown).  While a measurement
  runs the firmware ignores every command except ``E`` — so does this driver
  (a reply byte would corrupt the binary stream).
- No-ramp data stream: little-endian uint32 words ``(sample1 << 16) |
  sample2`` (two 12-bit readings), terminated by the end-of-stream marker
  ``0xFFFFFFFF`` (both halves > 12 bits: impossible as data) followed by the
  ``D`` stop acknowledgement.  The stop path drains the stream up to the
  marker, so no samples are lost between "stop" and the device halting.
- ``T`` floors the times to whole system-clock cycles (150 MHz);
  :meth:`calculate_actual_timing` mirrors that quantization exactly (pure
  computation, no serial traffic).
- ``V`` identity line ``V PICO2DAQ <version> <board id> <clk MHz>``:
  :meth:`connect` runs it as a liveness check — wrong firmware or a system
  clock other than 150 MHz refuses the connection, because the timing
  quantization (and the periods the app reports) assumes 150 MHz.
- ``Q`` status line: after a stop the driver queries the firmware's
  dropped-sample counter and logs a warning (USB backpressure gaps the
  series silently otherwise).

The driver never sends a command while its reader thread is running
(the firmware would ignore it and the reply could corrupt the stream).
"""
import logging
import math
import re
import threading
import time

import serial

from core.constants import overflow_sentinel
from hardware.config.schema import (
    ParameterBounds,
    RampSamplingConfig,
    TimingCapability,
    VoltageRange,
)
from hardware.interfaces import (
    ActualTiming,
    IDataAcquisition,
    IGainController,
    ITimingController,
)

logger = logging.getLogger(__name__)

SERIAL_TIMEOUT = 0.1
TOTAL_TIMEOUT = 1.5
QUERY_TIMEOUT = 0.5
READER_BATCH = 1000

# Firmware constants (must match raspberry_copy/main.c)
FW_NAME = "PICO2DAQ"
SYS_CLK_MHZ = 150.0                     # timing quantization mirror
STREAM_MARKER = 0xFFFFFFFF              # both halves > 12 bits: never data
ADC_SAMPLE_MAX = (1 << 12) - 1          # 12-bit ADC
# The PIO ramp generator's 16.8 clock divider bound: one trigger pulse
# costs 10 PIO cycles, so dt <= 65536 * 10 sys-clocks.
RAMP_DT_MAX_US = 65536.0 * 10.0 / SYS_CLK_MHZ
# Ramp process v2 (identical in both generators): reset LOW -> settle
# (RESET HIGH, no pulses) -> N = int/dt + 1 pulses (50% duty) -> restart.
# The application-level reset time is the RESET-LOW duration.
#
# Generator selection (bench result): the PIO drives dt up to the threshold
# (hardware-perfect edges; everything quantizes to the dt/10 tick), the
# software engine on the timing core above it (one system-clock-cycle
# resolution for dt, reset and settle, no dt-coupled quantization).
RAMP_PIO_MAX_US = 100.0
# Fixed settle between the reset release and the first pulse edge.
RAMP_SETTLE_PIO_CYCLES = 20             # PIO generator: 20 PIO cycles = 2*dt
RAMP_SETTLE_SOFT_US = 2.0               # software generator: 2 us

_IDENTITY_RE = re.compile(r"^V (\S+) (\S+) (\S+) (\d+(?:\.\d+)?)$")


class PicoSerialDAQ(IDataAcquisition, ITimingController, IGainController):
    """Raspberry Pi Pico 2 running the PICO2DAQ 2.0 firmware."""

    # --- Hardcoded layer-1 limits (enforced on every call; the config's
    # --- declared envelope is validated against these at load time).
    # --- They mirror the firmware's own validation of 'T' and 'P'. ---
    DRIVER_CAPABILITIES = {"timing", "gain", "acquisition"}
    GAIN_MAX_PINS = 3  # the capacitor command carries 3 bits
    ADC_BITS = 12
    ADC_NATIVE_RANGE = VoltageRange(0.0, 3.3)  # the bare ADC pin sees it all
    # Firmware constraint: the ramp-trigger mode and the acquisition loop
    # are mutually exclusive on this device (same uC, one ADC).
    SUPPORTS_RAMP_WHILE_ACQUIRING = False
    HARDWARE_LIMITS = TimingCapability(
        integration_time_us=ParameterBounds(5, 10_000_000, 1),
        reset_time_us=ParameterBounds(5, 10_000_000, 1),
        period_us=None,  # derived by the firmware: period = int + reset
        ramp_sampling=RampSamplingConfig(
            True, dt_us=ParameterBounds(1, RAMP_DT_MAX_US, 1)
        ),
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
        self._stream_ended = threading.Event()
        self._tail_bytes = b""        # bytes after the end-of-stream marker
        self._resync_drops = 0        # bytes dropped while realigning
        self._words_read = 0
        self._period_s = None         # from the last accepted configure_*
        self.identity = None          # parsed 'V' line, set by connect()

    # ------------------------------------------------------------------
    # Connection (port + firmware liveness check)
    # ------------------------------------------------------------------

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
                if not self._verify_identity():
                    self.serial.close()
                    return False
            return self.serial.is_open
        except serial.SerialException as e:
            logger.warning("Connection error: %s", e)
            return False

    def _verify_identity(self) -> bool:
        """Liveness check on a freshly opened port: confirm PICO2DAQ
        firmware and a 150 MHz system clock (the quantization mirror
        assumes it).  A bare Pico, another device or old firmware fails
        here instead of producing garbage."""
        identity = self.check_identity()
        if identity is None:
            logger.warning(
                "Pico identity check failed: no valid 'V' reply on %s "
                "(not a PICO2DAQ board?)", self._port
            )
            return False
        if identity["firmware"] != FW_NAME:
            logger.warning(
                "Pico identity check failed: firmware %r, expected %r",
                identity["firmware"], FW_NAME,
            )
            return False
        if abs(identity["clk_mhz"] - SYS_CLK_MHZ) > 0.5:
            logger.warning(
                "Pico identity check failed: system clock %s MHz, expected %s",
                identity["clk_mhz"], SYS_CLK_MHZ,
            )
            return False
        self.identity = identity
        logger.info(
            "Pico connected: %s %s, board %s, %s MHz",
            identity["firmware"], identity["version"],
            identity["board_id"], identity["clk_mhz"],
        )
        return True

    def check_identity(self, timeout: float = QUERY_TIMEOUT) -> dict | None:
        """Send the 'V' handshake and parse the reply.

        Returns {'firmware', 'version', 'board_id', 'clk_mhz'} or None.
        The port must already be open (connect() calls this right after
        opening; callers wanting the full flow use connect()).
        """
        if not self.serial.is_open:
            return None
        line = self._transact_line("V", timeout=timeout)
        m = _IDENTITY_RE.match(line)
        if m is None:
            return None
        return {
            "firmware": m.group(1),
            "version": m.group(2),
            "board_id": m.group(3),
            "clk_mhz": float(m.group(4)),
        }

    def disconnect(self) -> bool:
        if self.is_measuring:
            try:
                self.stop_measurement()
            except Exception:
                logger.exception("Error stopping the measurement on disconnect")
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

    @property
    def is_measuring(self) -> bool:
        """True while the reader thread is running."""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Command plumbing
    # ------------------------------------------------------------------

    def _send_command(self, command: str) -> bool:
        """Write one ASCII command and consume its one-byte reply."""
        if self.is_measuring:
            # The firmware ignores everything but 'E' while running; a reply
            # byte would corrupt the binary stream.
            logger.error("Command %r refused: a measurement is running", command)
            return False
        if not self.connect():
            return False

        deadline = time.monotonic() + self.timeout
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write((command + "\n").encode("ascii"))
            reply = self._read_reply(deadline)

        if reply == b"D":
            return True
        if reply:
            logger.warning("[Pico %s] rejected: reply %r", command, reply)
        else:
            logger.warning("[Pico %s] timed out", command)
        return False

    def _read_reply(self, deadline: float) -> bytes:
        """Read the single-byte command reply (b'' on timeout)."""
        while time.monotonic() < deadline:
            response = self.serial.read(1)
            if response:
                return response
            time.sleep(0.001)
        return b""

    def _transact_line(self, command: str, timeout: float = QUERY_TIMEOUT) -> str:
        """Write a command whose reply is one '\\n'-terminated line."""
        if not self.connect():
            return ""
        deadline = time.monotonic() + timeout
        line = bytearray()
        with self._lock:
            self.serial.reset_input_buffer()
            self.serial.write((command + "\n").encode("ascii"))
            while time.monotonic() < deadline:
                chunk = self.serial.read(1)
                if chunk:
                    line += chunk
                    if chunk == b"\n":
                        break
                else:
                    time.sleep(0.002)
        return line.decode("ascii", errors="replace").strip()

    # ------------------------------------------------------------------
    # IGainController
    # ------------------------------------------------------------------

    def set_gpio_mask(self, gpio_mask: list[bool]) -> bool:
        if len(gpio_mask) != self.GAIN_MAX_PINS or not all(
            isinstance(b, bool) for b in gpio_mask
        ):
            logger.error(
                "set_gpio_mask rejected: expected %d boolean pin states, got %r",
                self.GAIN_MAX_PINS, gpio_mask,
            )
            return False
        state = "".join("1" if b else "0" for b in gpio_mask)
        return self._send_command(f"C{state}")

    def set_event_mode(self, is_single_event: bool) -> bool:
        return self._send_command("RS" if is_single_event else "RM")

    # ------------------------------------------------------------------
    # ITimingController
    # ------------------------------------------------------------------

    @property
    def supports_ramp_sampling(self) -> bool:
        return self.HARDWARE_LIMITS.ramp_sampling.supported

    def _validate_timing_us(self, int_time_us: float, reset_time_us: float) -> bool:
        limits = self.HARDWARE_LIMITS
        ok = True
        if not limits.integration_time_us.contains_value(int_time_us):
            logger.error(
                "integration time %s us out of hardware limits %s",
                int_time_us, limits.integration_time_us,
            )
            ok = False
        if not limits.reset_time_us.contains_value(reset_time_us):
            logger.error(
                "reset time %s us out of hardware limits %s",
                reset_time_us, limits.reset_time_us,
            )
            ok = False
        return ok

    @staticmethod
    def _quantize_us(us: float) -> float:
        """Floor to whole system-clock cycles, exactly like the firmware's
        ``T`` command (``(uint32_t)(us * 150)``)."""
        return math.floor(us * SYS_CLK_MHZ) / SYS_CLK_MHZ

    def calculate_actual_timing(
        self,
        int_time_us: float,
        reset_time_us: float,
        period_us: float = None,
        num_channels: int = None,
    ) -> ActualTiming:
        # Pure computation (no serial traffic): mirror the firmware's
        # truncation to 150 MHz cycles.  The channel count does not affect
        # the timing on this device.
        act_int = self._quantize_us(int_time_us)
        act_rst = self._quantize_us(reset_time_us)
        period = float(period_us) if period_us is not None else (act_int + act_rst)
        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=act_int,
            reset_time_us=act_rst,
            period_us=period,
            sample_frequency_hz=freq,
        )

    def configure_noramp(self, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        if period_us is not None:
            logger.debug(
                "Pico derives the period itself (int + reset); ignoring period_us=%s",
                period_us,
            )
        if not self._send_command("XN"):
            return False
        if not self._send_command(f"T {int_time_us},{reset_time_us}"):
            return False
        self._period_s = (
            self._quantize_us(int_time_us) + self._quantize_us(reset_time_us)
        ) / 1e6
        return True

    # ------------------------------------------------------------------
    # Ramp timing mirror (pure computation — no serial traffic)
    # ------------------------------------------------------------------

    @staticmethod
    def ramp_generator_for(ramp_dt_us: float) -> str:
        """The generator the driver selects: 'pio' up to the threshold
        (hardware-perfect edges, dt/10-tick quantization), 'software'
        above it (one system-clock-cycle resolution)."""
        return "pio" if ramp_dt_us <= RAMP_PIO_MAX_US else "software"

    def calculate_ramp_timing(
        self, ramp_dt_us: float, int_time_us: float, reset_time_us: float
    ) -> ActualTiming:
        """Exact mirror of what the firmware will generate for a ramp
        configuration — the same numbers its 'Q' command reports (int =
        first-to-last CONV_START edge, reset = RESET-LOW duration).  The
        UI shows these values; configure_ramp() programs exactly this.

        Raises ValueError when the request cannot be represented
        (int < dt: the minimum integration is one interval = 2 pulses).
        """
        generator = self.ramp_generator_for(ramp_dt_us)
        clk = SYS_CLK_MHZ * 1e6
        int_cycles = math.floor(int_time_us * SYS_CLK_MHZ)
        reset_cycles = math.floor(reset_time_us * SYS_CLK_MHZ)

        if generator == "pio":
            div = SYS_CLK_MHZ * ramp_dt_us / 10.0            # ideal divider
            div_int = int(div)
            div_frac8 = int((div - div_int) * 256.0)         # firmware truncates
            div_actual = div_int + div_frac8 / 256.0
            dt_eff = div_actual * 10.0e6 / clk
            intervals = int(int_cycles * 1e6 / (clk * ramp_dt_us))
            reset_pio = int(reset_cycles * 10.0e6 / (clk * ramp_dt_us))
            if intervals < 1 or reset_pio < 1:
                raise ValueError(
                    f"int={int_time_us} us must be at least one dt={ramp_dt_us} "
                    "us interval (minimum integration = dt, 2 pulses)"
                )
            act_int = (intervals) * dt_eff           # first-to-last edge
            act_reset = reset_pio * dt_eff / 10.0    # RESET-LOW duration
            settle = RAMP_SETTLE_PIO_CYCLES * dt_eff / 10.0
            period = act_reset + settle + (intervals + 1) * dt_eff
        else:
            dt_cycles = max(1, math.floor(ramp_dt_us * SYS_CLK_MHZ))
            dt_eff = dt_cycles / SYS_CLK_MHZ
            intervals = int_cycles // dt_cycles
            if intervals < 1:
                raise ValueError(
                    f"int={int_time_us} us must be at least one dt={ramp_dt_us} "
                    "us interval (minimum integration = dt, 2 pulses)"
                )
            act_int = intervals * dt_eff
            act_reset = reset_cycles / SYS_CLK_MHZ
            settle = RAMP_SETTLE_SOFT_US
            period = act_reset + settle + (intervals + 1) * dt_eff

        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=act_int,
            reset_time_us=act_reset,
            period_us=period,
            sample_frequency_hz=freq,
        )

    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        dt_limits = self.HARDWARE_LIMITS.ramp_sampling.dt_us
        if dt_limits is not None and not dt_limits.contains_value(ramp_dt_us):
            logger.error(
                "ramp dt %s us out of hardware limits %s", ramp_dt_us, dt_limits
            )
            return False

        # The exact mirror decides representability and the applied values;
        # the firmware is then programmed to reproduce precisely these.
        try:
            actual = self.calculate_ramp_timing(ramp_dt_us, int_time_us, reset_time_us)
        except ValueError as exc:
            logger.error("ramp configuration rejected: %s", exc)
            return False
        generator = self.ramp_generator_for(ramp_dt_us)
        if actual.int_time_us != int_time_us or actual.reset_time_us != reset_time_us:
            logger.info(
                "ramp quantization (%s generator): int %s -> %s us, reset "
                "%s -> %s us (dt=%s us)",
                generator, int_time_us, actual.int_time_us,
                reset_time_us, actual.reset_time_us, ramp_dt_us,
            )

        if not self._send_command("XR"):
            return False
        if not self._send_command("GP" if generator == "pio" else "GC"):
            return False

        if generator == "pio":
            clk = SYS_CLK_MHZ * 1e6
            div = SYS_CLK_MHZ * ramp_dt_us / 10.0
            div_int = int(div)
            div_frac8 = int((div - div_int) * 256.0)
            div_actual = div_int + div_frac8 / 256.0
            dt_eff = div_actual * 10.0e6 / clk
            int_cycles = math.floor(int_time_us * SYS_CLK_MHZ)
            reset_cycles = math.floor(reset_time_us * SYS_CLK_MHZ)
            intervals = int(int_cycles * 1e6 / (clk * ramp_dt_us))
            reset_pio = int(reset_cycles * 10.0e6 / (clk * ramp_dt_us))
            # The application path: we computed the exact raw counts, the
            # firmware programs them verbatim ('O').
            if not self._send_command(
                f"O {div_int},{div_frac8},{reset_pio},{intervals + 1}"
            ):
                return False
        else:
            # Software generator: plain microseconds (T floors them, P is
            # cycle-floored) — nothing derived to drift.
            dt_eff = max(1.0, math.floor(ramp_dt_us * SYS_CLK_MHZ)) / SYS_CLK_MHZ
            if not self._send_command(f"T {int_time_us},{reset_time_us}"):
                return False
            if not self._send_command(f"P {ramp_dt_us}"):
                return False

        self._period_s = actual.period_us / 1e6
        return self._verify_applied_ramp(actual, generator, dt_eff)

    def _verify_applied_ramp(self, expected: ActualTiming, generator: str, dt_eff: float) -> bool:
        """One-time check after configure: the firmware's 'Q' status must
        report exactly the timing we computed (it is never queried for the
        UI display — those values come from calculate_ramp_timing)."""
        line = self._transact_line("Q")
        if not line:
            logger.warning("ramp verification skipped: no Q reply")
            return True
        try:
            fields = dict(
                part.split("=", 1) for part in line.split()[1:] if "=" in part
            )
            reported_gen = "pio" if fields["gen"] == "pio" else "software"
            ok = (
                reported_gen == generator
                and abs(float(fields["int"]) - expected.int_time_us) <= 0.01
                and abs(float(fields["reset"]) - expected.reset_time_us) <= 0.01
                and abs(float(fields["dt"]) - dt_eff) <= 0.01
            )
            if not ok:
                logger.error(
                    "ramp verification FAILED: expected gen=%s int=%.4f us "
                    "reset=%.4f us dt=%.4f us, firmware Q reports %s",
                    generator, expected.int_time_us, expected.reset_time_us,
                    dt_eff, line,
                )
            else:
                logger.debug("ramp verification ok: %s", line)
            return ok
        except (KeyError, ValueError):
            logger.warning("ramp verification: unparsable Q reply %r", line)
            return True

    def set_mode(self, debug_mode: bool) -> bool:
        # The firmware has no mode command: it always transmits the start
        # and stop samples of every cycle (debug); the operational
        # difference is derived on receipt.
        logger.debug("set_mode: no-op on the Pico (always start/stop)")
        return True

    def set_channels(self, multiple_channel: bool) -> bool:
        # The firmware has a single ADC (num_channels: 1 in the config);
        # there is no channel command to send.
        logger.debug("set_channels: no channel command on the Pico")
        return True

    def arm(self) -> bool:
        # No arm command: while idle the firmware holds the integrator in
        # reset (capacitor discharged), and every run starts with a clean
        # reset-settle-integrate sequence.
        logger.debug("arm: no-op on the Pico (idle state already resets the integrator)")
        return True

    def start_timing(self) -> bool:
        """Start the timing engine without the acquisition reader (bench use:
        this Pico holds no acquisition role — e.g. virtual acquisition — so
        nobody would consume the stream).  In no-ramp mode the firmware's
        ring buffer absorbs the unread data (drops counted in 'Q'), the
        timing itself is unaffected."""
        return self._send_command("S")

    def stop_timing(self) -> bool:
        if not self.connect():
            return False
        with self._lock:
            self.serial.reset_input_buffer()  # discard the unread stream
            self.serial.write(b"E\n")
        # Consume whatever is in flight (residual stream words) up to the
        # end-of-stream marker and the 'D' ack behind it.
        deadline = time.monotonic() + (self._period_s or 0.0) + 3.0
        tail = bytearray()
        ok = False
        with self._lock:
            while time.monotonic() < deadline:
                chunk = self.serial.read(256)
                if chunk:
                    tail += chunk
                    idx = tail.find(b"\xff\xff\xff\xff")  # the marker word
                    if idx != -1 and len(tail) >= idx + 5:
                        ok = tail[idx + 4:idx + 5] == b"D"
                        break
                else:
                    time.sleep(0.001)
        if not ok:
            logger.error(
                "Pico stop_timing: no end-of-stream marker + ack (device "
                "stalled or unplugged?)"
            )
        self._report_run_diagnostics()
        return ok

    # ------------------------------------------------------------------
    # IDataAcquisition
    # ------------------------------------------------------------------

    def get_adc_settings(self) -> dict:
        native = self.ADC_NATIVE_RANGE
        return {
            "bits": self.ADC_BITS,
            "input_range_volts": {
                "min_volts": native.min_volts,
                "max_volts": native.max_volts,
            },
            "supports_oversampling": False,
        }

    def set_input_range(self, min_volts: float, max_volts: float) -> bool:
        # Fixed range: the ADC pin sees the signal directly (no front-end
        # scaler).  Accepting anything else would mislabel every charge.
        native = self.ADC_NATIVE_RANGE
        if (
            abs(min_volts - native.min_volts) < 1e-9
            and abs(max_volts - native.max_volts) < 1e-9
        ):
            return True
        logger.error(
            "set_input_range rejected: the Pico ADC range is fixed at %s V",
            native,
        )
        return False

    def start_measurement(self, session_model, data_model, ramp_sink=None) -> bool:
        # The Pico's single ADC cannot acquire while it generates ramp
        # triggers (SUPPORTS_RAMP_WHILE_ACQUIRING = False) — the manager
        # refuses that combination long before reaching this point; a sink
        # here would be a wiring bug.
        if ramp_sink is not None:
            logger.error("start_measurement: this driver cannot stream ramp samples")
            return False
        if self.is_measuring:
            logger.error("start_measurement refused: a measurement is already running")
            return False
        if not self._send_command("S"):
            return False

        self._stop_event.clear()
        self._stream_ended.clear()
        self._tail_bytes = b""
        self._resync_drops = 0
        self._words_read = 0
        self._error = False

        self._thread = threading.Thread(
            target=self._read_loop, args=(session_model, data_model), daemon=True
        )
        self._thread.start()
        return True

    def stop_measurement(self) -> bool:
        if not self.is_measuring:
            # Best effort: put an idle device in a known state ('D' when it
            # already is idle, harmless) and still surface the diagnostics.
            ok = self._send_command("E") if self.is_connected else True
            if self.is_connected:
                self._report_run_diagnostics()
            return ok

        # 1) Tell the device to stop.  It streams the end-of-stream marker
        #    (and any samples still in flight) and only then acks 'D' —
        #    the reader keeps consuming until the marker, so nothing
        #    between "stop" and the halt is lost.
        with self._lock:
            self.serial.write(b"E\n")

        # 2) Wait for the marker, bounded by the configured period (the
        #    engine finishes its last cycle first).
        timeout = (self._period_s or 0.0) + 3.0
        marker_seen = self._stream_ended.wait(timeout)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

        # 3) The stop ack is the first byte after the marker (the reader
        #    leaves it unread — first in its tail buffer, else on the port).
        with self._lock:
            if self._tail_bytes:
                ack = self._tail_bytes[:1]
                self._tail_bytes = b""
            else:
                ack = self.serial.read(1)

        if not marker_seen:
            logger.error(
                "Pico stop: no end-of-stream marker within %.1f s "
                "(device stalled or unplugged?)", timeout,
            )
        elif ack != b"D":
            logger.error(
                "Pico stop: expected 'D' after the end-of-stream marker, got %r", ack
            )

        self._report_run_diagnostics()
        return marker_seen and ack == b"D"

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _read_loop(self, session, data_model):
        debug_mode = bool(getattr(session, "debug_mode", True))
        # App-side overflow/missing-channel marker: one past this ADC's full
        # scale (mapped to NaN by the conversion layer).
        missing = overflow_sentinel(self.ADC_BITS)
        byte_buffer = bytearray()
        batch = []
        try:
            while not self._stop_event.is_set() and not self._stream_ended.is_set():
                waiting = self.serial.in_waiting
                if not waiting:
                    time.sleep(0.001)
                    continue
                with self._lock:
                    chunk = self.serial.read(waiting)
                if not chunk:
                    continue
                byte_buffer += chunk

                while len(byte_buffer) >= 4:
                    word = int.from_bytes(byte_buffer[:4], "little")

                    if word == STREAM_MARKER:
                        # End of the stream: keep the bytes that follow (the
                        # stop ack) for stop_measurement and stop parsing.
                        del byte_buffer[:4]
                        with self._lock:
                            self._tail_bytes = bytes(byte_buffer)
                        byte_buffer.clear()
                        self._stream_ended.set()
                        break

                    start, stop = word >> 16, word & 0xFFFF
                    if start > ADC_SAMPLE_MAX or stop > ADC_SAMPLE_MAX:
                        # Lost 4-byte alignment (should not happen with the
                        # marker handshake): drop one byte and realign.
                        del byte_buffer[:1]
                        self._resync_drops += 1
                        continue

                    del byte_buffer[:4]
                    self._words_read += 1
                    if debug_mode:
                        batch.append(((start, stop), (missing, missing)))
                    else:
                        diff = stop - start if stop >= start else missing
                        batch.append((diff, missing))
                    if len(batch) >= READER_BATCH:
                        data_model.append_batch(batch)
                        batch = []
        except Exception:
            logger.exception("Error in Pico reader thread")
            self._error = True
        finally:
            if batch:
                data_model.append_batch(batch)
            logger.debug(
                "Pico reader finished: %d samples, %d resync byte(s) dropped",
                self._words_read, self._resync_drops,
            )

    # ------------------------------------------------------------------
    # Post-run diagnostics
    # ------------------------------------------------------------------

    def _report_run_diagnostics(self) -> None:
        if self._resync_drops:
            logger.warning(
                "Pico stream: %d byte(s) dropped while realigning — %d "
                "sample(s) may be corrupted",
                self._resync_drops, self._resync_drops // 4 + 1,
            )
        line = self._transact_line("Q")
        m = re.search(r"dropped=(\d+)", line) if line else None
        if m and int(m.group(1)):
            logger.warning(
                "Pico firmware dropped %s sample word(s) (USB backpressure): "
                "the series has gaps", m.group(1),
            )
