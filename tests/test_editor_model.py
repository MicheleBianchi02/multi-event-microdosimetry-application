"""
Tests for the hardware config editor model (pure dict-level logic).
"""
import copy

import pytest

from hardware.config import editor_model as em
from helpers import FULL_REGISTRY, all_pico_config, ganged_config
from hardware.config.schema import DriverDescriptor

DUMMY_DESCRIPTOR = DriverDescriptor(capabilities=frozenset({"acquisition"}))
REGISTRY = {**FULL_REGISTRY, "dummy_v1": DUMMY_DESCRIPTOR}


def sample_config() -> dict:
    return all_pico_config()


def pico_device_data() -> dict:
    return copy.deepcopy(sample_config()["devices"]["pico_1"])


def board_data() -> dict:
    return copy.deepcopy(sample_config()["circuits"]["board_ch1"])


def dummy_device_data() -> dict:
    return {
        "name": "Virtual acquisition",
        "driver": "dummy_v1",
        "connection": {"type": "dummy"},
        "capabilities": {
            "acquisition": {
                "num_channels": 2,
                "adc": {
                    "bits": 12,
                    "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 5.0}],
                },
            }
        },
    }


class TestFragments:
    def test_validate_fragment(self):
        em.validate_fragment({"kind": "device", "data": {"driver": "pico_serial_v1"}})
        em.validate_fragment(
            {"kind": "circuit", "data": {"fixed_capacitance_pF": 22.0}}
        )
        with pytest.raises(ValueError):
            em.validate_fragment({"kind": "macro", "data": {}})
        with pytest.raises(ValueError):
            em.validate_fragment({"kind": "device", "data": {"name": "x"}})

    def test_list_fragments(self, tmp_path):
        (tmp_path / "d.json").write_text(
            '{"kind": "device", "suggested_id": "pico", "data": {"driver": "pico_serial_v1"}}'
        )
        (tmp_path / "bad.json").write_text("{nope")
        fragments, problems = em.list_fragments([str(tmp_path)])
        assert len(fragments) == 1
        assert fragments[0]["suggested_id"] == "pico"
        assert len(problems) == 1
        # listing entries have no 'data': they must go through resolve_fragment
        assert "data" not in fragments[0]

    def test_resolve_fragment_from_listing_entry(self, tmp_path):
        (tmp_path / "d.json").write_text(
            '{"kind": "device", "suggested_id": "pico", "data": {"driver": "pico_serial_v1", "name": "Pico"}}'
        )
        listing, _ = em.list_fragments([str(tmp_path)])
        resolved = em.resolve_fragment(listing[0])  # the library-menu path
        assert resolved["kind"] == "device"
        assert resolved["data"]["driver"] == "pico_serial_v1"
        assert em.resolve_fragment({"kind": "device", "data": {"driver": "x"}})["data"]["driver"] == "x"
        with pytest.raises(ValueError):
            em.resolve_fragment({"kind": "device"})


