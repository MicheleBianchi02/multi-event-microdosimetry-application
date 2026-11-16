"""
Tests for the config-driven udev rule generator (pure parts + fake sysfs).
"""
import importlib.util
import os
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "genudev", os.path.join(os.path.dirname(__file__), "..", "tools", "generate_udev_rules.py")
)
genudev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(genudev)

EXAMPLE = os.path.join(
    os.path.dirname(__file__), "..", "examples", "hardware_config_all_pico.json"
)


def test_configured_serial_devices():
    import json

    devices = genudev.configured_serial_devices(json.load(open(EXAMPLE)))
    # symlink names are derived from the device ids (the config has no port)
    assert devices == [("pico_1", "atomiqa/pico_1")]


def test_rule_contains_serial_match():
    rule = genudev.build_rule("atomiqa/pico_1", {"vendor": "2e8a", "product": "0009", "serial": "E661"})
    assert rule == 'SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0009", ATTRS{serial}=="E661", SYMLINK+="atomiqa/pico_1"'


def test_rule_without_serial_falls_back_to_vid_pid():
    rule = genudev.build_rule("atomiqa/x", {"vendor": "2e8a", "product": None, "serial": None})
    assert rule == 'SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", SYMLINK+="atomiqa/x"'


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="fakes sysfs layout and symlinks: the udev workflow is Linux-only",
)
def test_usb_enumeration_over_fake_sysfs(tmp_path, monkeypatch):
    # /sys/class/tty/ttyACM0 -> ../../devices/.../2-1:1.0/ttyUSB...-ttyACM0
    usb_root = tmp_path / "devices" / "usb1" / "2-1"
    usb_root.mkdir(parents=True)
    (usb_root / "idVendor").write_text("2e8a\n")
    (usb_root / "idProduct").write_text("0009\n")
    (usb_root / "serial").write_text("E661SERIAL\n")
    iface = usb_root / "2-1:1.0"
    iface.mkdir()
    tty_target = iface / "ttyACM0"
    tty_target.mkdir()

    class_dir = tmp_path / "class" / "tty" / "ttyACM0"
    class_dir.mkdir(parents=True)
    os.symlink(str(tty_target), str(class_dir / "device"))

    def fake_glob(pattern):
        assert pattern == "/sys/class/tty/*"
        return [str(class_dir)]

    monkeypatch.setattr(genudev.glob, "glob", fake_glob)
    monkeypatch.setattr(genudev.os.path, "realpath", lambda p: str(tty_target))
    # the attribute walk goes UP from tty_target; keep realpath identity so the
    # parent chain is the fake tree itself
    devices = genudev.usb_serial_devices()
    assert devices == [
        {"tty": "/dev/ttyACM0", "vendor": "2e8a", "product": "0009", "serial": "E661SERIAL"}
    ]
