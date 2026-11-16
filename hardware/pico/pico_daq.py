"""Concrete driver for the Raspberry Pi Pico 2 DAQ board (firmware PICO2DAQ 2.5).

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
FW_REQUIRED_VERSION = (2, 5)             # 2.5 fixed the software generator's
                                        # reset anchor; the app path programs raw
                                        # counts ('O'/'F'/'A'/'B'), so mirrors newer
                                        # than the gate (2.6 divider round-up) work
                                        # on any board the gate accepts
SYS_CLK_MHZ = 150.0                     # timing quantization mirror
STREAM_MARKER = 0xFFFFFFFF              # both halves > 12 bits: never data
ADC_SAMPLE_MAX = (1 << 12) - 1          # 12-bit ADC
# Ramp dt upper bound = the general time range: the fixed-clock and
# software generators (the app paths for any dt when reset = 5 us, and for
# dt > 100 us otherwise) have one-clock-cycle resolution and no
# dt-coupled bound, and the dt-scaled PIO generator is never driven past
# RAMP_PIO_MAX_US by the auto-selection, so its 16.8 divider constraint
# is trivially satisfied.
RAMP_DT_MAX_US = 10_000_000.0
# Ramp process v2 (identical in all generators): reset LOW -> settle
# (RESET HIGH, no pulses) -> N = int/dt + 1 pulses (~50% duty) -> restart.
# The application-level reset time is the RESET-LOW duration.
#
# Generator selection: reset = 5 us (the firmware minimum, quantized)
# -> the fixed-clock PIO generator at any dt (zero jitter, one-cycle
# resolution, reset/settle independent of dt).  Otherwise the
# dt-scaled PIO drives dt up to the threshold (hardware-perfect edges;
# everything quantizes to the dt/10 tick), and the software engine on
# the timing core above it (one system-clock-cycle resolution for dt,
# reset and settle, no dt-coupled quantization).
RAMP_PIO_MAX_US = 100.0
# Fixed settle between the reset release and the first pulse edge.
RAMP_SETTLE_PIO_CYCLES = 20             # dt-scaled PIO: 20 PIO cycles = 2*dt
RAMP_SETTLE_SOFT_US = 2.0               # software generator: 2 us
# Software generator (firmware 2.4): the LAST pulse holds only
# min(dt/2, this cap) HIGH before the cycle wraps into the reset phase —
# the old trailing low half doubled the period in markers mode (dt =
# T_int).  dt <= 2 us keeps the exact old waveform.
RAMP_SOFT_LAST_HIGH_US = 1.0
# Fixed-clock PIO generator (firmware 2.2, docs/PICO/v2.2/ramp_fixed.pio):
# the SM runs at clk_sys with divider 1 (integer -> zero jitter) and its
# reset (5 us) / settle (2 us) are program immediates, so the dt
# half-period and the pulse count are the only programmed values.  One
# pulse spans 2*half + 7 sys-clock cycles; dt rounds UP to that grid
# (never faster than requested — the grid is odd-only, see
# _fixed_sm_raw_counts).
FIXED_RESET_US = 5.0
FIXED_SETTLE_US = 2.0
FIXED_PULSE_OVERHEAD_CYCLES = 7         # per-pulse instruction cycles
FIXED_MIN_DT_CYCLES = 9                 # overhead + 2 -> half >= 1
# Transient-test generator (firmware 2.3, docs/PICO/v2.2/ramp_transient.pio):
# the dt-scaled architecture with the clock PINNED by an integer divider —
# TRANSIENT_DIV = 16 at 150 MHz, so one tick = 16/150 us (~106.7 ns) and
# one pulse = 10 ticks = 1.0667 us = 937.5 kS/s.  Divider 15 would give
# exactly 1.000 us = the NI USB-6451's absolute 1 MS/s spec with zero
# margin: any positive crystal error of the Pico's nominal 150 MHz clocks
# the card above spec (the B35 failure class; the fixed generator is
# derated below the limit for the same reason).  The integer divider is
# the source of truth on both sides — a float-derived divider could floor
# back to 15.  A study mode (explicitly requested, never auto-selected):
# the reset is sweepable down to 1 us — deliberately below the validated
# reset minimum — and the settle before the first CONV_START edge is a
# single tick (~107 ns), so the ramp is sampled right at the reset
# release.  The APPLIED dt (not the nominal 1 us) is what the timing
# window's true-values card shows.
TRANSIENT_DIV = 16
TRANSIENT_DT_US = TRANSIENT_DIV * 10.0 / SYS_CLK_MHZ   # 1.0667 us, 937.5 kS/s
TRANSIENT_SETTLE_TICKS = 1              # one ~107 ns tick before the first edge
TRANSIENT_RESET_MIN_US = 1.0            # test-mode floor (below the 5 us minimum)
# Reset-sampling transient generator ('GR', firmware 2.3 bench build): the
# CONV_START pulses run DURING the reset phase (the first edge coincides
# with the reset assertion) and the integration phase is silent.  The
# app's fields are SWAPPED in this mode: the Integration Time field drives
# the pulsed (sampled) reset phase, the Reset Time field the silent
# integration.
TRANSIENT_RESET_PULSED_MIN_US = 1.0     # sampled reset-phase floor
TRANSIENT_RESET_MIN_INT_TICKS = 3       # firmware bound on the silent phase
# Test-mode upper bounds, from the generators' 32-bit counts (deliberately
# far above the 10 s measurement envelope: studies may integrate for tens
# of seconds).  Pulses are a uint32: dt = 1.0667 us allows ~4570 s of
# pulsed train — capped at 1 hour with margin.  The silent phase is
# counted in ~107 ns ticks (uint32): ~457 s — capped at 5 minutes.
TRANSIENT_TEST_INT_MAX_US = 3_600_000_000.0    # 1 h of pulsed train
TRANSIENT_TEST_SILENT_MAX_US = 300_000_000.0   # 5 min of silent phase

def _floor_cycles(us: float) -> int:
    """Floor a microsecond value to whole system-clock cycles, tolerant of
    the wizard's us -> s -> us float round trip landing one count low at
    exact boundaries (a typed 200 us arrives as 199.99999999999997, which
    floors to 29999 cycles instead of 30000 — and on the dt-scaled PIO
    that loses a whole 100 ns reset tick).  A typed value cannot specify
    a cycle to 1e-7 precision, so a product within a millionth of a cycle
    below an integer is round-trip noise, not part of the request."""
    return math.floor(us * SYS_CLK_MHZ + 1e-6)


_IDENTITY_RE = re.compile(r"^V (\S+) (\S+) (\S+) (\d+(?:\.\d+)?)$")


class PicoSerialDAQ(IDataAcquisition, ITimingController, IGainController):
    """Raspberry Pi Pico 2 running the PICO2DAQ 2.5 firmware."""

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
        firmware, version >= 2.5 and a 150 MHz system clock (the
        quantization mirror assumes it).  A bare Pico, another device or
        old firmware fails here instead of producing garbage."""
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
        version = self._parse_version(identity["version"])
        if re.search(r"[^\d.]", identity["version"]):
            # A suffixed TEST build (e.g. '2.4-test1', the inverted-reset
            # atomiqa_1 board): connectable on purpose, but loud — never
            # confuse it with a standard board's firmware.
            logger.warning(
                "Pico reports TEST firmware %r (e.g. the inverted-reset "
                "atomiqa_1 bench build): reset polarity is INVERTED on "
                "this board — do NOT run standard measurements against it",
                identity["version"],
            )
        if version is None or version < FW_REQUIRED_VERSION:
            logger.warning(
                "Pico identity check failed: firmware version %s, "
                "required >= %s (reflash the board)",
                identity["version"],
                ".".join(str(p) for p in FW_REQUIRED_VERSION),
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

    @staticmethod
    def _parse_version(text: str) -> tuple | None:
        """Firmware version as a comparable int tuple ('2.4' -> (2, 4)),
        or None when unparsable.  A trailing non-numeric suffix (the
        bench test builds, e.g. '2.4-test1') is stripped BEFORE the
        numeric gate: the base numbers alone decide mirror
        compatibility, so the test builds stay connectable."""
        m = re.match(r"^(\d+(?:\.\d+)*)", text.strip())
        if m is None:
            return None
        return tuple(int(p) for p in m.group(1).split("."))

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

    @property
    def transient_test_supported(self) -> bool:
        return True

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
        return _floor_cycles(us) / SYS_CLK_MHZ

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
    def ramp_generator_for(ramp_dt_us: float, reset_time_us: float) -> str:
        """The generator the driver selects: 'fixed' whenever the requested
        reset quantizes to exactly the fixed-clock PIO's 5 us constant (any
        dt — zero jitter, one-cycle resolution, reset/settle independent
        of dt); otherwise 'pio' up to the 100 us threshold (dt/10-tick
        quantization), 'software' above it."""
        if _floor_cycles(reset_time_us) / SYS_CLK_MHZ == FIXED_RESET_US:
            return "fixed"
        return "pio" if ramp_dt_us <= RAMP_PIO_MAX_US else "software"

    def _validate_ramp_dt_us(self, ramp_dt_us: float) -> None:
        """Layer-1 dt check: 0 < dt <= RAMP_DT_MAX_US.  Raises ValueError
        with the reason (shown by the timing preview and Apply)."""
        if ramp_dt_us > RAMP_DT_MAX_US or ramp_dt_us <= 0:
            raise ValueError(
                f"ramp dt {ramp_dt_us:g} us is out of range "
                f"(0 < dt <= {RAMP_DT_MAX_US:g} us)"
            )

    @staticmethod
    def _pio_raw_counts(ramp_dt_us: float, int_cycles: int, reset_cycles: int):
        """The PIO generator's raw programming counts — the single source
        of truth shared by the exact mirror (calculate_ramp_timing) and
        configure_ramp's 'O' command, so the two can never drift apart.

        Returns (div_int, div_frac8, reset_pio, intervals, dt_eff_us);
        the firmware clamps a sub-tick reset phase to one dt/10 tick and a
        cycle-floored zero interval to one interval when the request
        covers a full dt (recompute_ramp_params), and so does the mirror."""
        clk = SYS_CLK_MHZ * 1e6
        # Divider: round UP to the next 1/256 step, and one step MORE
        # when the request lands exactly on a step — the applied dt is
        # strictly greater than requested, never faster.  A dt of exactly
        # 1.000 us (divider exactly 15 = exactly 1 MS/s) sits at the
        # acquisition card's spec limit and DAQmx warns when jitter tips
        # it over (200012): one 1/256 step = 0.26 ns here -> 999.74 kS/s,
        # safely under.  Same philosophy as the fixed/test generators.
        div_steps = math.ceil(ramp_dt_us * SYS_CLK_MHZ * 25.6 - 1e-9)
        if abs(div_steps - ramp_dt_us * SYS_CLK_MHZ * 25.6) < 1e-9:
            div_steps += 1                     # exact landing: strictly >
        div_steps = min(div_steps, 65535 * 256 + 255)     # 16.8 format cap
        div_int = div_steps // 256
        div_frac8 = div_steps % 256
        div_actual = div_steps / 256.0
        dt_eff = div_actual * 10.0e6 / clk
        intervals = int(int_cycles * 1e6 / (clk * ramp_dt_us))
        if intervals < 1 and int_cycles >= _floor_cycles(ramp_dt_us):
            # The request spans at least one whole dt (e.g. markers mode,
            # dt = int with a non-grid T_int): only the fractional cycle
            # is lost to flooring — clamp to one interval like the firmware.
            intervals = 1
        reset_pio = max(1, int(reset_cycles * 10.0e6 / (clk * ramp_dt_us)))
        return div_int, div_frac8, reset_pio, intervals, dt_eff

    @staticmethod
    def _fixed_sm_raw_counts(ramp_dt_us: float, int_cycles: int):
        """The fixed-clock generator's raw programming counts — the single
        source of truth shared by the exact mirror (calculate_ramp_timing)
        and configure_ramp's 'F' command, so the two can never drift apart
        (the B35 rule).

        dt rounds UP to the generator's (2·half + overhead)-cycle grid:
        the grid holds only odd cycle counts, so an even request (e.g.
        dt = 1 us = 150 cycles) is unreachable — flooring it to 149 made
        the actual rate FASTER than requested and clocked the NI card
        above its 1 MS/s spec (DAQmx warning 200012); rounding up keeps
        the applied dt never faster than requested (1 us -> 151 cycles =
        1.0067 us, 993.4 kS/s).  The firmware's recompute_ramp_params
        clamps a cycle-floored zero interval to one when the request
        covers a full dt (markers mode), and so does the mirror.

        Returns (half, intervals, dt_eff_us)."""
        dt_cycles = max(FIXED_MIN_DT_CYCLES, _floor_cycles(ramp_dt_us))
        half = max(1, (dt_cycles - FIXED_PULSE_OVERHEAD_CYCLES + 1) // 2)  # ceil
        dt_eff_cycles = 2 * half + FIXED_PULSE_OVERHEAD_CYCLES
        dt_eff = dt_eff_cycles / SYS_CLK_MHZ
        intervals = int_cycles // dt_eff_cycles
        # markers clamp compares against the REQUESTED cycle count: with
        # round-up dt_eff can exceed it by one cycle (dt = T_int on an
        # unreachable grid point), and the request still covers a full dt
        # — same rule as _pio_raw_counts below.
        if intervals < 1 and int_cycles >= dt_cycles:
            intervals = 1
        return half, intervals, dt_eff

    @staticmethod
    def _transient_raw_counts(reset_time_us: float, int_cycles: int):
        """The transient-test generator's raw programming counts and exact
        timing — the single source of truth shared by the mirror
        (calculate_transient_timing) and configure_transient_test's 'A'
        command.  dt is pinned by the TRANSIENT_DIV divider (1.0667 us);
        the reset rounds UP to whole ~107 ns ticks (never shorter than
        requested); the settle is one tick.

        Returns (reset_ticks, intervals, reset_eff_us, dt_eff_us,
        period_us); reset_ticks is the FULL reset-LOW phase (the firmware
        pushes reset_ticks - 2)."""
        div_actual = float(TRANSIENT_DIV)               # integer: zero jitter
        tick = div_actual / SYS_CLK_MHZ                  # ~0.1067 us at 150 MHz
        dt_eff = 10.0 * tick                             # one 10-cycle pulse
        dt_sys_cycles = 10.0 * div_actual                # dt in sys cycles

        reset_ticks = max(2, math.ceil(reset_time_us / tick))
        intervals = int(int_cycles // dt_sys_cycles)
        if intervals < 1 and int_cycles >= _floor_cycles(TRANSIENT_DT_US):
            intervals = 1
        reset_eff = reset_ticks * tick
        period = reset_eff + TRANSIENT_SETTLE_TICKS * tick + (intervals + 1) * dt_eff
        return reset_ticks, intervals, reset_eff, dt_eff, period

    @staticmethod
    def _transient_reset_raw_counts(pulsed_us: float, silent_us: float):
        """The reset-sampling generator's raw programming counts and exact
        timing — shared by the mirror and configure's 'B' command.  The
        pulsed phase (reset LOW, sampled) lasts exactly rp * dt; the
        silent integration phase is counted in ~107 ns ticks.  Both round
        UP (never shorter than requested).

        Returns (rp, it_ticks, pulsed_eff_us, silent_eff_us, dt_eff_us,
        period_us)."""
        div_actual = float(TRANSIENT_DIV)               # integer: zero jitter
        tick = div_actual / SYS_CLK_MHZ                  # ~0.1067 us at 150 MHz
        dt_eff = 10.0 * tick                             # one 10-cycle pulse

        rp = max(1, math.ceil(pulsed_us / dt_eff))
        it_ticks = max(TRANSIENT_RESET_MIN_INT_TICKS, math.ceil(silent_us / tick))
        pulsed_eff = rp * dt_eff
        silent_eff = it_ticks * tick
        return rp, it_ticks, pulsed_eff, silent_eff, dt_eff, pulsed_eff + silent_eff

    def calculate_ramp_timing(
        self, ramp_dt_us: float, int_time_us: float, reset_time_us: float
    ) -> ActualTiming:
        """Exact mirror of what the firmware will generate for a ramp
        configuration — the same numbers its 'Q' command reports (int =
        first-to-last CONV_START edge, reset = RESET-LOW duration).  The
        UI shows these values; configure_ramp() programs exactly this.

        The returned :class:`ActualTiming` carries the applied window
        geometry — ``samples_per_window`` (the CONV_START edges per
        integration window) and ``ramp_dt_eff_us`` (the effective pulse
        spacing).  The whole ramp path downstream consumes these instead
        of re-deriving them from the quantized times: re-deriving is
        lossy and the drift silently misaligns the window grid.

        Raises ValueError when the request cannot be represented
        (int < dt: the minimum integration is one interval = 2 pulses; or
        dt beyond the time range — see _validate_ramp_dt_us).
        """
        self._validate_ramp_dt_us(ramp_dt_us)
        generator = self.ramp_generator_for(ramp_dt_us, reset_time_us)
        int_cycles = _floor_cycles(int_time_us)
        reset_cycles = _floor_cycles(reset_time_us)

        if generator == "fixed":
            # Fixed-clock PIO: reset/settle are the program's 5 us / 2 us
            # immediates; dt rounds UP to the (2*half + 7)-cycle grid
            # (never faster than requested) — the same one-system-clock-
            # cycle granularity as the software generator, with zero
            # jitter and no dt upper bound.
            half, intervals, dt_eff = self._fixed_sm_raw_counts(
                ramp_dt_us, int_cycles
            )
            if intervals < 1:
                raise ValueError(
                    f"int={int_time_us:g} us is below one dt={ramp_dt_us:g} us "
                    "interval: the minimum integration is dt (2 pulses)"
                )
            act_int = intervals * dt_eff           # first-to-last edge
            act_reset = FIXED_RESET_US             # RESET-LOW duration
            settle = FIXED_SETTLE_US
            period = act_reset + settle + (intervals + 1) * dt_eff
        elif generator == "pio":
            div_int, div_frac8, reset_pio, intervals, dt_eff = self._pio_raw_counts(
                ramp_dt_us, int_cycles, reset_cycles
            )
            if intervals < 1:
                raise ValueError(
                    f"int={int_time_us:g} us is below one dt={ramp_dt_us:g} us "
                    "interval: the minimum integration is dt (2 pulses)"
                )
            act_int = (intervals) * dt_eff           # first-to-last edge
            act_reset = reset_pio * dt_eff / 10.0    # RESET-LOW duration
            settle = RAMP_SETTLE_PIO_CYCLES * dt_eff / 10.0
            period = act_reset + settle + (intervals + 1) * dt_eff
        else:
            dt_cycles = max(1, _floor_cycles(ramp_dt_us))
            dt_eff = dt_cycles / SYS_CLK_MHZ
            intervals = int_cycles // dt_cycles
            if intervals < 1:
                raise ValueError(
                    f"int={int_time_us:g} us is below one dt={ramp_dt_us:g} us "
                    "interval: the minimum integration is dt (2 pulses)"
                )
            act_int = intervals * dt_eff
            act_reset = reset_cycles / SYS_CLK_MHZ
            settle = RAMP_SETTLE_SOFT_US
            # Firmware 2.4: the last pulse is a short high, then the
            # cycle wraps immediately — the train spans intervals*dt
            # plus min(dt/2, 1 us) instead of pulses*dt.
            half_cycles = max(1, dt_cycles // 2)
            last_high = min(
                half_cycles, int(RAMP_SOFT_LAST_HIGH_US * SYS_CLK_MHZ)
            ) / SYS_CLK_MHZ
            period = act_reset + settle + intervals * dt_eff + last_high

        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=act_int,
            reset_time_us=act_reset,
            period_us=period,
            sample_frequency_hz=freq,
            samples_per_window=intervals + 1,
            ramp_dt_eff_us=dt_eff,
        )

    def calculate_transient_timing(
        self, int_time_us: float, reset_time_us: float
    ) -> ActualTiming:
        """Exact mirror of the transient-test generator — the same numbers
        its 'Q' command reports (int = first-to-last edge, reset = the
        RESET-LOW tick phase).  dt is pinned to TRANSIENT_DT_US
        (1.0667 us — nominal "1 us", derated below the acquisition card's
        1 MS/s spec); the reset may go down to TRANSIENT_RESET_MIN_US
        (1 us — deliberately below the validated measurement minimum:
        this is the study mode).  The returned geometry feeds the ramp
        path verbatim like every other generator's mirror.

        Raises ValueError when the request cannot be represented (int
        below one dt interval, or int/reset outside their ranges)."""
        if not (TRANSIENT_RESET_MIN_US <= reset_time_us <= RAMP_DT_MAX_US):
            raise ValueError(
                f"reset time {reset_time_us:g} us is out of the transient "
                f"test range ({TRANSIENT_RESET_MIN_US:g} .. {RAMP_DT_MAX_US:g} us)"
            )
        if not (5.0 <= int_time_us <= TRANSIENT_TEST_INT_MAX_US):
            raise ValueError(
                f"integration time {int_time_us:g} us is out of the "
                "transient test range (5 us .. "
                f"{TRANSIENT_TEST_INT_MAX_US:g} us)"
            )
        int_cycles = _floor_cycles(int_time_us)
        reset_ticks, intervals, reset_eff, dt_eff, period = self._transient_raw_counts(
            reset_time_us, int_cycles
        )
        if intervals < 1:
            raise ValueError(
                f"int={int_time_us:g} us is below one dt={TRANSIENT_DT_US:g} us "
                "interval: the minimum integration is dt (2 pulses)"
            )
        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=intervals * dt_eff,
            reset_time_us=reset_eff,
            period_us=period,
            sample_frequency_hz=freq,
            samples_per_window=intervals + 1,
            ramp_dt_eff_us=dt_eff,
        )

    def configure_transient_test(
        self, int_time_us: float, reset_time_us: float
    ) -> bool:
        """Configure the transient-test generator: dt pinned to
        TRANSIENT_DT_US (1.0667 us — nominal "1 us"), the reset sweepable
        from 1 us, a one-tick (~107 ns) settle before the first edge.
        Programs the mirror's raw counts verbatim ('A') and verifies via
        'Q' exactly like the other application paths."""
        try:
            actual = self.calculate_transient_timing(int_time_us, reset_time_us)
        except ValueError as exc:
            logger.error("transient test rejected: %s", exc)
            return False
        if actual.reset_time_us != reset_time_us:
            logger.info(
                "transient test quantization: reset %s -> %s us "
                "(%.0f ns ticks)",
                reset_time_us, actual.reset_time_us,
                TRANSIENT_DIV / SYS_CLK_MHZ * 1e3,
            )
        if not self._send_command("XR"):
            return False
        if not self._send_command("GT"):
            return False
        int_cycles = _floor_cycles(int_time_us)
        reset_ticks, intervals, reset_eff, dt_eff, period = self._transient_raw_counts(
            reset_time_us, int_cycles
        )
        if not self._send_command(f"A {reset_ticks},{intervals + 1}"):
            return False
        self._period_s = actual.period_us / 1e6
        return self._verify_applied_ramp(actual, "transient", dt_eff)

    def calculate_transient_reset_timing(
        self, int_time_us: float, reset_time_us: float
    ) -> ActualTiming:
        """Exact mirror of the reset-sampling transient generator — the
        same numbers its 'Q' command reports, with the app's fields
        SWAPPED: ``int_time_us`` (the Integration Time field) drives the
        SAMPLED pulsed phase — the reset LOW, lasting exactly rp * dt
        (dt = TRANSIENT_DT_US, 1.0667 us) with one edge per pulse period
        from the assertion — and ``reset_time_us`` (the Reset Time field)
        the SILENT integration phase (~107 ns ticks).  The returned
        ``samples_per_window`` is the pulse count and feeds the ramp path
        verbatim.

        Raises ValueError when the request cannot be represented."""
        if not (TRANSIENT_RESET_PULSED_MIN_US <= int_time_us
                <= TRANSIENT_TEST_INT_MAX_US):
            raise ValueError(
                f"pulsed reset phase {int_time_us:g} us (Integration Time "
                f"field) is out of the transient test range "
                f"({TRANSIENT_RESET_PULSED_MIN_US:g} .. "
                f"{TRANSIENT_TEST_INT_MAX_US:g} us)"
            )
        # The silent phase is validated against the TEST range (1 us floor),
        # not the measurement integration limits: in this study mode a
        # short silent phase just repeats the sampled reset window sooner,
        # and the firmware accepts down to 3 ticks (300 ns).
        if not (TRANSIENT_RESET_MIN_US <= reset_time_us
                <= TRANSIENT_TEST_SILENT_MAX_US):
            raise ValueError(
                f"silent integration phase {reset_time_us:g} us (Reset Time "
                f"field) is out of the transient test range "
                f"({TRANSIENT_RESET_MIN_US:g} .. "
                f"{TRANSIENT_TEST_SILENT_MAX_US:g} us)"
            )
        rp, it_ticks, pulsed_eff, silent_eff, dt_eff, period = (
            self._transient_reset_raw_counts(int_time_us, reset_time_us)
        )
        freq = 1e6 / period if period > 0 else 0.0
        return ActualTiming(
            int_time_us=pulsed_eff,          # the app's Integration field
            reset_time_us=silent_eff,        # the app's Reset field
            period_us=period,
            sample_frequency_hz=freq,
            samples_per_window=rp,
            ramp_dt_eff_us=dt_eff,
        )

    def configure_transient_reset_test(
        self, int_time_us: float, reset_time_us: float
    ) -> bool:
        """Configure the reset-sampling transient generator (fields
        swapped: int field = pulsed reset phase, reset field = silent
        integration).  Programs the mirror's raw counts verbatim ('B')
        and verifies via 'Q' (fields compared swapped, pulses exact)."""
        try:
            actual = self.calculate_transient_reset_timing(int_time_us, reset_time_us)
        except ValueError as exc:
            logger.error("reset-sampling transient test rejected: %s", exc)
            return False
        if not self._send_command("XR"):
            return False
        if not self._send_command("GR"):
            return False
        rp, it_ticks, pulsed_eff, silent_eff, dt_eff, period = (
            self._transient_reset_raw_counts(int_time_us, reset_time_us)
        )
        if not self._send_command(f"B {rp},{it_ticks}"):
            return False
        self._period_s = actual.period_us / 1e6
        return self._verify_applied_ramp(actual, "transreset", dt_eff)

    def configure_ramp(self, ramp_dt_us: float, int_time_us: float, reset_time_us: float, period_us: float = None) -> bool:
        if not self._validate_timing_us(int_time_us, reset_time_us):
            return False
        try:
            self._validate_ramp_dt_us(ramp_dt_us)
        except ValueError as exc:
            logger.error("ramp configuration rejected: %s", exc)
            return False

        # The exact mirror decides representability and the applied values;
        # the firmware is then programmed to reproduce precisely these.
        try:
            actual = self.calculate_ramp_timing(ramp_dt_us, int_time_us, reset_time_us)
        except ValueError as exc:
            logger.error("ramp configuration rejected: %s", exc)
            return False
        generator = self.ramp_generator_for(ramp_dt_us, reset_time_us)
        if actual.int_time_us != int_time_us or actual.reset_time_us != reset_time_us:
            logger.info(
                "ramp quantization (%s generator): int %s -> %s us, reset "
                "%s -> %s us (dt=%s us)",
                generator, int_time_us, actual.int_time_us,
                reset_time_us, actual.reset_time_us, ramp_dt_us,
            )

        if not self._send_command("XR"):
            return False
        if not self._send_command(
            {"pio": "GP", "software": "GC", "fixed": "GF"}[generator]
        ):
            return False

        if generator == "pio":
            # The application path: the mirror's exact raw counts are
            # programmed verbatim ('O') — no firmware-side rounding can
            # disagree with what the host previewed.
            int_cycles = _floor_cycles(int_time_us)
            reset_cycles = _floor_cycles(reset_time_us)
            div_int, div_frac8, reset_pio, intervals, dt_eff = self._pio_raw_counts(
                ramp_dt_us, int_cycles, reset_cycles
            )
            if not self._send_command(
                f"O {div_int},{div_frac8},{reset_pio},{intervals + 1}"
            ):
                return False
        elif generator == "fixed":
            # Same philosophy on the fixed-clock generator: the mirror's
            # raw counts go in verbatim ('F') — half-period and pulses.
            int_cycles = _floor_cycles(int_time_us)
            half, intervals, dt_eff = self._fixed_sm_raw_counts(
                ramp_dt_us, int_cycles
            )
            if not self._send_command(f"F {half},{intervals + 1}"):
                return False
        else:
            # Software generator: plain microseconds (T floors them, P is
            # cycle-floored) — nothing derived to drift.
            dt_eff = actual.ramp_dt_eff_us
            if not self._send_command(f"T {int_time_us},{reset_time_us}"):
                return False
            if not self._send_command(f"P {ramp_dt_us}"):
                return False

        self._period_s = actual.period_us / 1e6
        return self._verify_applied_ramp(actual, generator, dt_eff)

    def _verify_applied_ramp(self, expected: ActualTiming, generator: str, dt_eff: float) -> bool:
        """One-time check after configure: the firmware's 'Q' status must
        report exactly the timing we computed (it is never queried for the
        UI display — those values come from calculate_ramp_timing).

        On the PIO and fixed paths the pulse count is verified too: 'O'
        and 'F' program it verbatim, so any disagreement means the
        generator will emit a different number of CONV_START edges per
        window than the ramp sink chunks on — the alignment bug class.
        (The software path skips the pulses check: the 2.1 firmware
        derives the pre-run 'Q' display pulses from the un-floored dt
        while the engine itself — and this mirror — floor dt to whole
        cycles first; the engine overwrites the display at run start.)"""
        line = self._transact_line("Q")
        if not line:
            logger.warning("ramp verification skipped: no Q reply")
            return True
        try:
            fields = dict(
                part.split("=", 1) for part in line.split()[1:] if "=" in part
            )
            reported_gen = {"pio": "pio", "c": "software", "fpio": "fixed",
                            "tpio": "transient",
                            "rpio": "transreset"}.get(fields["gen"])
            # Software-generator tolerance on the pre-run 'int' display:
            # the 2.1 firmware derives the idle 'Q' pulse count from the
            # un-floored dt while the engine (and this mirror) floor dt to
            # whole cycles first — on interval-boundary configs the
            # display can legitimately lag one interval behind what the
            # engine will generate.  The engine's behaviour is the mirror,
            # so tolerating the display artifact cannot hide a real
            # misconfiguration.  The PIO path ('O' programs the counts
            # verbatim) and the fixed path ('F' likewise) stay exact.
            int_tol = dt_eff + 0.01 if generator == "software" else 0.01
            if generator == "transreset":
                # The app's fields are swapped in this mode: Q's int is
                # the SILENT phase (= the mirror's reset_time_us, the
                # Reset Time field) and Q's reset is the PULSED phase
                # (= the mirror's int_time_us, the Integration field).
                ok = (
                    reported_gen == generator
                    and abs(float(fields["int"]) - expected.reset_time_us) <= 0.01
                    and abs(float(fields["reset"]) - expected.int_time_us) <= 0.01
                    and abs(float(fields["dt"]) - dt_eff) <= 0.01
                    and int(fields["pulses"]) == expected.samples_per_window
                )
            else:
                ok = (
                    reported_gen == generator
                    and abs(float(fields["int"]) - expected.int_time_us) <= int_tol
                    and abs(float(fields["reset"]) - expected.reset_time_us) <= 0.01
                    and abs(float(fields["dt"]) - dt_eff) <= 0.01
                )
                if ok and generator in ("pio", "fixed", "transient"):
                    ok = int(fields["pulses"]) == expected.samples_per_window
            if not ok:
                logger.error(
                    "ramp verification FAILED: expected gen=%s int=%.4f us "
                    "reset=%.4f us dt=%.4f us pulses=%s, firmware Q reports %s",
                    generator, expected.int_time_us, expected.reset_time_us,
                    dt_eff, expected.samples_per_window, line,
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
