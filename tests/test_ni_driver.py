"""
NI DAQmx driver tests (ni_daqmx_v1) against a fake `nidaqmx` package
injected into sys.modules — the same philosophy as the Pico fake serial
port: no hardware, no driver runtime, but the real driver code paths
(task configuration, external clock, volts->counts conversion, block
streaming into the sink, stop semantics).
"""
import os
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hardware.ni.ni_daqmx import NIDAQmxAcquisition  # noqa: E402


# ----------------------------------------------------------- fake nidaqmx

class FakeVoltageChan:
    def __init__(self, physical_channel, terminal_config, min_val, max_val):
        self.physical_channel = physical_channel
        self.terminal_config = terminal_config
        self.min_val = min_val
        self.max_val = max_val


class FakeAIChannels:
    def __init__(self, task):
        self.task = task
        self.added = []

    def add_ai_voltage_chan(self, physical_channel, terminal_config=None,
                            min_val=None, max_val=None):
        self.added.append(FakeVoltageChan(
            physical_channel, terminal_config, min_val, max_val))


class FakeTiming:
    def __init__(self, task):
        self.task = task
        self.clock = None

    def cfg_samp_clk_timing(self, rate=None, source=None, active_edge=None,
                            sample_mode=None):
        self.clock = dict(rate=rate, source=source, active_edge=active_edge,
                          sample_mode=sample_mode)


class FakeTask:
    all_tasks = []

    def __init__(self, new_task_name=""):
        self.ai_channels = FakeAIChannels(self)
        self.timing = FakeTiming(self)
        self.in_stream = self  # the reader grabs task.in_stream
        self.started = False
        self.stopped = False
        self.closed = False
        FakeTask.all_tasks.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    @property
    def avail_samp_per_chan(self):
        return FakeReader._available()


class FakeDaqError(Exception):
    """Mirrors nidaqmx.DaqError: carries the DAQmx error code."""

    def __init__(self, message, error_code, task_name=""):
        super().__init__(message)
        self.error_code = error_code


class FakeDaqReadError(FakeDaqError):
    """Mirrors nidaqmx.DaqReadError: a read error (timeouts included)
    that carries the number of samples already transferred into the
    caller's buffer (and removed from the device buffer)."""

    def __init__(self, message, error_code, samps_per_chan_read):
        super().__init__(message, error_code)
        self.samps_per_chan_read = samps_per_chan_read


READ_TIMEOUT_CODE = -200284


