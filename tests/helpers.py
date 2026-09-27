"""
Shared test helpers: hardware-config builders and registry descriptors used
by several test modules (kept here so the test files stay independent of
each other).
"""
from hardware.config.schema import (
    DriverDescriptor,
    FixedValue,
    ParameterBounds,
    RampSamplingConfig,
    TimingCapability,
    VoltageRange,
)

# Mirrors the real PicoSerialDAQ (firmware PICO2DAQ 2.1: the software
# generator's dt is bounded by the time range only; the driver enforces
# the deployed board's cap at runtime via the version gate).
PICO_DESCRIPTOR = DriverDescriptor(
    capabilities=frozenset({"timing", "gain", "acquisition"}),
    timing=TimingCapability(
        integration_time_us=ParameterBounds(5, 10_000_000, 1),
        reset_time_us=ParameterBounds(5, 10_000_000, 1),
        period_us=None,
        ramp_sampling=RampSamplingConfig(True, dt_us=ParameterBounds(1, 10_000_000, 1)),
    ),
    gain_max_pins=3,
    adc_bits=12,
    adc_native_range=VoltageRange(0.0, 3.3),
)

PIC_DESCRIPTOR = DriverDescriptor(
    capabilities=frozenset({"timing", "gain", "acquisition"}),
    timing=TimingCapability(
        integration_time_us=ParameterBounds(1, 512_000_000, 1),
        reset_time_us=ParameterBounds(1, 512_000_000, 1),
        period_us=None,
        ramp_sampling=RampSamplingConfig(False),
    ),
    gain_max_pins=1,
    adc_bits=12,
)

NI_DESCRIPTOR = DriverDescriptor(
    capabilities=frozenset({"acquisition"}),
    gain_max_pins=None,
    adc_bits=16,
    adc_native_range=None,
)

DUMMY_DESCRIPTOR = DriverDescriptor(capabilities=frozenset({"acquisition"}))

FULL_REGISTRY = {
    "pico_serial_v1": PICO_DESCRIPTOR,
    "pic_serial_v1": PIC_DESCRIPTOR,
    "ni_daqmx_v1": NI_DESCRIPTOR,
}


def _pico_bench() -> dict:
    return {
        "schema_version": "1.0",
        "devices": {
            "pico_1": {
                "name": "Pico 2 (all roles)",
                "driver": "pico_serial_v1",
                "connection": {"type": "serial", "baud_rate": 115200},
                "capabilities": {
                    "timing": {
                        "integration_time_us": {"min": 5, "max": 10_000_000, "step": 1},
                        "reset_time_us": {"min": 5, "max": 10_000_000, "step": 1},
                        "period_us": None,
                        "ramp_sampling": {
                            "supported": True,
                            "dt_us": {"min": 1, "max": 4360, "step": 1},
                        },
                    },
                    "gain": {"pins": [17, 18, 19]},
                    "acquisition": {
                        "num_channels": 1,
                        "adc": {
                            "bits": 12,
                            "input_ranges_volts": [{"min_volts": 0.0, "max_volts": 3.3}],
                            "supports_oversampling": False,
                        },
                    },
                },
            }
        },
        "circuits": {
            "board_ch1": {
                "name": "CH1 integrator",
                "fixed_capacitance_pF": 22.0,
                "switchable_capacitors": [
                    {"id": "c100", "value_pF": 100.0},
                    {"id": "c560", "value_pF": 560.0},
                    {"id": "c3300", "value_pF": 3300.0},
                ],
            },
            "board_ch2": {
                "name": "CH2 integrator",
                "fixed_capacitance_pF": 0.0,
                "switchable_capacitors": [{"id": "c100", "value_pF": 100.0}],
            },
        },
        "channels": {
            "ch1": {"circuit": "board_ch1"},
            "ch2": {"circuit": "board_ch2"},
        },
        "roles": {
            "timing": "pico_1",
            "acquisition": "pico_1",
            "gain_ch1": "pico_1",
            "gain_ch2": "pico_1",
        },
        "connections": [
            {"device": "pico_1", "pin": 17, "circuit": "board_ch1", "capacitor": "c100"},
            {"device": "pico_1", "pin": 18, "circuit": "board_ch1", "capacitor": "c560"},
            {"device": "pico_1", "pin": 19, "circuit": "board_ch1", "capacitor": "c3300"},
            {"device": "pico_1", "pin": 17, "circuit": "board_ch2", "capacitor": "c100"},
        ],
    }


