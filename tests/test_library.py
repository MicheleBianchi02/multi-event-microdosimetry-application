"""
Tests for the shipped component library and the dummy acquisition driver.
"""
import copy
import os

import pytest

from hardware.config import editor_model as em
from helpers import FULL_REGISTRY, ganged_config
from hardware.config.schema import DriverDescriptor
from hardware.core.hardware_manager import HardwareManager

LIBRARY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "hardware_library"
)
DUMMY_DESCRIPTOR = DriverDescriptor(capabilities=frozenset({"acquisition"}))
REGISTRY = {**FULL_REGISTRY, "dummy_v1": DUMMY_DESCRIPTOR}


def test_all_shipped_fragments_are_valid():
    fragments, problems = em.list_fragments([LIBRARY_DIR])
    assert problems == []
    kinds = sorted((f["kind"], f["suggested_id"]) for f in fragments)
    assert kinds == [
        ("circuit", "board_ch1"),
        ("circuit", "board_ch2"),
        ("device", "dummy_acq"),
        ("device", "ni_6451"),
        ("device", "pic_1"),
        ("device", "pico_1"),
    ]


def test_pic_fragment_declares_the_2s_reset_ceiling():
    """The PIC firmware generates the reset phase with a single timer period
    (max 2 s, no TMR1U prescaler — unlike the integration path's 512 s): the
    shipped fragment must not re-declare the old wide envelope, or configs
    assembled from it would refuse to load."""
    from hardware.pic.pic_daq import PICSerialDAQ

    fragment = em.load_fragment(os.path.join(LIBRARY_DIR, "device_pic.json"))
    reset = fragment["data"]["capabilities"]["timing"]["reset_time_us"]
    assert reset["max"] <= PICSerialDAQ.HARDWARE_LIMITS.reset_time_us.max
    assert PICSerialDAQ.HARDWARE_LIMITS.reset_time_us.max == 2_000_000


def test_saved_device_fragment_keeps_connection_and_reimports(tmp_path):
    """B18: 'Save as Component' keeps the connection block — it carries
    nothing machine-specific anymore (no port; the persistent name is
    derived from the device id) — so a saved fragment re-imports cleanly."""
    import json

    device = copy.deepcopy(ganged_config()["devices"]["pico_1"])
    device["connection"]["port"] = "/dev/stale"     # legacy field of an old config
    device.get("connection", {}).pop("port", None)  # what _save_component strips

    fragment = em.fragment_to_json(device, "device", "saved_pico")
    path = tmp_path / "saved_pico.json"
    path.write_text(json.dumps(fragment))

    loaded = em.load_fragment(str(path))
    assert loaded["kind"] == "device"
    assert loaded["data"]["connection"]["type"] == "serial"

    # assemble a minimal bench from the saved fragment (the "Add from
    # Library…" path) — it must validate without errors
    fragments = {f["suggested_id"]: f for f in em.list_fragments([LIBRARY_DIR])[0]}
    cfg = em.new_config()
    pico_id = em.add_device(cfg, loaded["data"], loaded.get("suggested_id", "device"))
    del cfg["devices"][pico_id]["capabilities"]["acquisition"]  # dummy fills that role
    dummy_id = em.add_device(cfg, em.load_fragment(fragments["dummy_acq"]["path"])["data"], "dummy_acq")
    board_id = em.add_circuit(cfg, em.load_fragment(fragments["board_ch1"]["path"])["data"], "board_ch1")
    em.set_role(cfg, "timing", pico_id)
    em.set_role(cfg, "gain_ch1", pico_id)
    em.set_role(cfg, "acquisition", dummy_id)
    em.set_channel(cfg, "ch1", board_id)
    for cap_id, pin in (("c100", 17), ("c560", 18), ("c3300", 19)):
        em.set_connection(cfg, "ch1", cap_id, pin)
    errors, _warnings = em.validate(cfg, REGISTRY)
    assert errors == []


def test_library_assembly_of_bench_test_config():
    """Timing+gain on the Pico, virtual acquisition — as assembled in the editor."""
    cfg = em.new_config()
    fragments = {f["suggested_id"]: f for f in em.list_fragments([LIBRARY_DIR])[0]}

    pico = em.load_fragment(fragments["pico_1"]["path"])
    dummy = em.load_fragment(fragments["dummy_acq"]["path"])
    board = em.load_fragment(fragments["board_ch1"]["path"])

    pico_id = em.add_device(cfg, pico["data"], pico["suggested_id"])
    # the same physical Pico cannot also be the acquisition when the virtual
    # device fills that role: strip its acquisition capability like the editor
    # user would (roles are assigned explicitly)
    del cfg["devices"][pico_id]["capabilities"]["acquisition"]
    dummy_id = em.add_device(cfg, dummy["data"], dummy["suggested_id"])
    board_id = em.add_circuit(cfg, board["data"], board["suggested_id"])

    em.set_role(cfg, "timing", pico_id)
    em.set_role(cfg, "gain_ch1", pico_id)
    em.set_role(cfg, "acquisition", dummy_id)
    em.set_channel(cfg, "ch1", board_id)
    for cap_id, pin in (("c100", 17), ("c560", 18), ("c3300", 19)):
        em.set_connection(cfg, "ch1", cap_id, pin)

    errors, warnings = em.validate(cfg, REGISTRY)
    assert errors == []
    assert warnings == []


def test_dummy_acquisition_in_manager(tmp_path):
    from hardware.dummy.dummy_acquisition import DummyAcquisition
    from hardware.registry import DRIVER_REGISTRY

    cfg = copy.deepcopy(ganged_config())
    cfg["devices"]["dummy_acq"] = {
        "name": "Virtual acquisition",
        "driver": "dummy_v1",
        "connection": {"type": "dummy"},
        "capabilities": {
            "acquisition": {
                "num_channels": 2,
                "adc": {"bits": 12, "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}]},
            }
        },
    }
    # replace the pico acquisition role with the dummy; drop its capability
    del cfg["devices"]["pico_1"]["capabilities"]["acquisition"]
    cfg["roles"]["acquisition"] = "dummy_acq"

    path = tmp_path / "bench.json"
    path.write_text(__import__("json").dumps(cfg))

    manager = HardwareManager()
    manager.load_config(str(path))
    assert isinstance(manager.acquisition, DummyAcquisition)
    assert manager.acquisition_is_virtual
    for device in manager.devices.values():  # no real serial ports on the bench CI
        if not getattr(device, "IS_VIRTUAL", False):
            device.connect = lambda: True
    assert manager.connect_all() == {}  # empty dict = every device connected
    assert manager.acquisition.arm() is True
    assert manager.acquisition.start_measurement(None, None) is True
    assert manager.acquisition.stop_measurement() is True

    # ramp exclusivity: same device holds timing+acquisition? no (dummy) -> allowed
    assert manager.can_ramp_while_acquiring is True


def test_pico_ramp_exclusivity(tmp_path):
    manager = HardwareManager()
    manager.load_config(
        str(_write(tmp_path, "single.json", _single_channel_pico()))
    )
    # timing and acquisition on the same Pico: ramp forbidden by firmware
    assert manager.can_ramp_while_acquiring is False


def _single_channel_pico() -> dict:
    return all_pico()


def all_pico() -> dict:
    from helpers import all_pico_config

    return all_pico_config()


def _write(tmp_path, name, cfg):
    import json

    path = tmp_path / name
    path.write_text(json.dumps(cfg))
    return path
