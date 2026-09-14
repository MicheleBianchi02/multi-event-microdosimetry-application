"""
Tests for the hardware config loader, validator and gain model.
"""
import copy
import json

import pytest

from hardware.config import (
    ConfigValidationError,
    DriverDescriptor,
    FixedValue,
    ParameterBounds,
    RampSamplingConfig,
    TimingCapability,
    build_channel_gain_model,
    channels_share_gain_device,
    combined_pin_states,
    hardware_config_from_dict,
    load_hardware_config,
)

from helpers import (
    FULL_REGISTRY,
    DUMMY_DESCRIPTOR,
    all_pico_config,
    fixed_reset_registry,
    ganged_config,
    split_config,
)


def load(cfg: dict, registry=None):
    return hardware_config_from_dict(copy.deepcopy(cfg), registry)


def expect_error(cfg: dict, fragment: str, registry=None):
    with pytest.raises(ConfigValidationError) as exc_info:
        load(cfg, registry)
    messages = "\n".join(exc_info.value.errors)
    assert fragment in messages, f"expected {fragment!r} in:\n{messages}"


# --------------------------------------------------------------------------
# Valid configurations
# --------------------------------------------------------------------------

class TestValidConfigs:
    def test_all_pico_valid(self):
        config = load(all_pico_config(), FULL_REGISTRY)
        assert config.warnings == []
        assert set(config.channels) == {"ch1"}

    def test_split_valid_with_fixed_reset(self):
        config = load(split_config(), FULL_REGISTRY)
        assert config.warnings == []
        reset = config.devices["pico_t"].timing.reset_time_us
        assert isinstance(reset, FixedValue) and reset.value == 500

    def test_single_detector(self):
        cfg = ganged_config()
        del cfg["channels"]["ch2"]
        del cfg["roles"]["gain_ch2"]
        del cfg["circuits"]["board_ch2"]
        cfg["connections"] = [c for c in cfg["connections"] if c["circuit"] != "board_ch2"]
        config = load(cfg, FULL_REGISTRY)
        assert set(config.channels) == {"ch1"}

    def test_load_from_file(self, tmp_path):
        path = tmp_path / "hardware_config.json"
        path.write_text(json.dumps(all_pico_config()))
        config = load_hardware_config(str(path), FULL_REGISTRY)
        assert "pico_1" in config.devices


# --------------------------------------------------------------------------
# Structural errors
# --------------------------------------------------------------------------