def all_pico_config() -> dict:
    """Today's bench, single detector: one Pico holds every role.

    The Pico acquires one channel only, so the default fixture uses a single
    channel; use ganged_config() for the hypothetical dual-channel Pico
    needed to exercise the shared-pin wiring.
    """
    cfg = _pico_bench()
    del cfg["channels"]["ch2"]
    del cfg["roles"]["gain_ch2"]
    del cfg["circuits"]["board_ch2"]
    cfg["connections"] = [c for c in cfg["connections"] if c["circuit"] == "board_ch1"]
    return cfg


def ganged_config() -> dict:
    """Dual-channel bench on one Pico: the 3 pins drive both circuits (ganged)."""
    cfg = _pico_bench()
    cfg["devices"]["pico_1"]["capabilities"]["acquisition"]["num_channels"] = 2
    return cfg


def split_config() -> dict:
    """The general split: Pico timing, Pico+PIC gains, NI acquisition."""
    cfg = ganged_config()
    del cfg["devices"]["pico_1"]["capabilities"]["timing"]
    cfg["devices"]["pico_1"]["name"] = "Pico 2 — CH1 gain controller"
    del cfg["devices"]["pico_1"]["capabilities"]["acquisition"]
    cfg["devices"]["pic_g2"] = {
        "name": "PIC — CH2 gain controller",
        "driver": "pic_serial_v1",
        "connection": {"type": "serial", "baud_rate": 115200},
        "capabilities": {"gain": {"pins": [2]}},
    }
    cfg["devices"]["pico_t"] = {
        "name": "Pico 2 — timing controller",
        "driver": "pico_serial_v1",
        "connection": {"type": "serial", "baud_rate": 115200},
        "capabilities": {
            "timing": {
                "integration_time_us": {"min": 5, "max": 10_000_000, "step": 1},
                "reset_time_us": {"value": 500},
                "period_us": None,
                "ramp_sampling": {"supported": True, "dt_us": {"min": 1, "max": 4360, "step": 1}},
            }
        },
    }
    cfg["devices"]["ni_1"] = {
        "name": "NI readout",
        "driver": "ni_daqmx_v1",
        "connection": {"type": "daqmx", "device": "Dev1"},
        "capabilities": {
            "acquisition": {
                "num_channels": 2,
                "adc": {
                    "bits": 16,
                    "input_ranges_volts": [
                        {"min_volts": -0.2, "max_volts": 0.2},
                        {"min_volts": -2.5, "max_volts": 2.5},
                        {"min_volts": -5.0, "max_volts": 5.0},
                        {"min_volts": -10.0, "max_volts": 10.0},
                    ],
                    "default_input_range_volts": {"min_volts": -5.0, "max_volts": 5.0},
                    "supports_oversampling": False,
                },
            }
        },
    }
    cfg["roles"] = {
        "timing": "pico_t",
        "acquisition": "ni_1",
        "gain_ch1": "pico_1",
        "gain_ch2": "pic_g2",
    }
    cfg["connections"][-1] = {"device": "pic_g2", "pin": 2, "circuit": "board_ch2", "capacitor": "c100"}
    return cfg


def fixed_reset_registry() -> dict:
    """Registry where the pico driver hardcodes a fixed 500 us reset."""
    descriptor = DriverDescriptor(
        capabilities=frozenset({"timing", "gain", "acquisition"}),
        timing=TimingCapability(
            integration_time_us=ParameterBounds(5, 10_000_000, 1),
            reset_time_us=FixedValue(500),
            period_us=None,
            ramp_sampling=RampSamplingConfig(True, dt_us=ParameterBounds(0.1, 65536.0 * 10.0 / 150.0, 0.1)),
        ),
        gain_max_pins=3,
        adc_bits=12,
    )
    return {"pico_serial_v1": descriptor}