class FakeReader:
    """Mirrors the REAL AnalogMultiChannelReader contract (nidaqmx 1.6.0):
    READ_ALL_AVAILABLE resolves to avail_samp_per_chan and — with
    verify_array_shape True, the default — the array is verified to be
    shaped (num_chans, count) EXACTLY; explicit-count reads wait up to
    `timeout` and on expiry raise DaqReadError carrying the partial count,
    with the transferred samples already written into the caller's array.
    `script` entries are exception instances (raised on pull) or
    (nchannels, nsamples) volts arrays moved into the buffer."""

    READ_ALL = -1  # nidaqmx.constants.READ_ALL_AVAILABLE
    script = []
    _buffered = []
    _lock = threading.Lock()
    requested_counts = []  # every count the driver asked to read

    def __init__(self, task_in_stream):
        self._task = task_in_stream
        self._verify_array_shape = True

    @property
    def verify_array_shape(self):
        return self._verify_array_shape

    @verify_array_shape.setter
    def verify_array_shape(self, val):
        self._verify_array_shape = val

    @classmethod
    def reset(cls):
        cls.script = []
        cls._buffered = []
        cls.requested_counts = []

    @classmethod
    def _pull_script(cls):
        with cls._lock:
            while cls.script:
                entry = cls.script.pop(0)
                if isinstance(entry, Exception):
                    raise entry
                cls._buffered.append(entry)

    @classmethod
    def _available(cls):
        return sum(entry.shape[1] for entry in cls._buffered)

    def _verify_array(self, data, count):
        if not self._verify_array_shape:
            return
        required = (data.shape[0], count)
        if data.shape != required:
            raise FakeDaqError(
                f"array shaped {data.shape}, required {required}", -200279
            )

    def _assemble(self, data, want):
        out = np.empty((data.shape[0], want), dtype=float)
        taken = 0
        while taken < want:
            chunk = self._buffered[0]
            take = min(want - taken, chunk.shape[1])
            out[:, taken:taken + take] = chunk[:, :take]
            taken += take
            if take < chunk.shape[1]:
                with self._lock:
                    self._buffered[0] = chunk[:, take:]
            else:
                with self._lock:
                    self._buffered.pop(0)
        data[:, :want] = out
        return want

    def read_many_sample(self, data, number_of_samples_per_channel, timeout=10.0):
        FakeReader.requested_counts.append(number_of_samples_per_channel)
        try:
            return self._read_impl(data, number_of_samples_per_channel, timeout)
        except FakeDaqReadError as exc:
            partial = exc.samps_per_chan_read
            if partial:
                # the real DAQmx call transfers the samples into the
                # caller's array before returning the error
                data[:, :partial] = -5.0
            raise

    def _read_impl(self, data, number_of_samples_per_channel, timeout=10.0):
        count = number_of_samples_per_channel
        if count == self.READ_ALL:
            # the real wrapper resolves READ_ALL to avail_samp_per_chan
            # (continuous task) BEFORE verifying the array shape
            self._pull_script()
            count = self._available()
        self._verify_array(data, count)

        deadline = time.monotonic() + (timeout if timeout is not None else 10.0)
        while True:
            self._pull_script()
            if self._available() >= count:
                return self._assemble(data, count)
            partial = min(self._available(), count)
            if time.monotonic() >= deadline:
                # real behaviour: the samples read before the timeout are
                # transferred into the array AND removed from the device
                # buffer, then the error carries their count
                if partial:
                    self._assemble(data, partial)
                raise FakeDaqReadError(
                    "samples not yet available", READ_TIMEOUT_CODE, partial
                )
            if self._task.stopped:
                # DAQmxStopTask aborts a blocked read
                if partial:
                    self._assemble(data, partial)
                raise FakeDaqReadError("task stopped", READ_TIMEOUT_CODE, partial)
            time.sleep(0.001)


class FakeDevice:
    def __init__(self, name, product_type="USB-6451"):
        self.name = name
        self.product_type = product_type

    def self_test_device(self):
        return None


class FakeSystem:
    devices = []  # class-level, set per test

    @staticmethod
    def local():
        return FakeSystem


class FakePhysicalChannel:
    """Records dig_port_logic_family writes (the driver sets the shared
    digital logic family to 3.3 V at connect)."""

    instances = []
    fail_on_family_set = False

    def __init__(self, name):
        self.name = name
        self._family = None
        FakePhysicalChannel.instances.append(self)

    def _get_family(self):
        return self._family

    def _set_family(self, value):
        if FakePhysicalChannel.fail_on_family_set:
            raise RuntimeError("family not supported")
        self._family = value

    dig_port_logic_family = property(_get_family, _set_family)


def _make_constants():
    module = types.ModuleType("nidaqmx.constants")
    import enum

    class AcquisitionType(enum.Enum):
        CONTINUOUS = 10123

    class Edge(enum.Enum):
        RISING = 10280
        FALLING = 10171

    class TerminalConfiguration(enum.Enum):
        RSE = 10083
        NRSE = 10078
        DIFF = 10106

    class LogicFamily(enum.Enum):
        ONE_POINT_EIGHT_V = 16184
        TWO_POINT_FIVE_V = 14620
        THREE_POINT_THREE_V = 14621
        FIVE_V = 14619

    module.AcquisitionType = AcquisitionType
    module.Edge = Edge
    module.TerminalConfiguration = TerminalConfiguration
    module.LogicFamily = LogicFamily
    module.READ_ALL_AVAILABLE = -1
    return module


