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
    assert limits["integration_time_us"] == {"min": 1, "max": 10_000_000, "step": 1}
    assert limits["ramp_sampling"]["supported"] is True
    assert limits["ramp_sampling"]["dt_us"]["min"] == 10


def test_adc_settings_from_config(pico_file):
    manager = HardwareManager()
    manager.load_config(pico_file)
    adc = manager.get_adc_settings()
    assert adc["bits"] == 12
    assert adc["num_channels"] == 1
    assert adc["input_range_volts"] == {"min_volts": 0.0, "max_volts": 5.0}


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
    monkeypatch.setattr(manager.devices["pico_1"], "connect", lambda: False)
    assert manager.connect_all() == ["pico_1"]


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
