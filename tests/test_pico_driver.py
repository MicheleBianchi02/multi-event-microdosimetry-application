"""
Protocol-level tests for the PicoSerialDAQ driver against a fake serial
port that speaks the PICO2DAQ 2.2 firmware protocol (see
raspberry_copy/README.md): one-byte command acks, the 'V' identity line,
the 'Q' status line, and the binary stream terminated by the
end-of-stream marker 0xFFFFFFFF followed by the 'D' stop ack.
"""
import logging
import time
import types

import pytest

from core.constants import overflow_sentinel
from hardware.pico.pico_daq import PicoSerialDAQ, STREAM_MARKER, SYS_CLK_MHZ

# App-side missing-channel marker the driver fills for the unacquired CH2:
# one past the 12-bit full scale (mapped to NaN by the conversion layer).
MISSING = overflow_sentinel(12)

IDENTITY = b"V PICO2DAQ 2.5 F4D87D3B6FBAEF13 150\n"
STATUS = (b"Q run=0 mode=noramp int=1000.000 reset=20.000 dt=10.000 "
          b"caps=000 event=ME dropped=0\n")


def word(start, stop):
    return ((start & 0xFFF) << 16) | (stop & 0xFFF)


class FakeSerialPort:
    """Minimal pyserial stand-in implementing the firmware's replies."""

    def __init__(self, identity=IDENTITY, status=STATUS,
                 data_words=(), fail_commands=(), drain_words=()):
        self.port = None
        self.baudrate = 115200
        self.timeout = 0.1
        self.is_open = False
        self._rx = bytearray()
        self._writes = []
        self._identity = identity
        self._status = status
        self._data_words = list(data_words)
        self._fail_commands = set(fail_commands)
        self._drain_words = list(drain_words)  # words flushed by the stop drain
        self._running = False

    # -- introspection for assertions --
    @property
    def commands(self):
        return [w.decode("ascii").strip() for w in self._writes]

    # -- pyserial API --
    def open(self):
        self.is_open = True
        self._rx.clear()

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self._rx.clear()

    def write(self, data):
        self._writes.append(bytes(data))
        self._handle(bytes(data).decode("ascii").strip())

    def read(self, size=1):
        n = min(size, len(self._rx))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    @property
    def in_waiting(self):
        return len(self._rx)

    # -- firmware behaviour --
    def _handle(self, line):
        if line in self._fail_commands:
            self._rx += b"E"
        elif line == "V":
            self._rx += self._identity
        elif line == "Q":
            self._rx += self._status
        elif line == "S":
            self._rx += b"D"
            self._running = True
            for w in self._data_words:
                self._rx += w.to_bytes(4, "little")
        elif line == "E":
            if self._running:
                # residual stream words, end-of-stream marker, then the ack
                for w in self._drain_words:
                    self._rx += w.to_bytes(4, "little")
                self._rx += STREAM_MARKER.to_bytes(4, "little") + b"D"
                self._running = False
            else:
                self._rx += b"D"
        else:
            # XN/XR/T/P/C.../RS/RM all ack D
            self._rx += b"D"


class StubDataModel:
    def __init__(self):
        self.batches = []

    def append_batch(self, batch):
        self.batches.append(list(batch))

    @property
    def samples(self):
        return [s for b in self.batches for s in b]


def make_driver(fake):
    driver = PicoSerialDAQ(port="/dev/fake")
    driver.serial = fake
    return driver


@pytest.fixture
def fake():
    return FakeSerialPort()


@pytest.fixture
def driver(fake):
    d = make_driver(fake)
    yield d
    if d.is_measuring:
        d.stop_measurement()


# ----------------------------------------------------------------------
# Connection / identity liveness check
# ----------------------------------------------------------------------

def test_connect_sends_identity_handshake(fake):
    d = make_driver(fake)
    assert d.connect() is True
    assert fake.commands == ["V"]
    assert d.identity["firmware"] == "PICO2DAQ"
    assert d.identity["board_id"] == "F4D87D3B6FBAEF13"
    assert d.identity["clk_mhz"] == 150


def test_connect_refuses_wrong_firmware():
    d = make_driver(FakeSerialPort(identity=b"V OTHERFW 1.0 XX 150\n"))
    assert d.connect() is False
    assert d.serial.is_open is False  # port closed again


def test_connect_refuses_clock_mismatch():
    d = make_driver(FakeSerialPort(identity=b"V PICO2DAQ 2.2 XX 125\n"))
    assert d.connect() is False


def test_connect_accepts_suffixed_test_firmware(caplog):
    """The inverted-reset bench build reports e.g. '2.5-test1': the base
    numbers pass the gate (connectable on purpose) and a loud warning
    marks it as a test board."""
    import logging

    d = make_driver(FakeSerialPort(identity=b"V PICO2DAQ 2.5-test1 XX 150\n"))
    with caplog.at_level(logging.WARNING):
        assert d.connect() is True
    assert d.identity["version"] == "2.5-test1"
    assert any("TEST firmware" in r.message for r in caplog.records)


def test_connect_refuses_unparsable_firmware():
    d = make_driver(FakeSerialPort(identity=b"V PICO2DAQ beta XX 150\n"))
    assert d.connect() is False


@pytest.mark.parametrize("old", ["2.1", "2.2", "2.3", "2.4"])
def test_connect_refuses_old_firmware(old):
    # 2.1 predates 'GF'/'F'; 2.2 predates 'GT'/'A' and ran the 1 us
    # settle; 2.3 predates the software generator's short last pulse;
    # 2.4 carried the stale-reset-anchor bug (reset ran ~dt): refused
    # either way, reflash
    d = make_driver(FakeSerialPort(
        identity=f"V PICO2DAQ {old} XX 150\n".encode()))
    assert d.connect() is False
    assert d.serial.is_open is False


def test_connect_refuses_silence():
    d = make_driver(FakeSerialPort(identity=b""))
    assert d.connect() is False