CONSTANTS = _make_constants()


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    FakeTask.all_tasks.clear()
    FakeReader.reset()
    FakePhysicalChannel.instances.clear()
    FakePhysicalChannel.fail_on_family_set = False
    FakeSystem.devices = [FakeDevice("NI6451")]

    root = types.ModuleType("nidaqmx")
    root.Task = FakeTask
    constants = CONSTANTS
    readers = types.ModuleType("nidaqmx.stream_readers")
    readers.AnalogMultiChannelReader = FakeReader
    system = types.ModuleType("nidaqmx.system")
    system.System = FakeSystem
    system.PhysicalChannel = FakePhysicalChannel
    errors = types.ModuleType("nidaqmx.errors")

    class DaqError(Exception):
        pass

    errors.DaqError = DaqError
    root.constants = constants
    root.stream_readers = readers
    root.system = system
    root.errors = errors

    monkeypatch.setitem(sys.modules, "nidaqmx", root)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", constants)
    monkeypatch.setitem(sys.modules, "nidaqmx.stream_readers", readers)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", system)
    monkeypatch.setitem(sys.modules, "nidaqmx.errors", errors)
    return root


class StubSink:
    def __init__(self):
        self.blocks = []
        self.gaps = []

    def write_block(self, channel, samples, abs_index=None):
        self.blocks.append((channel, np.asarray(samples).copy(), abs_index))

    def report_gap(self, channel, abs_index, missing):
        self.gaps.append((channel, abs_index, missing))

    def received(self, channel):
        """Concatenated samples of one channel, asserting the per-channel
        absolute indices are contiguous from 0 (the sink contract the
        window extractor's alignment depends on).  Block boundaries are
        arbitrary — the driver splits wherever its reads happen to fall."""
        pieces = []
        expected_index = 0
        for ch, samples, idx in self.blocks:
            if ch != channel:
                continue
            assert idx == expected_index, (idx, expected_index)
            pieces.append(samples)
            expected_index += len(samples)
        return np.concatenate(pieces) if pieces else np.array([], dtype=np.uint32)

    def total(self, channel):
        return len(self.received(channel))


def make_driver(connected=True, plan=(("ch1", "differential"), ("ch2", "differential"))):
    driver = NIDAQmxAcquisition(device_name="NI6451")
    driver._connected = connected
    if plan:
        assert driver.configure_channels(list(plan))
    return driver


def make_session(**overrides):
    import types as _t
    defaults = dict(
        debug_mode=True, multiple_channel=True, timing_mode="ramp",
        ramp_dt_s=1e-6, int_time=0.001, reset_time=0.0005, period_s=0.0015,
    )
    defaults.update(overrides)
    return _t.SimpleNamespace(**defaults)


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# ------------------------------------------------------------ connection

def test_factory_requires_daqmx_connection():
    from hardware.registry import DRIVER_REGISTRY
    registered = DRIVER_REGISTRY["ni_daqmx_v1"]
    with pytest.raises(ValueError):
        registered.create({"type": "serial"}, None)
    driver = registered.create({"type": "daqmx", "device": "NI6451"}, None)
    assert isinstance(driver, NIDAQmxAcquisition)


def test_connect_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "nidaqmx", None)  # import -> ImportError
    driver = NIDAQmxAcquisition(device_name="NI6451")
    assert driver.connect() is False


def test_connect_unknown_device(fake_nidaqmx):
    FakeSystem.devices = [FakeDevice("something_else")]
    driver = NIDAQmxAcquisition(device_name="NI6451")
    assert driver.connect() is False


def test_connect_ok(fake_nidaqmx):
    driver = NIDAQmxAcquisition(device_name="NI6451")
    assert driver.connect() is True
    assert driver.is_connected
    assert driver._product_type == "USB-6451"


# --------------------------------------------------------- configuration

def test_configure_channels_maps_fixed_terminals():
    driver = NIDAQmxAcquisition()
    assert driver.configure_channels([("ch1", "differential"), ("ch2", "differential")])
    assert driver._channel_plan == [("ch1", "differential"), ("ch2", "differential")]
    assert driver.configure_channels([("ch1", "rse"), ("ch2", "rse")])

    assert not driver.configure_channels([("ch1", "nrse")])       # not supported
    assert not driver.configure_channels([])                      # empty plan
    assert not driver.configure_channels([("ch3", "differential")])  # unknown channel