class TestStructuralErrors:
    def test_wrong_schema_version(self):
        cfg = all_pico_config()
        cfg["schema_version"] = "2.0"
        expect_error(cfg, "unsupported schema_version")

    def test_missing_name(self):
        cfg = all_pico_config()
        del cfg["devices"]["pico_1"]["name"]
        expect_error(cfg, "missing 'name'")

    def test_ramp_block_mandatory(self):
        cfg = all_pico_config()
        del cfg["devices"]["pico_1"]["capabilities"]["timing"]["ramp_sampling"]
        expect_error(cfg, "ramp_sampling")

    def test_ramp_dt_with_supported_false(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["timing"]["ramp_sampling"] = {
            "supported": False,
            "dt_us": {"min": 10, "max": 100, "step": 10},
        }
        expect_error(cfg, "declared but 'supported' is false")

    def test_bad_parameter_bounds(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["timing"]["integration_time_us"] = {
            "min": 100, "max": 10, "step": 1
        }
        expect_error(cfg, "min <= max")

    def test_value_combined_with_bounds(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["timing"]["reset_time_us"] = {
            "value": 500, "min": 1
        }
        expect_error(cfg, "cannot be combined")

    def test_missing_default_range(self):
        cfg = split_config()
        del cfg["devices"]["ni_1"]["capabilities"]["acquisition"]["adc"]["default_input_range_volts"]
        expect_error(cfg, "default_input_range_volts", FULL_REGISTRY)

    def test_default_range_not_in_list(self):
        cfg = split_config()
        adc = cfg["devices"]["ni_1"]["capabilities"]["acquisition"]["adc"]
        adc["default_input_range_volts"] = {"min_volts": -1.0, "max_volts": 1.0}
        expect_error(cfg, "must exactly match", FULL_REGISTRY)

    def test_default_range_on_fixed_adc(self):
        cfg = all_pico_config()
        adc = cfg["devices"]["pico_1"]["capabilities"]["acquisition"]["adc"]
        adc["default_input_range_volts"] = {"min_volts": 0.0, "max_volts": 5.0}
        expect_error(cfg, "must not be set when the range is fixed")

    def test_empty_pins(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["gain"]["pins"] = []
        expect_error(cfg, "non-empty list")

    def test_unknown_channel(self):
        cfg = all_pico_config()
        cfg["channels"]["ch3"] = {"circuit": "board_ch1"}
        expect_error(cfg, "unknown channel")

    def test_ch2_without_ch1(self):
        cfg = ganged_config()
        del cfg["channels"]["ch1"]
        expect_error(cfg, "'ch2' declared without 'ch1'")

    def test_unknown_role_name(self):
        cfg = all_pico_config()
        cfg["roles"]["timming"] = "pico_1"
        expect_error(cfg, "unknown role")

    def test_capacitor_without_id(self):
        cfg = all_pico_config()
        del cfg["circuits"]["board_ch1"]["switchable_capacitors"][0]["id"]
        expect_error(cfg, "missing 'id'")


# --------------------------------------------------------------------------
# Semantic errors (cross-references, roles, wiring)
# --------------------------------------------------------------------------

class TestSemanticErrors:
    def test_role_unknown_device(self):
        cfg = all_pico_config()
        cfg["roles"]["timing"] = "pico_9"
        expect_error(cfg, "unknown device")

    def test_role_without_capability_block(self):
        cfg = split_config()
        cfg["roles"]["timing"] = "pic_g2"  # PIC has no timing block
        expect_error(cfg, "no 'timing' capability block", FULL_REGISTRY)

    def test_channel_count_exceeds_acquisition(self):
        cfg = ganged_config()
        cfg["devices"]["pico_1"]["capabilities"]["acquisition"]["num_channels"] = 1
        expect_error(cfg, "can acquire", FULL_REGISTRY)

    def test_missing_gain_role(self):
        cfg = all_pico_config()
        del cfg["roles"]["gain_ch1"]
        expect_error(cfg, "gain_ch1")

    def test_circuit_shared_by_two_channels(self):
        cfg = ganged_config()
        cfg["channels"]["ch2"]["circuit"] = "board_ch1"
        expect_error(cfg, "already used by")

    def test_undriven_capacitor_is_warning(self):
        """A circuit may hold more capacitors than the controller drives:
        unwired ones are excluded from the gain table (warning, not error)."""
        cfg = ganged_config()
        cfg["connections"] = cfg["connections"][:3]  # drop board_ch2 edge
        config = load(cfg, FULL_REGISTRY)
        assert any("not wired" in w and "c3300" not in w for w in config.warnings)
        model = build_channel_gain_model(config, "ch2")
        assert model.options == []  # fixed = 0, nothing wired

    def test_serial_port_field_is_deprecated_warning(self):
        """The config carries no port (the persistent name is derived from
        the device id); a leftover 'port' field is ignored with a warning."""
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["connection"]["port"] = "/dev/pico_daq"
        config = load(cfg, FULL_REGISTRY)
        assert any(
            "ignored (deprecated)" in w and "/dev/atomiqa/pico_1" in w
            for w in config.warnings
        )

    def test_partially_wired_circuit_gain_table(self):
        cfg = all_pico_config()
        cfg["connections"] = [
            c for c in cfg["connections"] if c["capacitor"] == "c100"
        ]  # c560 and c3300 left unwired
        config = load(cfg, FULL_REGISTRY)
        assert any("c560" in w and "not wired" in w for w in config.warnings)
        assert any("c3300" in w and "not wired" in w for w in config.warnings)
        model = build_channel_gain_model(config, "ch1")
        caps = sorted(round(o.capacitance_pF, 3) for o in model.options)
        assert caps == [22.0, 122.0]  # fixed only, fixed + c100

    def test_capacitor_driven_by_wrong_device(self):
        cfg = split_config()
        cfg["connections"][-1] = {
            "device": "pico_1", "pin": 2, "circuit": "board_ch2", "capacitor": "c100"
        }
        expect_error(cfg, "but the gain role of ch2", FULL_REGISTRY)

    def test_pin_not_declared(self):
        cfg = all_pico_config()
        cfg["connections"][0]["pin"] = 7
        expect_error(cfg, "not declared in its gain.pins")

    def test_unknown_capacitor_in_connection(self):
        cfg = all_pico_config()
        cfg["connections"][0]["capacitor"] = "c999"
        expect_error(cfg, "unknown capacitor")

    def test_two_pins_one_capacitor(self):
        cfg = ganged_config()
        cfg["connections"].append(
            {"device": "pico_1", "pin": 4, "circuit": "board_ch1", "capacitor": "c100"}
        )
        expect_error(cfg, "more than one pin")

    def test_dangling_circuit_is_warning(self):
        cfg = all_pico_config()
        cfg["circuits"]["board_spare"] = {
            "name": "spare",
            "fixed_capacitance_pF": 22.0,
            "switchable_capacitors": [],
        }
        config = load(cfg, FULL_REGISTRY)
        assert any("board_spare" in w for w in config.warnings)


# --------------------------------------------------------------------------
# Registry cross-checks (the three-layer limits)
# --------------------------------------------------------------------------

class TestRegistryCrossChecks:
    def test_unknown_driver_code(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["driver"] = "mystery_v1"
        expect_error(cfg, "not registered", FULL_REGISTRY)

    def test_declared_bounds_exceed_hardware(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["timing"]["integration_time_us"] = {
            "min": 1, "max": 20_000_000, "step": 1
        }
        expect_error(cfg, "exceeds the hardware limits", FULL_REGISTRY)

    def test_declared_tighter_than_hardware_ok(self):
        cfg = all_pico_config()
        cfg["devices"]["pico_1"]["capabilities"]["timing"]["integration_time_us"] = {
            "min": 100, "max": 1000, "step": 10
        }
        config = load(cfg, FULL_REGISTRY)
        assert config.warnings == []

    def test_ramp_declared_on_non_ramp_driver(self):
        cfg = split_config()
        cfg["roles"]["timing"] = "pic_g2"
        cfg["devices"]["pic_g2"]["capabilities"]["timing"] = {
            "integration_time_us": {"min": 1, "max": 1000, "step": 1},
            "reset_time_us": {"min": 1, "max": 1000, "step": 1},
            "period_us": None,
            "ramp_sampling": {"supported": True, "dt_us": {"min": 10, "max": 100, "step": 10}},
        }
        expect_error(cfg, "cannot generate ramp triggers", FULL_REGISTRY)

    def test_settable_declared_but_driver_fixes(self):
        cfg = split_config()
        cfg["devices"]["pico_t"]["capabilities"]["timing"]["reset_time_us"] = {
            "min": 1, "max": 1000, "step": 1
        }
        expect_error(cfg, "driver fixes it", fixed_reset_registry())

    def test_wrong_fixed_value(self):
        cfg = split_config()
        cfg["devices"]["pico_t"]["capabilities"]["timing"]["reset_time_us"] = {"value": 600}
        expect_error(cfg, "fixes it at", fixed_reset_registry())

    def test_too_many_pins(self):
        cfg = split_config()
        cfg["devices"]["pico_1"]["capabilities"]["gain"]["pins"] = [2, 3, 4, 5]
        expect_error(cfg, "at most 3", FULL_REGISTRY)

    def test_wrong_adc_bits(self):
        cfg = split_config()
        cfg["devices"]["ni_1"]["capabilities"]["acquisition"]["adc"]["bits"] = 12
        expect_error(cfg, "16-bit ADC", FULL_REGISTRY)

    def test_capability_not_supported_by_driver(self):
        cfg = split_config()
        cfg["devices"]["ni_1"]["capabilities"]["timing"] = {
            "integration_time_us": {"min": 1, "max": 1000, "step": 1},
            "reset_time_us": {"min": 1, "max": 1000, "step": 1},
            "period_us": None,
            "ramp_sampling": {"supported": False},
        }
        expect_error(cfg, "cannot do it", FULL_REGISTRY)


# --------------------------------------------------------------------------
# Gain model
# --------------------------------------------------------------------------

class TestGainModel:
    def _single_channel_pico(self):
        return load(all_pico_config(), FULL_REGISTRY)

    def test_gain_table_ch1(self):
        config = self._single_channel_pico()
        model = build_channel_gain_model(config, "ch1")
        labels = [o.label for o in model.options]
        # 22 pF fixed + 3 switchable caps: all 8 combinations have C > 0
        assert len(model.options) == 8
        assert labels[0] == "1x"
        # largest capacitance 22+100+560+3300 = 3982 pF
        assert model.options[0].capacitance_pF == pytest.approx(3982.0)
        # smallest = fixed only = 22 pF
        assert model.options[-1].capacitance_pF == pytest.approx(22.0)
        expected = 3982.0 / 22.0
        assert model.options[-1].label == f"{expected:g}x"

    def test_gpio_mask_follows_pin_order(self):
        config = self._single_channel_pico()
        model = build_channel_gain_model(config, "ch1")
        by_cap = {round(o.capacitance_pF, 3): o for o in model.options}
        # only c3300 (pin 4, index 2) switched on
        option = by_cap[22.0 + 3300.0]
        assert option.mask_for("pico_1", 3) == (False, False, True)
        # c100 + c560 (pins 2,3 = indices 0,1)
        option = by_cap[22.0 + 100.0 + 560.0]
        assert option.mask_for("pico_1", 3) == (True, True, False)

    def test_ganged_pins_share_device(self):
        config = load(ganged_config())
        assert channels_share_gain_device(config) is True

    def test_ganged_pin_conflict_detected(self):
        config = load(ganged_config())
        m1 = build_channel_gain_model(config, "ch1")
        m2 = build_channel_gain_model(config, "ch2")
        # ch2's only option is c100 ON (fixed cap 0).  ch1 = fixed only
        # (22 pF, pin 0 off) conflicts with it on the ganged pin 0.
        opt_ch1 = next(o for o in m1.options if o.capacitance_pF == pytest.approx(22.0))
        opt_ch2 = next(o for o in m2.options if o.capacitance_pF == pytest.approx(100.0))
        assert combined_pin_states([opt_ch1, opt_ch2]) is None
        # both channels c100 on: no conflict, merged state has pin 0 on
        opt_ch1 = next(o for o in m1.options if o.capacitance_pF == pytest.approx(122.0))
        opt_ch2 = next(o for o in m2.options if o.capacitance_pF == pytest.approx(100.0))
        merged = combined_pin_states([opt_ch1, opt_ch2])
        assert merged["pico_1"][0] is True

    def test_split_setup_no_sharing(self):
        config = load(split_config(), FULL_REGISTRY)
        assert channels_share_gain_device(config) is False

    def test_ch2_table_with_no_fixed_capacitance(self):
        config = load(split_config(), FULL_REGISTRY)
        model = build_channel_gain_model(config, "ch2")
        assert len(model.options) == 1  # fixed=0, one switchable: only 100 pF valid
        assert model.options[0].capacitance_pF == pytest.approx(100.0)
        assert model.options[0].label == "1x"
