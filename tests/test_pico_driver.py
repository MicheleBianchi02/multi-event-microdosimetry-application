"""
Protocol-level tests for the PicoSerialDAQ driver against a fake serial
port that speaks the PICO2DAQ 2.0 firmware protocol (see
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

IDENTITY = b"V PICO2DAQ 2.0 F4D87D3B6FBAEF13 150\n"
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
    d = make_driver(FakeSerialPort(identity=b"V PICO2DAQ 2.0 XX 125\n"))
    assert d.connect() is False


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
    assert fake.commands == ["V", "XR", "GP", "O 750,0,20,21", "Q"]


def test_configure_ramp_selects_software_generator_above_threshold(fake, driver):
    # dt=200 -> software generator: plain T + P, no O, gen field 'c'
    fake._status = (b"Q run=0 mode=ramp gen=c int=1000.000 reset=100.000 "
                    b"dt=200.000 div=0.000 pulses=6 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(200.0, 1000.0, 100.0) is True
    assert fake.commands == ["V", "XR", "GC", "T 1000.0,100.0", "P 200.0", "Q"]


def test_calculate_ramp_timing_pio_mirror():
    d = make_driver(FakeSerialPort())
    # dt=50, int=1000, reset=100: div exact 750 -> dt_eff=50;
    # intervals=20 -> int=1000 edge-to-edge; reset_pio=20 -> 100 us low;
    # settle=20 PIO cycles=100 us; period=100+100+21*50=1250
    actual = d.calculate_ramp_timing(50.0, 1000.0, 100.0)
    assert actual.int_time_us == pytest.approx(1000.0)
    assert actual.reset_time_us == pytest.approx(100.0)
    assert actual.period_us == pytest.approx(1250.0)
    assert actual.sample_frequency_hz == pytest.approx(1e6 / 1250.0)


def test_calculate_ramp_timing_software_mirror():
    d = make_driver(FakeSerialPort())
    # dt=1000 (above the PIO threshold): everything floors to cycles
    actual = d.calculate_ramp_timing(1000.0, 3000.0, 500.0)
    assert actual.int_time_us == pytest.approx(3000.0)
    assert actual.reset_time_us == pytest.approx(500.0)
    # settle 2 us + train (3000/1000 + 1) * 1000 = 4000
    assert actual.period_us == pytest.approx(500.0 + 2.0 + 4000.0)


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
    assert fake.commands == ["V", "XR", "GP", "O 750,0,20,21", "Q"]


def test_configure_ramp_verification_tolerates_missing_q(fake, driver):
    fake._status = b""  # no Q reply: skipped with a warning, still ok
    assert driver.configure_ramp(50.0, 1000.0, 100.0) is True


def test_ramp_generator_threshold():
    d = make_driver(FakeSerialPort())
    assert d.ramp_generator_for(1.0) == "pio"
    assert d.ramp_generator_for(100.0) == "pio"
    assert d.ramp_generator_for(100.5) == "software"
    assert d.ramp_generator_for(1000.0) == "software"


def test_configure_ramp_quantizes_reset_to_pio_tick(fake, driver):
    # v2: reset = plain LOW duration, quantized to dt/10 ticks (no overhead
    # constraint).  7 us at dt=50 -> one 5-us tick.
    fake._status = (b"Q run=0 mode=ramp gen=pio int=1000.000 reset=5.000 "
                    b"dt=50.000 div=750.000 pulses=21 caps=000 event=ME dropped=0\n")
    assert driver.configure_ramp(50.0, 1000.0, 7.0) is True
    actual = driver.calculate_ramp_timing(50.0, 1000.0, 7.0)
    assert actual.reset_time_us == pytest.approx(5.0)


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
# Ramp-dt bound vs the deployed firmware (bug: the software generator has
# no dt cap, but firmware 2.0's 'P' validation applies the PIO-derived
# 4369 us bound to BOTH generators; markers mode with long T_int hits it)
# ----------------------------------------------------------------------

def _with_identity(driver, version):
    driver.identity = {
        "firmware": "PICO2DAQ",
        "version": version,
        "board_id": "TESTBOARD",
        "clk_mhz": 150.0,
    }
    return driver


def test_dt_beyond_4369_rejected_on_firmware_2_0(fake, driver):
    _with_identity(driver, "2.0")
    # markers-mode shape: dt = T_int = 100 ms (software generator)
    with pytest.raises(ValueError, match="caps ramp dt"):
        driver.calculate_ramp_timing(100_000.0, 100_000.0, 500.0)
    # and configure refuses BEFORE any serial traffic
    assert driver.configure_ramp(100_000.0, 100_000.0, 500.0) is False
    assert fake.commands == []


def test_dt_beyond_4369_accepted_from_firmware_2_1(fake, driver):
    _with_identity(driver, "2.1")
    actual = driver.calculate_ramp_timing(100_000.0, 100_000.0, 500.0)
    # software generator mirror: N = int/dt + 1 = 2 pulses, settle 2 us
    assert actual.int_time_us == pytest.approx(100_000.0)
    assert actual.reset_time_us == pytest.approx(500.0)
    assert actual.period_us == pytest.approx(500.0 + 2.0 + 2 * 100_000.0)


def test_dt_bound_unconnected_is_conservative(fake, driver):
    # no identity (never connected): assume the old firmware
    with pytest.raises(ValueError):
        driver.calculate_ramp_timing(10_000.0, 10_000.0, 500.0)


def test_dt_below_pio_bound_still_fine_on_both(fake, driver):
    for version in ("2.0", "2.1"):
        _with_identity(driver, version)
        actual = driver.calculate_ramp_timing(50.0, 200.0, 20.0)
        assert actual.int_time_us > 0


# ----------------------------------------------------------------------
# Firmware/driver version consistency: the driver's large-dt gate
# (RAMP_DT_LARGE_FW) must be satisfiable by the firmware source shipped in
# raspberry_copy — i.e. the flashed PICO2DAQ version reports >= 2.1, the
# release that lifted the 'P' dt cap for the software generator.
# ----------------------------------------------------------------------

def test_shipped_firmware_satisfies_the_large_dt_gate():
    import re
    from pathlib import Path

    main_c = Path(__file__).resolve().parent.parent / "raspberry_copy" / "main.c"
    if not main_c.exists():
        pytest.skip("raspberry_copy not present (firmware lives in its own checkout)")
    m = re.search(r'#define FW_VERSION\s+"(\d+)\.(\d+)"', main_c.read_text())
    assert m, "FW_VERSION define not found in the firmware source"
    from hardware.pico.pico_daq import RAMP_DT_LARGE_FW

    assert tuple(int(g) for g in m.groups()) >= RAMP_DT_LARGE_FW, (
        "the firmware source's FW_VERSION is older than the driver's "
        "RAMP_DT_LARGE_FW gate: long-integration markers mode would stay "
        "locked until the firmware is bumped (or the gate lowered)."
    )