def test_set_input_range_accepts_only_device_ranges():
    driver = NIDAQmxAcquisition()
    assert driver.set_input_range(-5.0, 5.0)
    assert driver.set_input_range(-0.2, 0.2)
    assert not driver.set_input_range(-7.0, 7.0)
    settings = driver.get_adc_settings()
    assert settings["bits"] == 20
    assert settings["input_range_volts"] == {"min_volts": -0.2, "max_volts": 0.2}


# ------------------------------------------------------------ measurement

def test_start_refuses_without_sink_or_plan():
    driver = make_driver()
    assert not driver.start_measurement(make_session(), None, ramp_sink=None)

    driver_no_plan = NIDAQmxAcquisition(device_name="NI6451")
    driver_no_plan._connected = True
    sink = StubSink()
    assert not driver_no_plan.start_measurement(make_session(), None, ramp_sink=sink)


def test_start_configures_task_channels_and_external_clock(fake_nidaqmx):
    driver = make_driver()
    driver.set_input_range(-5.0, 5.0)
    sink = StubSink()
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)

    (task,) = FakeTask.all_tasks
    assert [c.physical_channel for c in task.ai_channels.added] == [
        "NI6451/ai0", "NI6451/ai1"]
    from nidaqmx.constants import TerminalConfiguration
    assert [c.terminal_config for c in task.ai_channels.added] == [
        TerminalConfiguration.DIFF, TerminalConfiguration.DIFF]
    assert all(c.min_val == -5.0 and c.max_val == 5.0 for c in task.ai_channels.added)

    clock = task.timing.clock
    assert clock["source"] == "/NI6451/PFI0"
    assert clock["active_edge"] == CONSTANTS.Edge.RISING
    assert clock["sample_mode"] == CONSTANTS.AcquisitionType.CONTINUOUS
    assert clock["rate"] == pytest.approx(1e6)  # 1/dt, for buffer sizing

    driver.stop_measurement()
    assert task.stopped and task.closed


def test_task_is_armed_before_start_measurement_returns(fake_nidaqmx):
    """Alignment contract: when start_measurement returns, the task is
    already armed — the caller may send the timing 'S' immediately and the
    first CONV_START edge becomes the first sample of window 0."""
    driver = make_driver()
    sink = StubSink()
    FakeReader.script = []
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert FakeTask.all_tasks[-1].started is True
    driver.stop_measurement()


def test_read_timeout_tolerated_while_waiting_for_edges(fake_nidaqmx):
    """Armed task, timing engine not started yet: reads time out and the
    reader keeps waiting instead of failing the measurement."""
    driver = make_driver()
    driver.set_input_range(-5.0, 5.0)
    sink = StubSink()
    block = driver._block_size(1e6)

    FakeReader.script = [
        FakeDaqReadError("no edges yet", READ_TIMEOUT_CODE, 0),
        FakeDaqReadError("still no edges", READ_TIMEOUT_CODE, 0),
        np.stack([np.full(block, 0.0), np.full(block, 0.0)]),
    ]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == block)
    assert driver.stop_measurement() is True  # timeouts were not an error
    assert driver._error is False
    # everything scripted was delivered contiguously to both channels
    # (0.0 V over [-5, +5] maps to mid-scale 524288)
    assert sink.total(0) == block and sink.total(1) == block
    assert (sink.received(0) == 524288).all()


def test_reader_streams_converted_counts_with_indices(fake_nidaqmx):
    driver = make_driver()
    driver.set_input_range(-5.0, 5.0)
    sink = StubSink()

    block = driver._block_size(1e6)
    # volts chosen to map to exact counts over [-5, +5] at 20 bit:
    #   +5.0 V -> 1048575, 0.0 V -> 524288, -5.0 V -> 0, +10 V (overrange) -> clamp 1048575
    def row(v, n=None):
        return np.full(block if n is None else n, v, dtype=np.float64)

    # 20000 + 20000 samples: block boundaries in the sink are wherever the
    # driver's reads fall; only the aggregated stream is contractually exact
    FakeReader.script = [
        np.stack([row(5.0), row(0.0)]),
        np.stack([row(-5.0), row(10.0)]),   # ch2 overrange -> clamps
    ]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == 2 * block)
    driver.stop_measurement()

    ch1 = sink.received(0)
    ch2 = sink.received(1)
    assert len(ch1) == 2 * block and len(ch2) == 2 * block
    assert (ch1[:block] == 1048575).all()          # +5 V full scale
    assert (ch2[:block] == 524288).all()           # 0 V -> mid scale
    assert (ch1[block:] == 0).all()                # -5 V -> zero-based 0
    assert (ch2[block:] == 1048575).all()          # +10 V overrange -> clamped
    for _, counts, _ in sink.blocks:
        assert counts.dtype == np.uint32