def test_reconnect_does_not_repeat_identity(fake):
    d = make_driver(fake)
    assert d.connect()
    assert d._send_command("RM")
    assert fake.commands == ["V", "RM"]  # one V, ever


# ----------------------------------------------------------------------
# Gain / event mode commands
# ----------------------------------------------------------------------

def test_gpio_mask_framing(fake, driver):
    driver.set_connection_params("/dev/fake", 115200)
    assert driver.set_gpio_mask([True, False, True]) is True
    assert fake.commands == ["V", "C101"]


def test_gpio_mask_rejects_wrong_length(fake, driver):
    assert driver.set_gpio_mask([True, False]) is False
    assert fake.commands == []  # rejected before any serial traffic


def test_event_mode_commands(fake, driver):
    assert driver.set_event_mode(True) is True   # SE
    assert driver.set_event_mode(False) is True  # ME
    assert fake.commands == ["V", "RS", "RM"]


# ----------------------------------------------------------------------
# Timing configuration and quantization
# ----------------------------------------------------------------------

def test_configure_noramp_sequence(fake, driver):
    assert driver.configure_noramp(100.0, 20.0) is True
    assert fake.commands == ["V", "XN", "T 100.0,20.0"]


def test_configure_ramp_sequence(fake, driver):
    # dt=50 -> PIO generator (<= 100 us). div = 150*50/10 = 750.0;
    # int 1000 -> intervals = floor(1000/50) = 20 -> 21 pulses;
    # reset 100 -> reset_pio = floor(10*100/50) = 20 (LOW duration).
    # Q verification follows (STATUS has int=1000/reset=20 -> mismatch vs
    # the applied int=1000 -> configure returns False; use a matching Q).
    fake._status = (b"Q run=0 mode=ramp gen=pio int=1000.000 reset=100.000 "
                    b"dt=50.000 div=750.000 pulses=21 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(50.0, 1000.0, 100.0) is True
    # divider 750 exact -> one 1/256 step up (dt strictly > requested)
    assert fake.commands == ["V", "XR", "GP", "O 750,1,20,21", "Q"]


def test_configure_ramp_selects_software_generator_above_threshold(fake, driver):
    # dt=200 -> software generator: plain T + P, no O, gen field 'c'
    fake._status = (b"Q run=0 mode=ramp gen=c int=1000.000 reset=100.000 "
                    b"dt=200.000 div=0.000 pulses=6 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(200.0, 1000.0, 100.0) is True
    assert fake.commands == ["V", "XR", "GC", "T 1000.0,100.0", "P 200.0", "Q"]


def test_calculate_ramp_timing_pio_mirror():
    d = make_driver(FakeSerialPort())
    # dt=50, int=1000, reset=100: div would be exactly 750 -> one 1/256
    # step up (dt strictly > requested, never faster than the card's
    # rate spec): dt_eff = 750.00390625/15 = 50.0002604; intervals=20;
    # reset_pio=20 -> 2*dt_eff low; settle = 20 cycles = 2*dt_eff
    dt_eff = (750 * 256 + 1) / 256 / 15.0
    actual = d.calculate_ramp_timing(50.0, 1000.0, 100.0)
    assert actual.int_time_us == pytest.approx(20 * dt_eff)
    assert actual.reset_time_us == pytest.approx(2 * dt_eff)
    assert actual.period_us == pytest.approx(2 * dt_eff + 2 * dt_eff + 21 * dt_eff)
    assert actual.sample_frequency_hz == pytest.approx(1e6 / (25 * dt_eff))


def test_calculate_ramp_timing_software_mirror():
    d = make_driver(FakeSerialPort())
    # dt=1000 (above the PIO threshold): everything floors to cycles
    actual = d.calculate_ramp_timing(1000.0, 3000.0, 500.0)
    assert actual.int_time_us == pytest.approx(3000.0)
    assert actual.reset_time_us == pytest.approx(500.0)
    # settle 2 us + intervals*dt (3*1000) + last pulse's short high
    # (min(500, 1) = 1 us, firmware 2.4: the cycle wraps right after the
    # last edge instead of running the last pulse's low half out)
    assert actual.period_us == pytest.approx(500.0 + 2.0 + 3000.0 + 1.0)


def test_calculate_ramp_timing_rejects_int_below_dt():
    d = make_driver(FakeSerialPort())
    with pytest.raises(ValueError):
        d.calculate_ramp_timing(50.0, 30.0, 100.0)   # int < dt (PIO range)
    with pytest.raises(ValueError):
        d.calculate_ramp_timing(1000.0, 500.0, 100.0)  # int < dt (soft range)


def test_configure_ramp_rejects_int_below_dt(fake, driver):
    assert driver.configure_ramp(50.0, 30.0, 100.0) is False
    assert fake.commands == []  # rejected before any serial traffic


def test_configure_ramp_verifies_via_q(fake, driver):
    # firmware Q disagrees with the driver's mirror -> configure fails
    fake._status = (b"Q run=0 mode=ramp gen=pio int=900.000 reset=100.000 "
                    b"dt=50.000 div=750.000 pulses=21 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(50.0, 1000.0, 100.0) is False
    assert fake.commands == ["V", "XR", "GP", "O 750,1,20,21", "Q"]


def test_configure_ramp_verification_tolerates_missing_q(fake, driver):
    fake._status = b""  # no Q reply: skipped with a warning, still ok
    assert driver.configure_ramp(50.0, 1000.0, 100.0) is True


def test_ramp_generator_threshold():
    d = make_driver(FakeSerialPort())
    # reset above the fixed constant: the dt threshold rules apply
    assert d.ramp_generator_for(1.0, 100.0) == "pio"
    assert d.ramp_generator_for(100.0, 100.0) == "pio"
    assert d.ramp_generator_for(100.5, 100.0) == "software"
    assert d.ramp_generator_for(1000.0, 100.0) == "software"


def test_ramp_generator_fixed_when_reset_is_the_minimum():
    # reset = 5 us (quantized to the cycle grid) -> the fixed-clock
    # generator at ANY dt, including markers mode (dt = T_int)
    d = make_driver(FakeSerialPort())
    assert d.ramp_generator_for(1.0, 5.0) == "fixed"
    assert d.ramp_generator_for(50.0, 5.0) == "fixed"
    assert d.ramp_generator_for(100.0, 5.0) == "fixed"
    assert d.ramp_generator_for(10_000_000.0, 5.0) == "fixed"
    # anything one cycle-grid step above 5 us falls back to the old rules
    assert d.ramp_generator_for(50.0, 5.01) == "pio"
    assert d.ramp_generator_for(50.0, 6.0) == "pio"


def test_configure_ramp_quantizes_reset_to_pio_tick(fake, driver):
    # v2: reset = plain LOW duration, quantized to dt/10 ticks (no overhead
    # constraint).  7 us at dt=50 -> one 5-us tick.
    fake._status = (b"Q run=0 mode=ramp gen=pio int=1000.000 reset=5.000 "
                    b"dt=50.000 div=750.000 pulses=21 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(50.0, 1000.0, 7.0) is True
    actual = driver.calculate_ramp_timing(50.0, 1000.0, 7.0)
    assert actual.reset_time_us == pytest.approx(actual.ramp_dt_eff_us / 10)


def test_configure_rejects_out_of_limits_before_traffic(fake, driver):
    assert driver.configure_noramp(100.0, 4.9) is False  # reset below 5 us
    assert driver.configure_noramp(100.0, 5.0) is True   # boundary is legal
    assert driver.configure_noramp(4.0, 20.0) is False   # int below 5 us
    assert driver.configure_ramp(10000.0, 100.0, 20.0) is False  # dt > 4369
    driver.serial._rx.clear()


def test_configure_reports_firmware_rejection(fake, driver):
    fake._fail_commands.add("T 100.0,20.0")
    assert driver.configure_noramp(100.0, 20.0) is False


def test_calculate_actual_timing_mirrors_firmware_floor():
    d = make_driver(FakeSerialPort())
    # 20.01 us * 150 = 3001.5 cycles -> floor 3001 -> 20.006666... us
    actual = d.calculate_actual_timing(20.01, 10.0)
    assert actual.int_time_us == pytest.approx(3001 / SYS_CLK_MHZ)
    assert actual.reset_time_us == pytest.approx(1500 / SYS_CLK_MHZ)
    assert actual.period_us == pytest.approx(4501 / SYS_CLK_MHZ)
    assert actual.sample_frequency_hz == pytest.approx(1e6 / (4501 / SYS_CLK_MHZ))


def test_calculate_actual_timing_exact_values_unchanged():
    d = make_driver(FakeSerialPort())
    actual = d.calculate_actual_timing(100.0, 20.0)
    assert actual.int_time_us == 100.0
    assert actual.reset_time_us == 20.0
    assert actual.period_us == 120.0
    assert actual.sample_frequency_hz == pytest.approx(1e6 / 120.0)


# ----------------------------------------------------------------------
# Acquisition: start, stream, marker-terminated stop
# ----------------------------------------------------------------------

def run_and_stop(driver, words, debug=True):
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=debug)
    assert driver.start_measurement(session, data_model) is True
    assert driver.stop_measurement() is True
    return data_model


def test_start_stop_streams_all_samples(fake, driver):
    words = [word(100, 200), word(300, 250), word(4000, 4095)]
    fake._data_words = words
    data_model = run_and_stop(driver, words)
    assert data_model.samples == [
        ((100, 200), (MISSING, MISSING)),
        ((300, 250), (MISSING, MISSING)),
        ((4000, 4095), (MISSING, MISSING)),
    ]
    # stop consumed the marker and the ack
    assert fake.commands == ["V", "S", "E", "Q"]
    assert fake.in_waiting == 0
    assert not driver.is_measuring


def test_operational_mode_derives_diff(fake, driver):
    words = [word(100, 200), word(500, 400)]  # second: stop < start -> overflow
    fake._data_words = words
    data_model = run_and_stop(driver, words, debug=False)
    assert data_model.samples == [
        (100, MISSING),
        (MISSING, MISSING),
    ]


def test_reader_realigns_after_garbage_byte(fake, driver):
    # one stray byte before the stream: the reader drops it and realigns
    fake._data_words = [word(10, 20)]
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    # inject the stray byte after the S ack (already queued by the fake)
    driver.serial._rx.insert(1, 0xAB)
    assert driver.stop_measurement() is True
    assert data_model.samples == [((10, 20), (MISSING, MISSING))]
    assert driver._resync_drops == 1


def test_stop_without_marker_fails(fake, driver):
    fake._data_words = [word(1, 2)]

    def broken_eot(line):
        if line == "E":
            fake._rx += b"D"  # ack but no end-of-stream marker
            return True
        return False

    original_handle, fake._handle = fake._handle, None
    def handle(line):
        if broken_eot(line):
            return
        original_handle(line)
    fake._handle = handle

    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    assert driver.stop_measurement() is False  # marker missing -> failure


def test_commands_refused_while_measuring(fake, driver):
    fake._data_words = [word(1, 2)]
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    n_writes = len(fake._writes)
    assert driver.configure_noramp(100.0, 20.0) is False
    assert driver.set_gpio_mask([False, False, False]) is False
    assert len(fake._writes) == n_writes  # nothing was sent
    assert driver.stop_measurement() is True


def test_second_start_refused_while_measuring(fake, driver):
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    assert driver.start_measurement(session, data_model) is False
    assert driver.stop_measurement() is True


def test_stop_reports_firmware_dropped_samples(fake, driver, caplog):
    fake._status = STATUS.replace(b"dropped=0", b"dropped=7")
    assert driver.connect() is True
    with caplog.at_level(logging.WARNING):
        assert driver.stop_measurement() is True  # idle stop: still ok
    assert any("dropped 7" in r.message for r in caplog.records)


def test_q_diagnostics_after_real_stop(fake, driver, caplog):
    fake._data_words = [word(1, 2)]
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    fake._status = STATUS.replace(b"dropped=0", b"dropped=3")
    with caplog.at_level(logging.WARNING):
        assert driver.stop_measurement() is True
    assert any("dropped 3" in r.message for r in caplog.records)


# ----------------------------------------------------------------------
# Timing-only start/stop (virtual-acquisition bench mode)
# ----------------------------------------------------------------------

def test_start_timing_sends_s_without_reader(fake, driver):
    assert driver.start_timing() is True
    assert fake.commands == ["V", "S"]
    assert not driver.is_measuring  # no reader thread in timing-only mode


def test_stop_timing_drains_to_marker_and_ack(fake, driver):
    assert driver.start_timing() is True
    # unread stream left in the ring: flushed by the firmware on 'E', ahead
    # of the end-of-stream marker and the ack
    fake._drain_words = [word(1, 2), word(3, 4)]
    assert driver.stop_timing() is True
    assert driver._resync_drops == 0
    assert fake.in_waiting == 0


def test_stop_timing_fails_without_marker(fake, driver):
    assert driver.start_timing() is True
    fake._fail_commands.add("E")  # firmware stalled: no marker, no ack
    assert driver.stop_timing() is False


def test_start_timing_refused_while_reader_runs(fake, driver):
    fake._data_words = [word(1, 2)]
    data_model = StubDataModel()
    session = types.SimpleNamespace(debug_mode=True)
    assert driver.start_measurement(session, data_model) is True
    assert driver.start_timing() is False  # S while streaming: refused
    assert driver.stop_measurement() is True


# ----------------------------------------------------------------------
# ADC settings
# ----------------------------------------------------------------------

def test_adc_settings_and_fixed_range(fake, driver):
    settings = driver.get_adc_settings()
    assert settings["bits"] == 12
    assert settings["input_range_volts"] == {"min_volts": 0.0, "max_volts": 3.3}
    assert driver.set_input_range(0.0, 3.3) is True
    assert driver.set_input_range(0.0, 5.0) is False  # no scaler on this bench


# ----------------------------------------------------------------------
# Timing interface no-ops
# ----------------------------------------------------------------------

def test_interface_noops(fake, driver):
    assert driver.set_mode(True) is True
    assert driver.set_channels(False) is True
    assert driver.arm() is True
    assert fake.commands == []  # the Pico has no such commands
    assert driver.supports_ramp_sampling is True


# ----------------------------------------------------------------------
# HardwareManager routing: virtual acquisition starts the real timing
# ----------------------------------------------------------------------

def _bench_config_with_dummy_acquisition() -> dict:
    """Pico timing + gain, virtual acquisition (the scope-bench setup)."""
    from helpers import all_pico_config

    cfg = all_pico_config()
    del cfg["devices"]["pico_1"]["capabilities"]["acquisition"]
    cfg["devices"]["dummy_acq"] = {
        "name": "Virtual acquisition",
        "driver": "dummy_v1",
        "connection": {"type": "dummy"},
        "capabilities": {
            "acquisition": {
                "num_channels": 1,
                "adc": {
                    "bits": 12,
                    "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 3.3}],
                },
            }
        },
    }
    cfg["roles"]["acquisition"] = "dummy_acq"
    return cfg


def _load_with_fake_serial(tmp_path, cfg):
    import json as _json

    from hardware.core.hardware_manager import HardwareManager

    path = tmp_path / "bench.json"
    path.write_text(_json.dumps(cfg))
    manager = HardwareManager()
    manager.load_config(str(path))
    fake = FakeSerialPort()
    manager.devices["pico_1"].serial = fake
    return manager, fake


def test_virtual_acquisition_starts_the_real_timing(tmp_path):
    manager, fake = _load_with_fake_serial(
        tmp_path, _bench_config_with_dummy_acquisition()
    )
    assert manager.acquisition_is_virtual
    assert manager.start_timing_signal() is True
    assert "S" in fake.commands
    assert manager.stop_timing_signal() is True
    assert "E" in fake.commands


def test_all_in_one_timing_signal_is_a_noop(tmp_path):
    from helpers import all_pico_config

    manager, fake = _load_with_fake_serial(tmp_path, all_pico_config())
    assert not manager.acquisition_is_virtual
    # the acquisition start/stop owns S/E on the same physical device: the
    # signal helpers must not send a second (mid-run refused) command
    assert manager.start_timing_signal() is True
    assert fake.commands == []
    assert manager.stop_timing_signal() is True
    assert fake.commands == []


# ----------------------------------------------------------------------
# Ramp-dt bound: the software generator (the app path for dt > 100 us)
# is bounded only by the general time range — markers mode with long
# integrations (dt = T_int) and coarse-dt ramp sampling both work.
# ----------------------------------------------------------------------

def test_dt_beyond_the_time_range_rejected(fake, driver):
    # markers-mode shape with an absurd dt: above the 10 s range
    with pytest.raises(ValueError, match="out of range"):
        driver.calculate_ramp_timing(10_000_001.0, 10_000_001.0, 500.0)
    # and configure refuses BEFORE any serial traffic
    assert driver.configure_ramp(10_000_001.0, 10_000_001.0, 500.0) is False
    assert fake.commands == []


def test_dt_zero_or_negative_rejected(fake, driver):
    with pytest.raises(ValueError):
        driver.calculate_ramp_timing(0.0, 1000.0, 500.0)


def test_long_integration_markers_mirror(fake, driver):
    # dt = T_int = 100 ms: N = 2 pulses, software generator, settle 2 us.
    # Firmware 2.4: the last pulse holds only min(dt/2, 1 us) high and
    # wraps — the period is ~T_int, not the ~2*T_int of the old trailing
    # low half (the rate-doubling the bench measured).
    actual = driver.calculate_ramp_timing(100_000.0, 100_000.0, 500.0)
    assert actual.int_time_us == pytest.approx(100_000.0)
    assert actual.reset_time_us == pytest.approx(500.0)
    assert actual.period_us == pytest.approx(500.0 + 2.0 + 100_000.0 + 1.0)
    assert actual.samples_per_window == 2


def test_dt_within_pio_bound_still_fine(fake, driver):
    actual = driver.calculate_ramp_timing(50.0, 200.0, 20.0)
    assert actual.int_time_us > 0


# ----------------------------------------------------------------------
# Ramp window geometry (the alignment contract of the whole ramp path):
# the mirror is the single source of the pulses-per-window count and of
# the effective dt; configure_ramp programs exactly these.
# ----------------------------------------------------------------------

def _q_line(actual, generator, pulses):
    """Synthesize a firmware 'Q' reply reporting the mirror's values."""
    return (
        f"Q run=0 mode=ramp gen={generator} int={actual.int_time_us:.3f} "
        f"reset={actual.reset_time_us:.3f} dt={actual.ramp_dt_eff_us:.3f} "
        f"div=0.000 pulses={pulses} caps=000 event=ME dropped=0\n"
    ).encode()


def test_ramp_mirror_carries_window_geometry():
    """samples_per_window is the pulse count the generator emits,
    ramp_dt_eff_us the quantized dt — for both generators, including the
    non-grid dt whose lossy re-derivation drifted the window grid."""
    d = make_driver(FakeSerialPort())
    pio = d.calculate_ramp_timing(7.31, 100.0066, 50.0)
    assert pio.samples_per_window == 14          # intervals 13 + 1
    # divider rounds UP to the next 1/256 step: 7.31 -> 7.31015625
    assert pio.ramp_dt_eff_us == pytest.approx(28071 / 3840.0)

    soft = d.calculate_ramp_timing(300.0066, 20_000.0, 500.0)
    assert soft.samples_per_window == 67         # 66 intervals + 1
    assert soft.ramp_dt_eff_us == pytest.approx(300.0)


def test_markers_non_grid_tint_is_representable_on_the_pio_range():
    """dt = int with a non-grid T_int <= 100 us (markers on the PIO
    generator, reset above the fixed constant): cycle flooring must not
    reject the request — the firmware clamps the zero interval to one
    (2 pulses), and so must the mirror."""
    d = make_driver(FakeSerialPort())
    actual = d.calculate_ramp_timing(99.9934, 99.9934, 50.0)
    assert actual.samples_per_window == 2
    assert actual.int_time_us == pytest.approx(actual.ramp_dt_eff_us)


def test_subtick_reset_is_clamped_not_rejected():
    """A reset shorter than one dt/10 PIO tick degenerates to one tick
    (firmware recompute_ramp_params clamps it) — it is not an integration
    error, and the old message blamed the integration time."""
    d = make_driver(FakeSerialPort())
    actual = d.calculate_ramp_timing(50.0, 1000.0, 1.5)
    dt_eff = (750 * 256 + 1) / 256 / 15.0
    assert actual.reset_time_us == pytest.approx(dt_eff / 10)  # one tick
    assert actual.samples_per_window == 21


@pytest.mark.parametrize(
    "dt,int_us,reset_us",
    [
        (50.0, 1000.0, 100.0),       # grid dt, PIO
        (7.31, 100.0066, 50.0),      # non-grid dt + non-grid int (the drift case)
        (7.31, 10_000_000.0, 1000.0),  # long integration, huge pulse count
        (99.9934, 99.9934, 50.0),    # markers, non-grid, PIO range
        (200.0, 1000.0, 100.0),      # software generator
    ],
)
def test_configure_ramp_o_counts_match_the_mirror(fake, driver, dt, int_us, reset_us):
    """The 'O' command programs EXACTLY the mirror's geometry — the same
    numbers the preview showed.  Feeding the mirror quantized values a
    second time (what Apply used to do) loses one interval here."""
    actual = driver.calculate_ramp_timing(dt, int_us, reset_us)
    fake._status = _q_line(actual, "pio" if dt <= 100.0 else "c",
                           actual.samples_per_window)
    assert driver.configure_ramp(dt, int_us, reset_us) is True
    o_cmds = [c for c in fake.commands if c.startswith("O ")]
    if dt <= 100.0:
        assert len(o_cmds) == 1
        div_int, div_frac8, reset_pio, pulses = map(int, o_cmds[0][2:].split(","))
        assert pulses == actual.samples_per_window
        assert div_int + div_frac8 / 256.0 == pytest.approx(
            actual.ramp_dt_eff_us * 150.0 / 10.0)
    else:
        # software generator: plain T + P, no O command
        assert o_cmds == []
        assert "P 200.0" in fake.commands


@pytest.mark.parametrize(
    "dt,int_us",
    [
        (50.0, 1000.0),            # regular ramp sampling on the fixed generator
        (7.31, 10_000_000.0),      # long integration, huge pulse count
        (99.9934, 99.9934),        # markers, non-grid T_int (the clamp case)
        (100_000.0, 100_000.0),    # markers with a long integration
    ],
)
def test_configure_ramp_f_counts_match_the_mirror(fake, driver, dt, int_us):
    """The 'F' command programs EXACTLY the mirror's geometry on the
    fixed-clock generator (reset = 5 us): the half-period and pulse
    counts must be the same numbers the preview showed — the B35 rule
    shared with the 'O' path."""
    actual = driver.calculate_ramp_timing(dt, int_us, 5.0)
    fake._status = _q_line(actual, "fpio", actual.samples_per_window)
    assert driver.configure_ramp(dt, int_us, 5.0) is True
    f_cmds = [c for c in fake.commands if c.startswith("F ")]
    assert len(f_cmds) == 1
    half, pulses = map(int, f_cmds[0][2:].split(","))
    assert pulses == actual.samples_per_window
    # dt_eff = (2*half + 7) / 150 MHz — the programmed count IS the mirror
    assert (2 * half + 7) / SYS_CLK_MHZ == pytest.approx(actual.ramp_dt_eff_us)


def test_calculate_ramp_timing_fixed_mirror():
    d = make_driver(FakeSerialPort())
    # dt=50 -> 7500 cycles (even, unreachable on the odd-only grid) ->
    # rounded UP: half=(7500-7+1)//2=3747 -> dt_eff=7501 cycles; int=1000
    # -> 150000 cycles -> 19 intervals; reset = the 5 us immediate,
    # settle 2 us.
    actual = d.calculate_ramp_timing(50.0, 1000.0, 5.0)
    assert actual.int_time_us == pytest.approx(19 * 7501 / SYS_CLK_MHZ)
    assert actual.reset_time_us == pytest.approx(5.0)
    assert actual.ramp_dt_eff_us == pytest.approx(7501 / SYS_CLK_MHZ)
    assert actual.period_us == pytest.approx(5.0 + 2.0 + 20 * 7501 / SYS_CLK_MHZ)
    assert actual.samples_per_window == 20


def test_fixed_generator_rounds_up_never_faster_than_requested():
    """The odd-only (2·half + 7) grid cannot hit an even cycle count:
    dt = 1 us = 150 cycles floored to 149 clocked the NI card above its
    1 MS/s spec (DAQmx 200012) — rounding up to 151 keeps the applied dt
    never faster than requested."""
    d = make_driver(FakeSerialPort())
    actual = d.calculate_ramp_timing(1.0, 1000.0, 5.0)
    assert actual.ramp_dt_eff_us == pytest.approx(151 / SYS_CLK_MHZ)  # 1.0067 us
    assert actual.ramp_dt_eff_us >= 1.0
    # a dt already on the grid is untouched
    on_grid = d.calculate_ramp_timing(151 / SYS_CLK_MHZ, 1000.0, 5.0)
    assert on_grid.ramp_dt_eff_us == pytest.approx(151 / SYS_CLK_MHZ)


def test_configure_ramp_fixed_path_sequence(fake, driver):
    # dt=50, int=1000, reset=5 -> fixed generator: XR + GF + F, verified
    fake._status = (b"Q run=0 mode=ramp gen=fpio int=950.127 reset=5.000 "
                    b"dt=50.007 div=1.000 pulses=20 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(50.0, 1000.0, 5.0) is True
    assert fake.commands == ["V", "XR", "GF", "F 3747,20", "Q"]


def test_configure_ramp_fixed_verifies_pulses_via_q(fake, driver):
    """A pulse-count disagreement on the fixed path is a hard configure
    failure, exactly like the PIO path: the generator would emit a
    different number of CONV_START edges per window than the ramp sink
    chunks on."""
    actual = driver.calculate_ramp_timing(50.0, 1000.0, 5.0)
    fake._status = _q_line(actual, "fpio", actual.samples_per_window - 1)
    assert driver.configure_ramp(50.0, 1000.0, 5.0) is False


def test_markers_long_integration_on_the_fixed_generator():
    """Markers mode with reset = 5 us: the fixed generator serves dt =
    T_int = 100 ms (no dt-coupled dead time, zero CPU load) with the two
    boundary edges per window."""
    d = make_driver(FakeSerialPort())
    actual = d.calculate_ramp_timing(100_000.0, 100_000.0, 5.0)
    assert actual.samples_per_window == 2
    assert actual.int_time_us == pytest.approx(actual.ramp_dt_eff_us)
    assert actual.reset_time_us == pytest.approx(5.0)
    # period = 7 us of reset+settle + the two boundary edges
    assert actual.period_us == pytest.approx(7.0 + 2 * actual.ramp_dt_eff_us)


def test_configure_ramp_verifies_pulses_via_q(fake, driver):
    """A pulse-count disagreement is a hard configure failure: the
    generator would emit a different number of CONV_START edges per
    window than the ramp sink chunks on."""
    actual = driver.calculate_ramp_timing(50.0, 1000.0, 100.0)
    fake._status = _q_line(actual, "pio", actual.samples_per_window - 1)
    assert driver.configure_ramp(50.0, 1000.0, 100.0) is False


def test_int_below_dt_message_names_the_constraint():
    d = make_driver(FakeSerialPort())
    with pytest.raises(ValueError, match="below one dt"):
        d.calculate_ramp_timing(50.0, 30.0, 100.0)


# ----------------------------------------------------------------------
# Transient-test generator (firmware 2.3): dt pinned to 1 us, reset in
# 100 ns ticks from 1 us (below the validated measurement minimum — a
# study mode), 100 ns settle before the first CONV_START edge.
# ----------------------------------------------------------------------

def test_transient_mirror_exact_ticks():
    d = make_driver(FakeSerialPort())
    # dt pinned by TRANSIENT_DIV 16: tick = 16/150 us, dt = 1.0667 us.
    # reset 1 us -> ceil(9.375) = 10 ticks = 1.0667 us; int 100 us =
    # 15000 cycles -> 93 intervals of 160 -> 94 pulses; period =
    # 10 ticks + 1 tick settle + 94 * dt
    a = d.calculate_transient_timing(100.0, 1.0)
    assert a.reset_time_us == pytest.approx(1.0666667)   # 10 ticks
    assert a.int_time_us == pytest.approx(99.2)          # 93 * dt
    assert a.ramp_dt_eff_us == pytest.approx(1.0666667)
    assert a.samples_per_window == 94
    assert a.period_us == pytest.approx(101.44)


def test_transient_reset_rounds_up_never_shorter():
    d = make_driver(FakeSerialPort())
    a = d.calculate_transient_timing(100.0, 1.05)
    assert a.reset_time_us == pytest.approx(1.0666667)   # 10 ticks (>= 1.05)


def test_transient_rejects_out_of_range():
    d = make_driver(FakeSerialPort())
    with pytest.raises(ValueError, match="transient"):
        d.calculate_transient_timing(100.0, 0.5)      # reset below 1 us
    with pytest.raises(ValueError):
        d.calculate_transient_timing(100.0, 11_000_000.0)  # beyond 10 s
    with pytest.raises(ValueError):
        d.calculate_transient_timing(1.0, 5.0)        # int below the limits


def test_transient_a_counts_match_the_mirror(fake, driver):
    """The 'A' command programs EXACTLY the mirror's geometry — the same
    counts the preview showed (the B35 rule, shared with 'O'/'F')."""
    actual = driver.calculate_transient_timing(10_000.0, 3.5)
    fake._status = _q_line(actual, "tpio", actual.samples_per_window)
    assert driver.configure_transient_test(10_000.0, 3.5) is True
    a_cmds = [c for c in fake.commands if c.startswith("A ")]
    assert a_cmds == ["A 33,9376"]      # 3.5 us -> 33 ticks; 9375 intervals
    ticks, pulses = map(int, a_cmds[0][2:].split(","))
    assert pulses == actual.samples_per_window
    # one tick = TRANSIENT_DIV/150 us
    assert ticks * 16 / 150 == pytest.approx(actual.reset_time_us)


def test_configure_transient_test_sequence(fake, driver):
    fake._status = (b"Q run=0 mode=ramp gen=tpio int=99.200 reset=1.067 "
                    b"dt=1.067 div=16.000 pulses=94 caps=000 event=ME dropped=0\n")
    assert driver.configure_transient_test(100.0, 1.0) is True
    assert fake.commands == ["V", "XR", "GT", "A 10,94", "Q"]


def test_transient_verification_exact_pulses(fake, driver):
    actual = driver.calculate_transient_timing(100.0, 1.0)
    fake._status = _q_line(actual, "tpio", actual.samples_per_window - 1)
    assert driver.configure_transient_test(100.0, 1.0) is False


# ----------------------------------------------------------------------
# Firmware-image guard: every PIO program the code references must be
# LOADED (pio_add_program) and therefore present in the shipped UF2.
# Regression: the first 2.3 build shipped without the transient program
# loaded — the SM executed empty pio1 memory (garbage waveform) while
# 'Q' still verified, because Q reports the programmed registers, not
# what the SM executes.
# ----------------------------------------------------------------------

PIO_PROGRAM_WORDS = {
    "ramp_control": [
        0x80A0, 0xA0C7, 0x80A0, 0xA026, 0x0044, 0xBF47, 0xB321,
        0xF401, 0xF300, 0x1087,
    ],
    "ramp_fixed": [
        0x80A0, 0xA0C7, 0x80A0, 0xE53E, 0xAF42, 0xA642, 0x0044, 0xBE47,
        0xFC2F, 0xBF42, 0x1049, 0xF001, 0xB026, 0x104D, 0xF000, 0xB026,
        0x1050, 0x108B,
    ],
    "ramp_transient": [
        0x80A0, 0xA0C7, 0x80A0, 0xA026, 0x0044, 0xB047, 0xF401,
        0xF300, 0x1086,
    ],
    "ramp_transient_reset": [
        0x80A0, 0xA0C7, 0x80A0, 0xA046, 0xE401, 0xE300, 0x0084,
        0xB027, 0x1048, 0xB046,
    ],
}


def _uf2_payload(path):
    """The flash image reassembled from the UF2's 512-byte blocks: each
    block carries its target ADDRESS and 256 payload bytes — placing by
    address (not naive concatenation) keeps a program array contiguous in
    the image even when it straddles block boundaries or the blocks are
    not ordered."""
    blob = open(path, "rb").read()
    chunks = []
    for off in range(0, len(blob) - 511, 512):
        block = blob[off:off + 512]
        assert block[:4] == b"UF2\n", f"bad UF2 block at {off}"
        addr = int.from_bytes(block[12:16], "little")
        size = int.from_bytes(block[16:20], "little")  # NOT [20:24]: that
        # is the block NUMBER (the first parser bug shipped as "size")
        chunks.append((addr, block[32:32 + size]))
    base = min(a for a, _ in chunks)
    image = bytearray(max(a + len(d) for a, d in chunks) - base)
    for addr, data in chunks:
        image[addr - base:addr - base + len(data)] = data
    return bytes(image)


def test_shipped_uf2_loads_every_pio_program():
    """The shipped firmware image must contain the assembled words of all
    PIO programs (a program referenced by the code but never passed to
    pio_add_program is silently garbage-collected out of the image)."""
    import os
    import struct

    uf2 = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "PICO", "v2.2", "pico2_daq_firmware.uf2",
    )
    if not os.path.exists(uf2):
        pytest.skip("firmware image not built")
    image = _uf2_payload(uf2)
    for name, words in PIO_PROGRAM_WORDS.items():
        seq = b"".join(struct.pack("<H", w) for w in words)
        assert seq in image, (
            f"{name} is missing from the firmware image — its "
            "pio_add_program() call was probably lost"
        )


# ----------------------------------------------------------------------
# Reset-sampling transient generator ('GR'/'B'): pulses DURING the reset
# phase, integration silent.  The app's fields are SWAPPED: Integration
# Time = the sampled pulsed reset phase (exact multiples of dt = 1.0667 us,
# one sample per pulse), Reset Time = the silent integration.
# ----------------------------------------------------------------------

def test_transient_reset_mirror_swapped_fields():
    d = make_driver(FakeSerialPort())
    a = d.calculate_transient_reset_timing(3.0, 20000.0)
    assert a.int_time_us == pytest.approx(3.2)        # 3 pulses * dt
    assert a.reset_time_us == pytest.approx(20000.0)  # silent phase
    assert a.samples_per_window == 3
    assert a.ramp_dt_eff_us == pytest.approx(1.0666667)
    assert a.period_us == pytest.approx(20003.2)


def test_transient_reset_rounds_up():
    d = make_driver(FakeSerialPort())
    a = d.calculate_transient_reset_timing(3.3, 2000.0)
    assert a.int_time_us == pytest.approx(4.2666667)  # 4 pulses (3 * dt < 3.3)
    assert a.samples_per_window == 4
    b = d.calculate_transient_reset_timing(3.0, 2000.04)
    assert b.reset_time_us == pytest.approx(2000.1066667)  # 20001 ticks


def test_transient_reset_rejects_out_of_range():
    d = make_driver(FakeSerialPort())
    with pytest.raises(ValueError, match="pulsed reset phase"):
        d.calculate_transient_reset_timing(0.5, 20000.0)
    with pytest.raises(ValueError, match="silent integration"):
        d.calculate_transient_reset_timing(3.0, 0.5)  # below the 1 us test floor
    # A SHORT silent phase is legal in this study mode (the cycle just
    # repeats sooner): 2-3 us must pass, unlike a measurement integration
    # (the applied value rounds UP to whole ~107 ns ticks).
    assert d.calculate_transient_reset_timing(3.0, 3.0).reset_time_us == pytest.approx(3.0933333)
    assert d.calculate_transient_reset_timing(3.0, 2.0).reset_time_us == pytest.approx(2.0266667)


def test_configure_transient_reset_sequence(fake, driver):
    # Q reports the hardware's view: int = the SILENT phase, reset = the
    # PULSED phase (swapped relative to the mirror) — verified swapped.
    fake._status = (b"Q run=0 mode=ramp gen=rpio int=20000.000 reset=3.200 "
                    b"dt=1.067 div=16.000 pulses=3 caps=000 event=ME dropped=0\n")
    assert driver.configure_transient_reset_test(3.0, 20000.0) is True
    assert fake.commands == ["V", "XR", "GR", "B 3,187500", "Q"]


def test_transient_reset_b_counts_match_the_mirror(fake, driver):
    actual = driver.calculate_transient_reset_timing(20.5, 500.0)
    fake._status = (
        f"Q run=0 mode=ramp gen=rpio int={actual.reset_time_us:.3f} "
        f"reset={actual.int_time_us:.3f} dt={actual.ramp_dt_eff_us:.3f} "
        f"div=16.000 pulses={actual.samples_per_window} caps=000 event=ME dropped=0\n"
    ).encode()
    assert driver.configure_transient_reset_test(20.5, 500.0) is True
    assert [c for c in fake.commands if c.startswith("B ")] == ["B 20,4688"]


def test_transient_reset_verification_pulses_exact(fake, driver):
    actual = driver.calculate_transient_reset_timing(3.0, 20000.0)
    fake._status = (
        f"Q run=0 mode=ramp gen=rpio int={actual.reset_time_us:.3f} "
        f"reset={actual.int_time_us:.3f} dt={actual.ramp_dt_eff_us:.3f} "
        f"div=16.000 pulses={actual.samples_per_window - 1} "
        f"caps=000 event=ME dropped=0\n"
    ).encode()
    assert driver.configure_transient_reset_test(3.0, 20000.0) is False


# ----------------------------------------------------------------------
# Test-mode integration lengths beyond the 10 s measurement envelope:
# studies integrate for tens of seconds (generators' 32-bit pulse counts
# allow ~70 min; the envelope caps at 1 h / silent 5 min).
# ----------------------------------------------------------------------

def test_transient_accepts_tens_of_seconds():
    d = make_driver(FakeSerialPort())
    a = d.calculate_transient_timing(60_000_000.0, 5.0)   # 60 s of pulses
    assert a.samples_per_window == 56_250_001     # 60 s / dt(1.0667 us) + 1
    assert a.int_time_us == pytest.approx(60_000_000.0)
    assert a.period_us == pytest.approx(
        5.0133333 + 0.1066667 + 56_250_001.0 * (16.0 / 15.0))
    with pytest.raises(ValueError, match="transient test range"):
        d.calculate_transient_timing(4_000_000_000.0, 5.0)  # beyond 1 h


def test_transient_reset_accepts_tens_of_seconds():
    d = make_driver(FakeSerialPort())
    a = d.calculate_transient_reset_timing(60_000_000.0, 120_000_000.0)
    assert a.samples_per_window == 56_250_000  # 60 s of pulsed reset / dt
    assert a.int_time_us == pytest.approx(60_000_000.0)
    assert a.reset_time_us == pytest.approx(120_000_000.0)  # 2 min silent
    assert a.period_us == pytest.approx(180_000_000.0)
    with pytest.raises(ValueError, match="transient test range"):
        d.calculate_transient_reset_timing(60_000_000.0, 400_000_000.0)
    with pytest.raises(ValueError, match="transient test range"):
        d.calculate_transient_reset_timing(4_000_000_000.0, 120_000_000.0)


def test_roundtrip_boundary_reset_keeps_its_tick():
    """The wizard's us -> s -> us conversion delivers exact boundary values
    one float count low (typed 200 us -> 199.99999999999997): without the
    guarded floor that lost a whole 100 ns reset tick on the dt-scaled PIO
    (bench log: 'reset 199.99999999999997 -> 199.9 us (dt=1.0 us)')."""
    d = make_driver(FakeSerialPort())
    a = d.calculate_ramp_timing(1.0, 1_000_000.0, 199.99999999999997)
    dt_eff = (15 * 256 + 1) / 256 / 15.0        # one step past divider 15
    assert a.reset_time_us == pytest.approx(2000 * dt_eff / 10)   # 2000 ticks
    assert a.int_time_us == pytest.approx(1_000_000 * dt_eff)
    assert a.ramp_dt_eff_us == pytest.approx(dt_eff)   # strictly > 1 us
    assert 1e6 / a.ramp_dt_eff_us < 1_000_000.0        # under the card spec
    assert a.samples_per_window == 1_000_001
    # the no-ramp mirror no longer shaves a cycle off boundaries either
    # (arguments: integration, reset)
    b = d.calculate_actual_timing(199.99999999999997, 10.0)
    assert b.int_time_us == pytest.approx(200.0)
