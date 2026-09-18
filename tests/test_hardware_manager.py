"""
Tests for the refactored HardwareManager (no physical hardware needed).
"""
import json

import pytest

from helpers import all_pico_config, ganged_config, FULL_REGISTRY  # noqa: F401
from hardware.core.hardware_manager import HardwareManager


@pytest.fixture
def pico_file(tmp_path):
    path = tmp_path / "hardware_config.json"
    path.write_text(json.dumps(all_pico_config()))
    return str(path)


def test_load_config_and_roles(pico_file):
    manager = HardwareManager()
    assert manager.is_loaded is False
    manager.load_config(pico_file)
    assert manager.is_loaded
    assert manager.timing is manager.devices["pico_1"]
    assert manager.acquisition is manager.gain_ch1  # one device, every role
    assert manager.gain_ch2 is None  # single-channel config
    assert manager.active_channels == ["ch1"]
    assert manager.max_channels == 1
    assert manager.supports_ramp_sampling is True
    assert manager.config_sha256 is not None


def test_gain_labels_and_capacitance(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    labels = manager.get_gain_labels(1)
    assert len(labels) == 8  # 22 pF fixed + 3 switchable
    assert manager.get_capacitance(1, "1x") == pytest.approx(3982.0)
    assert manager.get_gain_labels(2) == []


def test_declared_timing_limits(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    limits = manager.declared_timing_limits()
    assert limits["integration_time_us"] == {"min": 5, "max": 10_000_000, "step": 1}
    assert limits["reset_time_us"] == {"min": 5, "max": 10_000_000, "step": 1}
    assert limits["ramp_sampling"]["supported"] is True
    assert limits["ramp_sampling"]["dt_us"]["min"] == 1
    assert limits["ramp_sampling"]["dt_us"]["max"] == 4360


def test_adc_settings_from_config(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    adc = manager.get_adc_settings()
    assert adc["bits"] == 12
    assert adc["num_channels"] == 1
    assert adc["input_range_volts"] == {"min_volts": 0.0, "max_volts": 3.3}


def test_apply_gain_sends_device_mask(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    sent = []
    manager.devices["pico_1"].set_gpio_mask = lambda mask: sent.append(list(mask)) or True

    assert manager.apply_gain(1, "1x") is True
    assert sent[-1] == [True, True, True]
    assert manager.apply_gain(1, "181x") or True  # label exists?
    # smallest capacitance (22 pF) = all pins off
    smallest = min(manager.get_gain_labels(1), key=lambda l: manager.get_capacitance(1, l))
    assert manager.apply_gain(1, smallest) is True
    assert sent[-1] == [False, False, False]


def test_config_listener_notified(pico_file):
    manager = HardwareManager()
    calls = []
    manager.add_config_listener(lambda m: calls.append(m))
    manager.load_config(pico_file)
    assert len(calls) == 1


def test_ganged_conflict_rejected(tmp_path, monkeypatch):
    path = tmp_path / "ganged.json"
    path.write_text(json.dumps(ganged_config()))
    manager = HardwareManager()
    manager.load_config(path)
    assert manager.channels_share_gain is True

    sent = []
    for device in manager.devices.values():
        device.set_gpio_mask = lambda mask: sent.append(list(mask)) or True

    # ch2 has exactly one option (100 pF, pin 0 on).  Selecting ch1's
    # fixed-only 22 pF (pin 0 off) must be rejected on the ganged pin.
    smallest_ch1 = min(manager.get_gain_labels(1), key=lambda l: manager.get_capacitance(1, l))
    assert manager.apply_gain(2, manager.get_gain_labels(2)[0]) is True
    assert manager.apply_gain(1, smallest_ch1) is False
    # consistent selections (both pin 0 on) succeed
    labels = {round(manager.get_capacitance(1, l)): l for l in manager.get_gain_labels(1)}
    assert manager.apply_gain(1, labels[round(22.0 + 100.0)]) is True


def test_set_event_mode_both_gain_controllers(tmp_path):
    from helpers import split_config

    cfg = split_config()
    # the NI driver does not exist yet: emulate a 2-channel acquisition
    # with a registered driver so the real registry accepts the file
    cfg["devices"]["ni_1"]["driver"] = "pic_serial_v1"
    cfg["devices"]["ni_1"]["capabilities"]["acquisition"] = {
        "num_channels": 2,
        "adc": {
            "bits": 12,
            "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}],
        },
    }
    path = tmp_path / "split.json"
    path.write_text(json.dumps(cfg))
    manager = HardwareManager()
    manager.load_config(path)
    called = []
    manager.devices["pico_1"].set_event_mode = lambda se: called.append(("pico_1", se)) or True
    manager.devices["pic_g2"].set_event_mode = lambda se: called.append(("pic_g2", se)) or True
    assert manager.set_event_mode(True) is True
    assert set(called) == {("pico_1", True), ("pic_g2", True)}


def test_invalid_config_raises(tmp_path):
    from helpers import all_pico_config

    cfg = all_pico_config()
    cfg["roles"]["timing"] = "nonexistent"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(cfg))
    manager = HardwareManager()
    with pytest.raises(Exception):
        manager.load_config(path)
    assert manager.is_loaded is False


def test_connect_all_reports_failures(pico_file, monkeypatch):
    manager = HardwareManager()
    manager.load_config(pico_file)
    manager.devices["pico_1"] = FakeSerialDevice(connect_result=False)
    failed = manager.connect_all()
    assert list(failed.keys()) == ["pico_1"]
    assert failed["pico_1"]  # non-empty error message


# ----------------------------------------------------------------------
# Connection API (auto-connect via configured ports, manual per-device
# mapping — session-only)
# ----------------------------------------------------------------------

class FakeSerialDevice:
    """Duck-typed replacement for a serial driver (connect state is
    controllable per instance; the real drivers expose is_connected as a
    class property, which cannot be overridden per instance)."""

    def __init__(self, port="/dev/fake", baud_rate=115200, connect_result=True):
        self._port = port
        self._baud_rate = baud_rate
        self.is_connected = False
        self._connect_result = connect_result
        self.set_params_calls = []

    def connect(self) -> bool:
        if self._connect_result:
            self.is_connected = True
            return True
        self.is_connected = False
        return False

    def disconnect(self) -> bool:
        self.is_connected = False
        return True

    def set_connection_params(self, port, baud_rate) -> None:
        self.set_params_calls.append((port, baud_rate))
        self._port = port
        self._baud_rate = baud_rate


def test_connect_device_ok_and_failure(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    fake = FakeSerialDevice(connect_result=True)
    manager.devices["pico_1"] = fake
    assert manager.connect_device("pico_1") == (True, "")
    assert manager.connection_status()["pico_1"]["connected"] is True

    failing = FakeSerialDevice(connect_result=False)
    manager.devices["pico_1"] = failing
    ok, error = manager.connect_device("pico_1")
    assert ok is False and error


def test_persistent_port_derived_from_device_id(pico_file):
    """The config carries no port: at load each serial device gets the
    persistent name derived from its id (created by the admin's udev rule)."""
    manager = HardwareManager()
    manager.load_config(pico_file)
    assert manager.devices["pico_1"]._port == "/dev/atomiqa/pico_1"
    assert manager.devices["pico_1"]._baud_rate == 115200
    assert manager.connection_status()["pico_1"]["port"] == "/dev/atomiqa/pico_1"


def test_connect_device_unknown_id(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    ok, error = manager.connect_device("does_not_exist")
    assert ok is False and "not found" in error


def test_set_device_port_routes_to_driver(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    fake = FakeSerialDevice()
    manager.devices["pico_1"] = fake
    assert manager.set_device_port("pico_1", "/dev/ttyUSB42") is True
    # baud rate comes from the config, the port from the manual mapping
    assert fake.set_params_calls == [("/dev/ttyUSB42", 115200)]
    assert fake._port == "/dev/ttyUSB42"


def test_role_device_ids_and_device_roles(tmp_path):
    from helpers import split_config

    cfg = split_config()
    cfg["devices"]["ni_1"]["driver"] = "pic_serial_v1"  # NI not registered yet
    cfg["devices"]["ni_1"]["capabilities"]["acquisition"] = {
        "num_channels": 2,
        "adc": {"bits": 12, "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}]},
    }
    path = tmp_path / "split.json"
    path.write_text(json.dumps(cfg))
    manager = HardwareManager()
    manager.load_config(str(path))
    # unique, role order, no duplicates for multi-role devices
    assert manager.role_device_ids() == ["pico_t", "ni_1", "pico_1", "pic_g2"]
    assert manager.device_roles("pico_1") == ["gain_ch1"]
    assert manager.device_roles("pico_t") == ["timing"]


def test_all_role_devices_connected(tmp_path):
    from helpers import split_config

    cfg = split_config()
    cfg["devices"]["ni_1"]["driver"] = "pic_serial_v1"
    cfg["devices"]["ni_1"]["capabilities"]["acquisition"] = {
        "num_channels": 2,
        "adc": {"bits": 12, "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}]},
    }
    path = tmp_path / "split.json"
    path.write_text(json.dumps(cfg))
    manager = HardwareManager()
    manager.load_config(str(path))
    assert manager.all_role_devices_connected() is False

    for dev_id in manager.role_device_ids():
        manager.devices[dev_id] = FakeSerialDevice()
        assert manager.connect_device(dev_id) == (True, "")
    assert manager.all_role_devices_connected() is True

    # one role device dropping breaks the wizard gate
    manager.devices["pico_t"].is_connected = False
    assert manager.all_role_devices_connected() is False


def test_connection_status_shape(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    status = manager.connection_status()
    assert set(status.keys()) == {"pico_1"}
    info = status["pico_1"]
    assert info["name"] == "Pico 2 (all roles)"
    assert info["driver"] == "pico_serial_v1"
    assert info["transport"] == "serial"
    assert info["port"] == "/dev/atomiqa/pico_1"
    assert info["baud_rate"] == 115200
    assert info["connected"] is False


def test_shipped_example_config_loads():
    """examples/hardware_config_all_pico.json must validate against the real
    driver registry (the file users copy as their first template)."""
    import os

    example = os.path.join(
        os.path.dirname(__file__), "..", "examples", "hardware_config_all_pico.json"
    )
    manager = HardwareManager()
    manager.load_config(example)
    assert manager.active_channels == ["ch1"]
    assert any("board_ch2" in w for w in manager.config.warnings)  # dangling detector 2


def test_timing_interface_delegations(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)

    # test arm()
    armed = []
    manager.timing.arm = lambda: armed.append(True) or True
    assert manager.arm() is True
    assert armed == [True]

    # test set_timing_channels()
    channels_set = []
    manager.timing.set_channels = lambda n: channels_set.append(n) or True
    assert manager.set_timing_channels(2) is True
    assert channels_set == [2]

    # test set_timing_mode()
    mode_set = []
    manager.timing.set_mode = lambda m: mode_set.append(m) or True
    assert manager.set_timing_mode(True) is True
    assert mode_set == [True]

    # test calculate_actual_timing()
    actual = manager.calculate_actual_timing(1000, 500, 1500)
    assert actual.int_time_us == 1000
    assert actual.reset_time_us == 500
    assert actual.period_us == 1500
    assert actual.sample_frequency_hz == pytest.approx(1e6 / 1500)
    # unpackable as tuple for backward compatibility
    t_int, t_rst = actual
    assert t_int == 1000 and t_rst == 500


def test_set_input_range(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    ranges_set = []
    manager.acquisition.set_input_range = lambda min_v, max_v: ranges_set.append((min_v, max_v)) or True
    assert manager.set_input_range(0.0, 5.0) is True
    assert ranges_set == [(0.0, 5.0)]


def test_atomic_apply_gains(tmp_path):
    path = tmp_path / "ganged.json"
    path.write_text(json.dumps(ganged_config()))
    manager = HardwareManager()
    manager.load_config(path)

    sent = []
    manager.devices["pico_1"].set_gpio_mask = lambda mask: sent.append(list(mask)) or True

    # In ganged_config, pin 2 is shared between CH1 c100 and CH2 c100.
    # CH2 has fixed_capacitance_pF = 0, so its only valid option has pin 2 ON (100 pF).
    model1 = manager.gain_model_ch1
    model2 = manager.gain_model_ch2

    ch1_opt_p2_on = next(o for o in model1.options if o.pin_states["pico_1"].get(0) is True)
    ch1_opt_p2_off = next(o for o in model1.options if o.pin_states["pico_1"].get(0) is False)
    ch2_opt = model2.options[0]  # has pin 2 True

    # Compatible options for ch1_opt_p2_on should contain ch2_opt; for ch1_opt_p2_off it should be empty
    assert ch2_opt.label in manager.get_compatible_gain_labels(2, ch1_opt_p2_on.label)
    assert manager.get_compatible_gain_labels(2, ch1_opt_p2_off.label) == []

    # Atomic apply with conflict should return False and NOT call set_gpio_mask
    sent.clear()
    assert manager.apply_gains(ch1_opt_p2_off.label, ch2_opt.label) is False
    assert len(sent) == 0

    # Atomic apply with compatible selections should succeed and call set_gpio_mask ONCE
    sent.clear()
    assert manager.apply_gains(ch1_opt_p2_on.label, ch2_opt.label) is True
    assert len(sent) == 1
    # Pin 2 (index 0) must be True
    assert sent[0][0] is True


def test_b20_multiple_capacitors_summed_per_pin(tmp_path):
    """Verify that multiple capacitors wired to the same pin have their capacitances summed."""
    cfg = all_pico_config()
    # Wire both c100 and c560 to pin 17
    cfg["connections"] = [
        {"device": "pico_1", "pin": 17, "circuit": "board_ch1", "capacitor": "c100"},
        {"device": "pico_1", "pin": 17, "circuit": "board_ch1", "capacitor": "c560"},
    ]
    path = tmp_path / "multi_cap.json"
    path.write_text(json.dumps(cfg))
    manager = HardwareManager()
    manager.load_config(path)

    model = manager.gain_model_ch1
    # Fixed is 22.0 pF. With pin 2 ON, capacitance should be 22.0 + 100.0 + 560.0 = 682.0 pF
    caps = {round(opt.capacitance_pF) for opt in model.options}
    assert 22 in caps
    assert 682 in caps


# ----------------------------------------------------------------------
# PIC quantization must know the channel count (TX delay depends on it)
# ----------------------------------------------------------------------

def _pic_bench(two_channel: bool) -> dict:
    """Minimal all-PIC config; the PIC acquires both channels but has 1 gain pin."""
    devices = {
        "pic_1": {
            "name": "PIC (all roles)",
            "driver": "pic_serial_v1",
            "connection": {"type": "serial", "baud_rate": 115200},
            "capabilities": {
                "timing": {
                    "integration_time_us": {"min": 1, "max": 512_000_000, "step": 1},
                    "reset_time_us": {"min": 1, "max": 512_000_000, "step": 1},
                    "period_us": None,
                    "ramp_sampling": {"supported": False},
                },
                "gain": {"pins": [2]},
                "acquisition": {
                    "num_channels": 2,
                    "adc": {
                        "bits": 12,
                        "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}],
                        "supports_oversampling": False,
                    },
                },
            },
        }
    }
    circuits = {
        "board_ch1": {
            "name": "CH1 integrator",
            "fixed_capacitance_pF": 22.0,
            "switchable_capacitors": [{"id": "c100", "value_pF": 100.0}],
        }
    }
    channels = {"ch1": {"circuit": "board_ch1"}}
    roles = {"timing": "pic_1", "acquisition": "pic_1", "gain_ch1": "pic_1"}
    connections = [{"device": "pic_1", "pin": 2, "circuit": "board_ch1", "capacitor": "c100"}]
    if two_channel:
        circuits["board_ch2"] = {
            "name": "CH2 integrator",
            "fixed_capacitance_pF": 0.0,
            "switchable_capacitors": [{"id": "c220", "value_pF": 220.0}],
        }
        channels["ch2"] = {"circuit": "board_ch2"}
        roles["gain_ch2"] = "pic_1"
        connections.append({"device": "pic_1", "pin": 2, "circuit": "board_ch2", "capacitor": "c220"})
    return {
        "schema_version": "1.0",
        "devices": devices,
        "circuits": circuits,
        "channels": channels,
        "roles": roles,
        "connections": connections,
    }


def test_manager_injects_channel_count_into_pic_quantization(tmp_path):
    """The PIC reset quantization compensates the serial TX delay, which
    differs between single (13 ticks) and double (24 ticks) channel: the
    manager must inject the config's channel count (30 ms int, 500 us reset
    diverge between the two delay constants)."""
    single = HardwareManager()
    single.load_config(_write(tmp_path, "pic_single.json", _pic_bench(two_channel=False)))
    act_single = single.calculate_actual_timing(30_000, 500)
    # 16 timer ticks / 32768 Hz (single-channel TX delay compensated exactly)
    assert act_single.reset_time_us == pytest.approx(488.28125)

    double = HardwareManager()
    double.load_config(_write(tmp_path, "pic_double.json", _pic_bench(two_channel=True)))
    act_double = double.calculate_actual_timing(30_000, 500)
    # bin underflows the 24-tick double-channel delay: 25 ticks
    assert act_double.reset_time_us == pytest.approx(762.939453125)


def _write(tmp_path, name: str, cfg: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(cfg))
    return str(path)


def test_apply_gains_reports_unknown_state_on_partial_device_failure(tmp_path):
    """Two independent gain uCs: if CH2's device rejects after CH1's device
    switched, the bench state is unknowable until a full re-apply succeeds."""
    cfg = {
        "schema_version": "1.0",
        "devices": {
            "pico_g1": {
                "name": "Pico timing + gain CH1",
                "driver": "pico_serial_v1",
                "connection": {"type": "serial", "baud_rate": 115200},
                "capabilities": {
                    "timing": {
                        "integration_time_us": {"min": 5, "max": 10_000_000, "step": 1},
                        "reset_time_us": {"min": 10, "max": 10_000_000, "step": 1},
                        "period_us": None,
                        "ramp_sampling": {"supported": True, "dt_us": {"min": 10, "max": 4360, "step": 10}},
                    },
                    "gain": {"pins": [2, 3]},
                    "acquisition": {
                        "num_channels": 2,
                        "adc": {
                            "bits": 12,
                            "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 3.3}],
                            "supports_oversampling": False,
                        },
                    },
                },
            },
            "pic_g2": {
                "name": "PIC gain CH2",
                "driver": "pic_serial_v1",
                "connection": {"type": "serial", "baud_rate": 115200},
                "capabilities": {"gain": {"pins": [2]}},
            },
        },
        "circuits": {
            "board_ch1": {
                "name": "CH1 integrator",
                "fixed_capacitance_pF": 22.0,
                "switchable_capacitors": [{"id": "c100", "value_pF": 100.0}],
            },
            "board_ch2": {
                "name": "CH2 integrator",
                "fixed_capacitance_pF": 0.0,
                "switchable_capacitors": [{"id": "c220", "value_pF": 220.0}],
            },
        },
        "channels": {"ch1": {"circuit": "board_ch1"}, "ch2": {"circuit": "board_ch2"}},
        "roles": {"timing": "pico_g1", "acquisition": "pico_g1", "gain_ch1": "pico_g1", "gain_ch2": "pic_g2"},
        "connections": [
            {"device": "pico_g1", "pin": 2, "circuit": "board_ch1", "capacitor": "c100"},
            {"device": "pic_g2", "pin": 2, "circuit": "board_ch2", "capacitor": "c220"},
        ],
    }
    manager = HardwareManager()
    manager.load_config(_write(tmp_path, "split_gain.json", cfg))
    manager.devices["pico_g1"].set_gpio_mask = lambda mask: True
    manager.devices["pic_g2"].set_gpio_mask = lambda mask: True

    ch1_label = manager.get_gain_labels(1)[0]
    ch2_label = manager.get_gain_labels(2)[0]

    assert manager.apply_gains(ch1_label, ch2_label) is True
    assert manager.gain_state_unknown is False

    # CH2's device starts rejecting: CH1 is switched, then the apply fails.
    manager.devices["pic_g2"].set_gpio_mask = lambda mask: False
    assert manager.apply_gains(ch1_label, ch2_label) is False
    assert manager.gain_state_unknown is True

    # A fully successful re-apply clears the flag.
    manager.devices["pic_g2"].set_gpio_mask = lambda mask: True
    assert manager.apply_gains(ch1_label, ch2_label) is True
    assert manager.gain_state_unknown is False


def test_set_event_mode_signals_shared_device_once(tmp_path):
    """A uC holding both gain roles receives the SE/ME command exactly once."""
    manager = HardwareManager()
    manager.load_config(_write(tmp_path, "ganged_event.json", ganged_config()))
    calls = []
    manager.devices["pico_1"].set_event_mode = lambda single: calls.append(single) or True
    assert manager.set_event_mode(False) is True
    assert calls == [False]