def test_never_reads_all_available_and_disables_shape_verification(fake_nidaqmx):
    """B30 pin: the driver reads explicit counts with array verification
    disabled — READ_ALL_AVAILABLE into the fixed buffer would fail the
    wrapper's EXACT (chans, avail) shape check on the real library."""
    driver = make_driver()
    sink = StubSink()
    block = driver._block_size(1e6)
    FakeReader.script = [np.stack([
        np.arange(block + 7, dtype=np.float64),
        np.arange(block + 7, dtype=np.float64),
    ])]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == block + 7)
    driver.stop_measurement()

    (task,) = FakeTask.all_tasks
    assert driver._reader.verify_array_shape is False
    assert FakeReader.requested_counts, "the driver never read"
    assert all(c != FakeReader.READ_ALL for c in FakeReader.requested_counts), (
        "READ_ALL_AVAILABLE must never be requested into a fixed buffer"
    )
    # and an oversized READ_ALL against a verifying reader fails exactly
    # like the real wrapper (this is what the old drain did)
    probing = FakeReader(task)
    small = np.empty((2, 4), dtype=np.float64)
    FakeReader._buffered = [np.zeros((2, 9))]
    with pytest.raises(FakeDaqError):
        probing.read_many_sample(small, FakeReader.READ_ALL, timeout=0.05)


def test_partial_timeout_delivers_transferred_samples(fake_nidaqmx):
    """B31 pin: a read timeout that already transferred samples into the
    buffer (and removed them from the device buffer) must deliver them —
    otherwise the sink counters do not advance and every subsequent
    window is silently misaligned."""
    driver = make_driver()
    sink = StubSink()
    block = driver._block_size(1e6)

    FakeReader.script = [
        # a hiccup: the read transferred 2 samples then timed out — the
        # fake fills the caller's buffer with zeros for the partial (as
        # the real DAQmx call does) and raises with the count
        FakeDaqReadError("partial", READ_TIMEOUT_CODE, 2),
        np.stack([
            np.full(block, 5.0),
            np.full(block, 5.0),
        ]),
    ]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == 2 + block)
    assert driver.stop_measurement() is True
    assert driver._error is False

    # counters advanced across the partial: contiguous 2 + block samples
    ch1 = sink.received(0)
    assert len(ch1) == 2 + block
    assert ch1[:2].tolist() == [0, 0]          # the transferred partial
    assert (ch1[2:] == 1048575).all()          # +5 V block after it
    assert sink.gaps == []                     # no gap needed: contiguous


def test_single_channel_plan_streams_one_channel(fake_nidaqmx):
    driver = make_driver(plan=(("ch1", "rse"),))
    sink = StubSink()
    block = driver._block_size(1e6)
    FakeReader.script = [np.stack([np.full(block, 1.0)])]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == block)
    driver.stop_measurement()

    (task,) = FakeTask.all_tasks
    assert [c.physical_channel for c in task.ai_channels.added] == ["NI6451/ai2"]
    assert {b[0] for b in sink.blocks} == {0}  # only ch1 -> sink channel 0


def test_mixed_terminations_allowed(fake_nidaqmx):
    driver = make_driver(plan=(("ch1", "differential"), ("ch2", "rse")))
    sink = StubSink()
    FakeReader.script = []
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    driver.stop_measurement()
    (task,) = FakeTask.all_tasks
    assert [c.physical_channel for c in task.ai_channels.added] == [
        "NI6451/ai0", "NI6451/ai3"]