class TestAssembly:
    def test_add_device_uniquifies_id(self):
        cfg = sample_config()
        new_id = em.add_device(cfg, pico_device_data(), "pico_1")
        assert new_id != "pico_1"
        assert len(cfg["devices"]) == 2

    def test_remove_device_clears_roles_and_connections(self):
        cfg = sample_config()
        notes = em.remove_device(cfg, "pico_1")
        assert cfg["devices"] == {}
        assert all(v is None for v in cfg["roles"].values())
        assert cfg["connections"] == []
        assert notes  # user is told what was released

    def test_remove_circuit_removes_channel_and_wiring(self):
        cfg = ganged_config()
        notes = em.remove_circuit(cfg, "board_ch2")
        assert "board_ch2" not in cfg["circuits"]
        assert "ch2" not in cfg["channels"]
        assert cfg["roles"]["gain_ch2"] is None
        assert all(c["circuit"] != "board_ch2" for c in cfg["connections"])
        assert notes

    def test_set_channel_clears_gain_role_on_removal(self):
        cfg = sample_config()
        em.set_channel(cfg, "ch1", None)
        assert "ch1" not in cfg["channels"]
        assert cfg["roles"]["gain_ch1"] is None

    def test_rename_capacitor_updates_connections(self):
        cfg = sample_config()
        em.rename_capacitor(cfg, "board_ch1", "c100", "c120")
        assert cfg["circuits"]["board_ch1"]["switchable_capacitors"][0]["id"] == "c120"
        wired = [c["capacitor"] for c in cfg["connections"] if c["circuit"] == "board_ch1"]
        assert "c120" in wired and "c100" not in wired

    def test_remove_capacitor_drops_edge(self):
        cfg = sample_config()
        em.remove_capacitor(cfg, "board_ch1", "c3300")
        assert cfg["circuits"]["board_ch1"]["switchable_capacitors"][-1]["id"] == "c560"
        assert all(c["capacitor"] != "c3300" for c in cfg["connections"])

    def test_pending_channel_declared_but_unassigned(self):
        """Enabling ch2 before choosing its circuit must KEEP the channel
        (empty circuit), with validation pointing at the missing circuit."""
        cfg = ganged_config()
        del cfg["channels"]["ch2"]  # start single-detector
        em.set_channel(cfg, "ch2", "")
        assert "ch2" in cfg["channels"]
        errors, _ = em.validate(cfg, REGISTRY)
        assert any("ch2" in e and "circuit" in e for e in errors)
        # assigning the circuit completes it
        em.set_channel(cfg, "ch2", "board_ch2")
        errors, _ = em.validate(cfg, REGISTRY)
        assert not any("ch2" in e and "circuit" in e for e in errors)

    def test_gain_role_change_prunes_stale_wiring(self):
        """The user's scenario: wire 3 caps to a 3-pin pico, then switch the
        gain device to the 1-pin pic — stale edges must disappear, and the
        error pane must not report edges of the old device."""
        cfg = all_pico_config()
        pic = {
            "name": "PIC", "driver": "pic_serial_v1",
            "connection": {"type": "serial", "port": "", "baud_rate": 115200},
            "capabilities": {"gain": {"pins": [2]}},
        }
        em.add_device(cfg, pic, "pic_1")
        em.set_role(cfg, "gain_ch1", "pic_1")  # <- the change that stale-d the edges

        edges = [c for c in cfg["connections"] if c["circuit"] == "board_ch1"]
        assert edges == []  # none of the pico's edges survive the switch
        errors, warnings = em.validate(cfg, REGISTRY)
        assert not any("driven by device" in e for e in errors)
        # the pic can drive c100 again from pin 2:
        em.set_connection(cfg, "ch1", "c100", 2)
        errors, warnings = em.validate(cfg, REGISTRY)
        assert not any("c100" in e for e in errors)
        assert any("c560" in w and "not wired" in w for w in warnings)
        assert any("c3300" in w and "not wired" in w for w in warnings)

    def test_circuit_reassignment_prunes_old_wiring(self):
        cfg = ganged_config()
        em.add_circuit(cfg, {"name": "spare", "fixed_capacitance_pF": 10.0,
                             "switchable_capacitors": [{"id": "c47", "value_pF": 47.0}]}, "board_spare")
        em.set_channel(cfg, "ch1", "board_spare")
        assert all(c["circuit"] != "board_ch1" for c in cfg["connections"])
        assert em.connection_of(cfg, "ch1", "c47") is None  # new circuit: unwired

    def test_set_connection_replaces_edge(self):
        cfg = sample_config()
        em.set_connection(cfg, "ch1", "c100", 19)
        em.set_connection(cfg, "ch1", "c100", 17)
        edges = [
            c
            for c in cfg["connections"]
            if c["circuit"] == "board_ch1" and c["capacitor"] == "c100"
        ]
        assert len(edges) == 1 and edges[0]["pin"] == 17
        assert em.connection_of(cfg, "ch1", "c100") == 17

    def test_set_connection_unwire(self):
        cfg = sample_config()
        em.set_connection(cfg, "ch1", "c100", None)
        assert em.connection_of(cfg, "ch1", "c100") is None


class TestValidation:
    def test_valid_config_zero_errors(self):
        errors, warnings = em.validate(sample_config(), REGISTRY)
        assert errors == []

    def test_assembled_timing_plus_dummy_acquisition(self):
        """The bench-test scenario: real Pico timing+gain, virtual acquisition."""
        cfg = em.new_config()
        pico_id = em.add_device(cfg, pico_device_data(), "pico_timing")
        del cfg["devices"][pico_id]["capabilities"]["acquisition"]
        dummy_id = em.add_device(cfg, dummy_device_data(), "dummy_acq")
        board_id = em.add_circuit(cfg, board_data(), "board_ch1")
        em.set_role(cfg, "timing", pico_id)
        em.set_role(cfg, "gain_ch1", pico_id)
        em.set_role(cfg, "acquisition", dummy_id)
        em.set_channel(cfg, "ch1", board_id)
        for cap_id in ("c100", "c560", "c3300"):
            em.set_connection(cfg, "ch1", cap_id, {"c100": 17, "c560": 18, "c3300": 19}[cap_id])

        errors, _ = em.validate(cfg, REGISTRY)
        assert errors == []

    def test_edit_detects_breakage(self):
        cfg = sample_config()
        em.remove_device(cfg, "pico_1")
        errors, _ = em.validate(cfg, REGISTRY)
        assert any("roles" in e for e in errors)

    def test_unknown_driver_rejected(self):
        cfg = sample_config()
        cfg["devices"]["pico_1"]["driver"] = "ghost_v1"
        errors, _ = em.validate(cfg, REGISTRY)
        assert any("not registered" in e for e in errors)