def test_read_error_flags_stop(fake_nidaqmx):
    driver = make_driver()
    sink = StubSink()
    FakeReader.script = [RuntimeError("DAQmx read failed")]
    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    time.sleep(0.3)  # let the reader hit the error
    assert driver._error is True
    assert driver.stop_measurement() is False


# ------------------------------------------------- bug 3: logic family

def test_connect_sets_33v_logic_family_on_pfi0(fake_nidaqmx):
    """The Pico's CONV_START is 3.3 V; the 6451's shared digital logic
    family defaults to 5 V (VIH >= 3.66 V — out of spec).  connect() must
    set the family to 3.3 V on the clock's physical channel."""
    driver = NIDAQmxAcquisition(device_name="NI6451")
    assert driver.connect() is True
    (chan,) = FakePhysicalChannel.instances
    assert chan.name == "NI6451/PFI0"
    assert chan.dig_port_logic_family == CONSTANTS.LogicFamily.THREE_POINT_THREE_V


def test_connect_fails_when_logic_family_cannot_be_set(fake_nidaqmx):
    FakePhysicalChannel.fail_on_family_set = True
    driver = NIDAQmxAcquisition(device_name="NI6451")
    assert driver.connect() is False  # never arm with an out-of-spec clock


# ------------------------------------- bug 4b: block sizing, stop drain

def test_block_size_and_read_timeout_scale_with_rate():
    # markers mode at T_int = 100 ms -> 10 S/s: ONE sample per read (was
    # 1024 -> ~102 s lumps), timeout ~10 block times, bounded to [0.5, 2] s
    assert NIDAQmxAcquisition._block_size(10.0) == 1
    assert NIDAQmxAcquisition._block_size(1e6) == 20_000
    assert NIDAQmxAcquisition._read_timeout_s(1, 10.0) == 1.0   # 10 block times
    assert NIDAQmxAcquisition._read_timeout_s(1, 5.0) == 2.0    # clamped at max
    assert NIDAQmxAcquisition._read_timeout_s(1, 100.0) == 0.5  # clamped at min
    assert NIDAQmxAcquisition._read_timeout_s(20_000, 1e6) == 0.5


def test_stop_drains_the_buffered_tail_before_task_stop(fake_nidaqmx):
    """Samples sitting unread in the DAQmx buffer must reach the sink
    BEFORE task.stop() discards them (measurement rows in markers mode).
    The fake discards its buffer once the task stopped, so this test only
    passes if the driver drains first — including a tail that arrives
    around the stop itself."""
    driver = make_driver()
    sink = StubSink()
    block = driver._block_size(1e6)  # 20000
    # first entry: block + 500 samples; the run consumes it, then the
    # reader idles in the no-edges wait
    first = np.stack([
        np.arange(block + 500, dtype=np.float64),
        np.arange(block + 500, dtype=np.float64) + 1000,
    ])
    FakeReader.script = [first]

    assert driver.start_measurement(make_session(), None, ramp_sink=sink)
    assert _wait_for(lambda: sink.total(0) == block + 500)

    # a second block becomes available around the stop: whatever the main
    # loop misses must be picked up by the final drain
    second = np.stack([
        np.full(3000, 2.5),
        np.full(3000, 2.5),
    ])
    FakeReader.script.append(second)
    assert driver.stop_measurement() is True

    # EVERYTHING was delivered, contiguous: (block+500) + 3000 per channel
    total = block + 500 + 3000
    assert sink.total(0) == total and sink.total(1) == total
    ch1 = sink.received(0)
    assert (ch1[block + 500:] == np.rint((2.5 + 5.0) * (2**20 - 1) / 10.0)).all()


# --------------------------------------------- start ordering (regression)

class FakeTimingDevice:
    """Stands in for the Pico's timing role: records WHEN 'S' is sent and
    whether the NI task was already armed at that moment (the 6451 ignores
    CONV_START edges while not armed — arming late misaligns the whole
    stream, see the window extractor's abs_index-0 contract)."""

    def __init__(self):
        self.calls = []
        self.task_armed_at_start = None

    def start_timing(self):
        self.calls.append("S")
        tasks = FakeTask.all_tasks
        self.task_armed_at_start = bool(tasks and tasks[-1].started)
        return True

    def stop_timing(self):
        self.calls.append("E")
        return True


class FakeManager:
    def __init__(self, acquisition, timing):
        self.acquisition = acquisition
        self.timing = timing
        self.calls = []

    def start_timing_signal(self):
        self.calls.append("timing_start")
        return self.timing.start_timing()

    def stop_timing_signal(self):
        self.calls.append("timing_stop")
        return self.timing.stop_timing()


class FakeAcquisitionSpy:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def start_measurement(self, session, data_model, ramp_sink=None):
        self.calls.append("start")
        return self.ok

    def stop_measurement(self):
        self.calls.append("stop")
        return True


def test_acquisition_is_armed_before_timing_start(fake_nidaqmx):
    """The real NI driver, driven through HardwareService exactly as the
    measurement start does: by the time the timing engine's 'S' is sent,
    the DAQmx task must be armed (regression: the timing used to start
    first, so the stream began at an arbitrary window phase and every
    start/stop pair was misaligned)."""
    from services.hardware_service import HardwareService

    driver = make_driver()
    sink = StubSink()
    FakeReader.script = []
    timing = FakeTimingDevice()
    manager = FakeManager(acquisition=driver, timing=timing)

    service = HardwareService(manager, data_model=None, session_model=make_session())
    assert service.start_receiving(simulate=False, ramp_sink=sink) is True
    try:
        assert timing.calls == ["S"]
        assert timing.task_armed_at_start is True  # the core of the fix
    finally:
        service.stop_receiving(send_stop=True)

    assert driver.stop_measurement() is True


def test_timing_start_failure_stops_the_armed_acquisition(fake_nidaqmx):
    """If the timing engine refuses to start, the armed acquisition must be
    stopped again — otherwise the NI would sit armed forever with no clock."""
    from services.hardware_service import HardwareService

    spy = FakeAcquisitionSpy(ok=True)
    timing = FakeTimingDevice()
    timing.start_timing = lambda: False  # timing start fails
    manager = FakeManager(acquisition=spy, timing=timing)

    service = HardwareService(manager, data_model=None, session_model=make_session())
    assert service.start_receiving(simulate=False) is False
    assert service.has_error()
    assert spy.calls == ["start", "stop"]  # armed, then cleaned up


# ------------------------------------------------------ config-level tests

def _example_path():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples", "hardware_config_pico_ni.json")


def test_example_pico_ni_config_loads_and_wires_the_plan():
    from hardware.registry import registry_descriptors
    from hardware.config import load_hardware_config

    config = load_hardware_config(_example_path(), registry_descriptors())
    assert config.roles.acquisition == "ni_6451"
    assert config.channels["ch1"].termination == "differential"
    assert config.channels["ch2"].termination == "differential"

    # the manager hands the plan to the driver (no hardware needed: the
    # channel map is validated at load time)
    from hardware.core.hardware_manager import HardwareManager
    manager = HardwareManager()
    manager.load_config(_example_path())
    driver = manager.acquisition
    assert isinstance(driver, NIDAQmxAcquisition)
    assert driver._channel_plan == [("ch1", "differential"), ("ch2", "differential")]
    assert manager.acquisition_max_rate_hz == 1_000_000.0


def _patch_example(tmp_path, mutate):
    import json
    with open(_example_path()) as f:
        data = json.load(f)
    mutate(data)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_config_rejects_bad_termination(tmp_path):
    from hardware.registry import registry_descriptors
    from hardware.config import load_hardware_config, ConfigValidationError

    def mutate(data):
        data["channels"]["ch1"]["termination"] = "nrse"

    with pytest.raises(ConfigValidationError) as excinfo:
        load_hardware_config(_patch_example(tmp_path, mutate), registry_descriptors())
    assert any("termination" in e for e in excinfo.value.errors)


def test_config_rejects_daqmx_without_device(tmp_path):
    from hardware.registry import registry_descriptors
    from hardware.config import load_hardware_config, ConfigValidationError

    def mutate(data):
        data["devices"]["ni_6451"]["connection"] = {"type": "daqmx"}

    with pytest.raises(ConfigValidationError) as excinfo:
        load_hardware_config(_patch_example(tmp_path, mutate), registry_descriptors())
    assert any("connection.device" in e for e in excinfo.value.errors)


def test_config_rejects_too_many_channels(tmp_path):
    from hardware.registry import registry_descriptors
    from hardware.config import load_hardware_config, ConfigValidationError

    def mutate(data):
        data["devices"]["ni_6451"]["capabilities"]["acquisition"]["num_channels"] = 5

    with pytest.raises(ConfigValidationError) as excinfo:
        load_hardware_config(_patch_example(tmp_path, mutate), registry_descriptors())
    assert any("num_channels" in e for e in excinfo.value.errors)


def test_config_rejects_wrong_bits(tmp_path):
    from hardware.registry import registry_descriptors
    from hardware.config import load_hardware_config, ConfigValidationError

    def mutate(data):
        data["devices"]["ni_6451"]["capabilities"]["acquisition"]["adc"]["bits"] = 16

    with pytest.raises(ConfigValidationError) as excinfo:
        load_hardware_config(_patch_example(tmp_path, mutate), registry_descriptors())
    assert any("bits" in e for e in excinfo.value.errors)


# --------------------------------------------- stop ordering (B32)

def test_stop_stops_timing_before_the_acquisition(fake_nidaqmx):
    """Mirror of the B24 start order: the timing engine ('E') must stop
    BEFORE the acquisition — otherwise the CONV_START edges keep flowing
    during the drain, which never empties and strands the buffered tail
    (and makes every stop take the full join timeout)."""
    from services.hardware_service import HardwareService

    driver = make_driver()
    sink = StubSink()
    timing = FakeTimingDevice()
    manager = FakeManager(acquisition=driver, timing=timing)
    # one shared timeline: the acquisition stop logs into the same list
    original_stop = driver.stop_measurement

    def spy_stop():
        manager.calls.append("acq_stop")
        return original_stop()

    driver.stop_measurement = spy_stop

    service = HardwareService(manager, data_model=None, session_model=make_session())
    assert service.start_receiving(simulate=False, ramp_sink=sink) is True
    assert service.stop_receiving(send_stop=True) is True

    assert manager.calls == ["timing_start", "timing_stop", "acq_stop"]
    assert timing.calls == ["S", "E"]
    assert timing.task_armed_at_start is True


# ------------------------------------- configure-time reason (B33)

def test_configure_timing_raises_the_mirror_reason():
    """configure_timing validates through the driver's mirror before
    touching hardware: an unrepresentable request raises ValueError with
    the reason (the wizard shows it) instead of a generic apply failure."""
    from hardware.core.hardware_manager import HardwareManager
    from hardware.interfaces import ActualTiming

    class LimitedTiming:
        supports_ramp_sampling = True

        def calculate_ramp_timing(self, dt, int_us, reset_us):
            if dt > 4369.0:
                raise ValueError("ramp dt 100000 us is out of range: capped")
            return ActualTiming(int_us, reset_us, int_us + reset_us + 2.0, 1e6 / (int_us + reset_us + 2.0))

        def configure_ramp(self, dt, int_us, reset_us, period_us=None):
            self.configured = (dt, int_us, reset_us)
            return True

        def configure_noramp(self, int_us, reset_us, period_us=None):
            return True

    timing = LimitedTiming()
    manager = HardwareManager()
    manager.timing = timing
    manager.acquisition = FakeAcquisitionSpy()

    with pytest.raises(ValueError, match="out of range"):
        manager.configure_timing(False, None, 100_000.0, 500.0)

    # and a representable request proceeds to the driver call
    assert manager.configure_timing(False, None, 100.0, 500.0) is True
    assert timing.configured == (100.0, 100.0, 500.0)  # markers: dt = int


def test_time_window_failure_branch_makes_the_status_frame_visible():
    """The preview reports rejections while the user is still editing —
    the status frame must become visible in the failure branch, not only
    on the first Apply press."""
    import inspect

    from views.time_window import TimeWindow

    source = inspect.getsource(TimeWindow.on_configuration_result)
    assert "self.status_frame.setVisible(True)" in source
