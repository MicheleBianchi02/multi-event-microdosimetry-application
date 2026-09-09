"""
Loader for hardware_config.json: parses the file into the typed objects of
`schema.py` and validates it (structure + semantics + cross-check against
the driver registry).  All problems found are reported together; any error
raises ConfigValidationError, warnings are attached to the config.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from hardware.config.schema import (
    AcquisitionCapability,
    AdcCapability,
    CapacitorConfig,
    ChannelConfig,
    CircuitConfig,
    ConnectionConfig,
    DeviceConfig,
    DriverDescriptor,
    FixedValue,
    GainCapability,
    HardwareConfig,
    ParameterBounds,
    RampSamplingConfig,
    RolesConfig,
    TimingCapability,
    VoltageRange,
)

logger = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = "1.0"
TIMING_PARAMETER_NAMES = ("integration_time_us", "reset_time_us", "period_us")
VALID_CHANNEL_IDS = ("ch1", "ch2")
VALID_ROLE_NAMES = ("timing", "acquisition", "gain_ch1", "gain_ch2")
VALID_CAPABILITIES = ("timing", "gain", "acquisition")


class ConfigValidationError(Exception):
    """Raised when a hardware config file cannot be accepted. All errors at once."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Invalid hardware configuration:\n - " + "\n - ".join(errors))


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_hardware_config(
    path: str, driver_registry: Optional[dict[str, DriverDescriptor]] = None
) -> HardwareConfig:
    """Load and validate a hardware config file.

    Raises OSError if the file cannot be read, ConfigValidationError if it
    is not a valid configuration.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    config = hardware_config_from_dict(data, driver_registry)
    config.source_path = path  # type: ignore[attr-defined]
    return config


def hardware_config_from_dict(
    data: Any, driver_registry: Optional[dict[str, DriverDescriptor]] = None
) -> HardwareConfig:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        raise ConfigValidationError(["the file must contain a JSON object"])

    schema_version = data.get("schema_version")
    if not schema_version:
        errors.append("missing 'schema_version'")
    elif not isinstance(schema_version, str):
        errors.append("'schema_version' must be a string")
    elif schema_version != SUPPORTED_SCHEMA_VERSION:
        errors.append(
            f"unsupported schema_version {schema_version!r} "
            f"(this app understands {SUPPORTED_SCHEMA_VERSION!r})"
        )

    devices = _parse_devices(data.get("devices"), errors)
    circuits = _parse_circuits(data.get("circuits"), errors)
    channels = _parse_channels(data.get("channels"), errors)
    roles = _parse_roles(data.get("roles"), errors)
    connections = _parse_connections(data.get("connections"), errors)

    if errors:
        raise ConfigValidationError(errors)

    config = HardwareConfig(
        schema_version=schema_version,
        devices=devices,
        circuits=circuits,
        channels=channels,
        roles=roles,
        connections=connections,
        warnings=warnings,
    )

    _validate_semantics(config, errors, warnings)
    if driver_registry is not None:
        _validate_against_registry(config, driver_registry, errors, warnings)
    if errors:
        raise ConfigValidationError(errors)

    return config


# ---------------------------------------------------------------------------
# Parsing (structure + per-object checks)
# ---------------------------------------------------------------------------

def _required_str(container: dict, key: str, where: str, errors: list) -> Optional[str]:
    value = container.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(f"{where}: missing '{key}'")
        return None
    if not isinstance(value, str):
        errors.append(f"{where}: '{key}' must be a string")
        return None
    return value


def _parse_devices(raw: Any, errors: list) -> dict[str, DeviceConfig]:
    if not isinstance(raw, dict) or not raw:
        errors.append("'devices' must be a non-empty object")
        return {}

    devices: dict[str, DeviceConfig] = {}
    for dev_id, dev_raw in raw.items():
        where = f"devices.{dev_id}"
        if not isinstance(dev_raw, dict):
            errors.append(f"{where}: must be an object")
            continue

        name = _required_str(dev_raw, "name", where, errors)
        driver = _required_str(dev_raw, "driver", where, errors)

        connection = dev_raw.get("connection")
        if not isinstance(connection, dict) or "type" not in connection:
            errors.append(f"{where}.connection: must be an object with a 'type'")

        capabilities_raw = dev_raw.get("capabilities", {})
        if not isinstance(capabilities_raw, dict):
            errors.append(f"{where}.capabilities: must be an object")
            capabilities_raw = {}

        unknown = set(capabilities_raw) - set(VALID_CAPABILITIES)
        for key in unknown:
            errors.append(f"{where}.capabilities.{key}: unknown capability")

        timing = None
        if "timing" in capabilities_raw:
            timing = _parse_timing(capabilities_raw["timing"], f"{where}.capabilities.timing", errors)

        gain = None
        if "gain" in capabilities_raw:
            gain = _parse_gain(capabilities_raw["gain"], f"{where}.capabilities.gain", errors)

        acquisition = None
        if "acquisition" in capabilities_raw:
            acquisition = _parse_acquisition(
                capabilities_raw["acquisition"], f"{where}.capabilities.acquisition", errors
            )

        devices[dev_id] = DeviceConfig(
            id=dev_id,
            name=name or dev_id,
            driver=driver or "",
            connection=connection if isinstance(connection, dict) else {},
            firmware_version=dev_raw.get("firmware_version"),
            note=dev_raw.get("note"),
            timing=timing,
            gain=gain,
            acquisition=acquisition,
        )
    return devices


def _parse_parameter(raw: Any, allow_none: bool, where: str, errors: list):
    """Parse one timing parameter: {min,max,step} | {value} | null."""
    if raw is None:
        if allow_none:
            return None
        errors.append(f"{where}: this parameter cannot be null")
        return None

    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object {{min, max, step}} or {{value}}")
        return None

    if "value" in raw:
        if set(raw) - {"value"}:
            errors.append(f"{where}: 'value' cannot be combined with min/max/step")
            return None
        value = raw["value"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            errors.append(f"{where}.value: must be a positive number")
            return None
        return FixedValue(value=float(value))

    if "min" not in raw or "max" not in raw:
        errors.append(f"{where}: must have 'min' and 'max' (and optional 'step')")
        return None
    numbers = {}
    for key in ("min", "max", "step"):
        v = raw.get(key, 1)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"{where}.{key}: must be a number")
            return None
        numbers[key] = float(v)
    if numbers["min"] < 0 or numbers["min"] > numbers["max"]:
        errors.append(f"{where}: need 0 <= min <= max")
        return None
    if numbers["step"] <= 0:
        errors.append(f"{where}.step: must be positive")
        return None
    return ParameterBounds(**numbers)


def _parse_timing(raw: Any, where: str, errors: list) -> Optional[TimingCapability]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object")
        return None

    params = {}
    for name in TIMING_PARAMETER_NAMES:
        params[name] = _parse_parameter(
            raw.get(name), allow_none=(name == "period_us"), where=f"{where}.{name}", errors=errors
        )

    ramp_raw = raw.get("ramp_sampling")
    if not isinstance(ramp_raw, dict) or "supported" not in ramp_raw:
        errors.append(f"{where}.ramp_sampling: required, with an explicit 'supported' flag")
        return None

    supported = ramp_raw["supported"]
    if not isinstance(supported, bool):
        errors.append(f"{where}.ramp_sampling.supported: must be true or false")
        return None

    dt = None
    if supported:
        dt = _parse_parameter(ramp_raw.get("dt_us"), False, f"{where}.ramp_sampling.dt_us", errors)
    elif "dt_us" in ramp_raw:
        errors.append(f"{where}.ramp_sampling.dt_us: declared but 'supported' is false")

    return TimingCapability(
        integration_time_us=params["integration_time_us"],
        reset_time_us=params["reset_time_us"],
        period_us=params["period_us"],
        ramp_sampling=RampSamplingConfig(supported=supported, dt_us=dt),
    )


def _parse_gain(raw: Any, where: str, errors: list) -> Optional[GainCapability]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object")
        return None

    pins = raw.get("pins")
    if not isinstance(pins, list) or not pins:
        errors.append(f"{where}.pins: must be a non-empty list of pin identifiers")
        return None
    for pin in pins:
        if isinstance(pin, bool) or not isinstance(pin, (int, str)):
            errors.append(f"{where}.pins: identifiers must be numbers or strings")
            return None
    if len(set(map(str, pins))) != len(pins):
        errors.append(f"{where}.pins: duplicate identifiers")
        return None
    return GainCapability(pins=tuple(pins))


def _parse_acquisition(raw: Any, where: str, errors: list) -> Optional[AcquisitionCapability]:
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object")
        return None

    num_channels = raw.get("num_channels")
    if not isinstance(num_channels, int) or isinstance(num_channels, bool) or num_channels < 1:
        errors.append(f"{where}.num_channels: must be a positive integer")
        num_channels = None

    adc_raw = raw.get("adc")
    if not isinstance(adc_raw, dict):
        errors.append(f"{where}.adc: required object")
        return None

    bits = adc_raw.get("bits")
    if not isinstance(bits, int) or isinstance(bits, bool) or not 1 <= bits <= 32:
        errors.append(f"{where}.adc.bits: must be an integer in [1, 32]")
        bits = None

    ranges_raw = adc_raw.get("input_ranges_volts")
    if not isinstance(ranges_raw, list) or not ranges_raw:
        errors.append(f"{where}.adc.input_ranges_volts: must be a non-empty list")
        ranges_raw = []

    ranges = []
    for i, r in enumerate(ranges_raw):
        r_where = f"{where}.adc.input_ranges_volts[{i}]"
        if not isinstance(r, dict) or "min_volts" not in r or "max_volts" not in r:
            errors.append(f"{r_where}: must have 'min_volts' and 'max_volts'")
            continue
        lo, hi = r["min_volts"], r["max_volts"]
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            errors.append(f"{r_where}: limits must be numbers")
            continue
        if lo >= hi:
            errors.append(f"{r_where}: need min_volts < max_volts")
            continue
        ranges.append(VoltageRange(min_volts=float(lo), max_volts=float(hi)))

    default = None
    default_raw = adc_raw.get("default_input_range_volts")
    if default_raw is not None:
        if not isinstance(default_raw, dict) or "min_volts" not in default_raw or "max_volts" not in default_raw:
            errors.append(
                f"{where}.adc.default_input_range_volts: must have 'min_volts' and 'max_volts'"
            )
        else:
            default = VoltageRange(
                min_volts=float(default_raw["min_volts"]),
                max_volts=float(default_raw["max_volts"]),
            )

    if len(ranges) > 1 and default is None:
        errors.append(
            f"{where}.adc.default_input_range_volts: required when more than one "
            f"input range is declared"
        )
    if len(ranges) == 1 and default is not None:
        errors.append(
            f"{where}.adc.default_input_range_volts: must not be set when the range is fixed"
        )
    if default is not None and default not in ranges:
        errors.append(
            f"{where}.adc.default_input_range_volts: must exactly match one declared range"
        )

    oversampling = adc_raw.get("supports_oversampling", False)
    if not isinstance(oversampling, bool):
        errors.append(f"{where}.adc.supports_oversampling: must be true or false")
        oversampling = False

    if num_channels is None or bits is None or not ranges:
        return None
    return AcquisitionCapability(
        num_channels=num_channels,
        adc=AdcCapability(
            bits=bits,
            input_ranges_volts=tuple(ranges),
            default_input_range_volts=default,
            supports_oversampling=oversampling,
        ),
    )


def _parse_circuits(raw: Any, errors: list) -> dict[str, CircuitConfig]:
    if not isinstance(raw, dict) or not raw:
        errors.append("'circuits' must be a non-empty object")
        return {}

    circuits: dict[str, CircuitConfig] = {}
    for c_id, c_raw in raw.items():
        where = f"circuits.{c_id}"
        if not isinstance(c_raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        name = _required_str(c_raw, "name", where, errors)

        fixed = c_raw.get("fixed_capacitance_pF", 0.0)
        if not isinstance(fixed, (int, float)) or isinstance(fixed, bool) or fixed < 0:
            errors.append(f"{where}.fixed_capacitance_pF: must be a number >= 0")
            fixed = 0.0

        caps_raw = c_raw.get("switchable_capacitors", [])
        if caps_raw is None:
            caps_raw = []
        if not isinstance(caps_raw, list):
            errors.append(f"{where}.switchable_capacitors: must be a list (empty if none)")
            caps_raw = []

        capacitors = []
        seen_ids = set()
        for i, cap_raw in enumerate(caps_raw):
            cap_where = f"{where}.switchable_capacitors[{i}]"
            if not isinstance(cap_raw, dict):
                errors.append(f"{cap_where}: must be an object")
                continue
            cap_id = _required_str(cap_raw, "id", cap_where, errors)
            value = cap_raw.get("value_pF")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                errors.append(f"{cap_where}.value_pF: must be a positive number")
                continue
            if cap_id in seen_ids:
                errors.append(f"{cap_where}: duplicate capacitor id {cap_id!r}")
                continue
            seen_ids.add(cap_id)
            capacitors.append(CapacitorConfig(id=cap_id, value_pF=float(value), note=cap_raw.get("note")))

        circuits[c_id] = CircuitConfig(
            id=c_id,
            name=name or c_id,
            fixed_capacitance_pF=float(fixed),
            switchable_capacitors=tuple(capacitors),
            model=c_raw.get("model"),
            note=c_raw.get("note"),
        )
    return circuits


def _parse_channels(raw: Any, errors: list) -> dict[str, ChannelConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append("'channels' must be an object")
        return {}

    channels: dict[str, ChannelConfig] = {}
    for ch_id, ch_raw in raw.items():
        where = f"channels.{ch_id}"
        if ch_id not in VALID_CHANNEL_IDS:
            errors.append(f"{where}: unknown channel (valid: {', '.join(VALID_CHANNEL_IDS)})")
            continue
        if not isinstance(ch_raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        circuit = _required_str(ch_raw, "circuit", where, errors)
        channels[ch_id] = ChannelConfig(
            id=ch_id,
            circuit=circuit or "",
            adc_input=ch_raw.get("adc_input"),
            note=ch_raw.get("note"),
        )

    if "ch2" in channels and "ch1" not in channels:
        errors.append("channels: 'ch2' declared without 'ch1' (single-channel runs use ch1)")
    return channels


def _parse_roles(raw: Any, errors: list) -> RolesConfig:
    if not isinstance(raw, dict):
        errors.append("'roles' must be an object")
        return RolesConfig()

    for key in raw:
        if key not in VALID_ROLE_NAMES:
            errors.append(f"roles.{key}: unknown role (valid: {', '.join(VALID_ROLE_NAMES)})")

    roles = RolesConfig(
        timing=raw.get("timing"),
        acquisition=raw.get("acquisition"),
        gain_ch1=raw.get("gain_ch1"),
        gain_ch2=raw.get("gain_ch2"),
    )
    for name in ("timing", "acquisition"):
        if getattr(roles, name) is None:
            errors.append(f"roles.{name}: this role is required")
    return roles


def _parse_connections(raw: Any, errors: list) -> list[ConnectionConfig]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append("'connections' must be a list of wiring entries")
        return []

    connections = []
    for i, c_raw in enumerate(raw):
        where = f"connections[{i}]"
        if not isinstance(c_raw, dict):
            errors.append(f"{where}: must be an object")
            continue
        device = _required_str(c_raw, "device", where, errors)
        circuit = _required_str(c_raw, "circuit", where, errors)
        capacitor = _required_str(c_raw, "capacitor", where, errors)
        pin = c_raw.get("pin")
        if isinstance(pin, bool) or not isinstance(pin, (int, str)):
            errors.append(f"{where}.pin: must be a number or string")
            continue
        if device is None or circuit is None or capacitor is None:
            continue
        connections.append(
            ConnectionConfig(device=device, pin=pin, circuit=circuit, capacitor=capacitor, note=c_raw.get("note"))
        )
    return connections


# ---------------------------------------------------------------------------
# Semantic validation (cross-references, roles, wiring)
# ---------------------------------------------------------------------------

def _validate_semantics(config: HardwareConfig, errors: list, warnings: list) -> None:
    devices, circuits, channels, roles, connections = (
        config.devices, config.circuits, config.channels, config.roles, config.connections,
    )

    # --- roles -> devices -> capability blocks ---
    role_capability = {
        "timing": "timing",
        "acquisition": "acquisition",
        "gain_ch1": "gain",
        "gain_ch2": "gain",
    }
    for role_name in VALID_ROLE_NAMES:
        dev_id = getattr(roles, role_name)
        if dev_id is None:
            continue
        if dev_id not in devices:
            errors.append(f"roles.{role_name}: unknown device {dev_id!r}")
            continue
        device = devices[dev_id]
        capability = role_capability[role_name]
        if getattr(device, capability) is None:
            errors.append(
                f"roles.{role_name}: device {dev_id!r} has no '{capability}' capability block"
            )

    # --- channels -> circuits ---
    circuit_users: dict[str, str] = {}
    for ch_id, channel in channels.items():
        if channel.circuit not in circuits:
            errors.append(f"channels.{ch_id}.circuit: unknown circuit {channel.circuit!r}")
        elif channel.circuit in circuit_users:
            errors.append(
                f"channels.{ch_id}: circuit {channel.circuit!r} already used by "
                f"channel {circuit_users[channel.circuit]!r}"
            )
        else:
            circuit_users[channel.circuit] = ch_id

    if not channels:
        warnings.append("no channels configured: no measurement is possible with this file")

    # --- channel count vs acquisition capability (B8) ---
    acq_dev = devices.get(roles.acquisition) if roles.acquisition else None
    if acq_dev and acq_dev.acquisition:
        if len(channels) > acq_dev.acquisition.num_channels:
            errors.append(
                f"roles.acquisition: device {roles.acquisition!r} can acquire "
                f"{acq_dev.acquisition.num_channels} channel(s) but {len(channels)} are configured"
            )

    # --- gain roles required for active channels ---
    for ch_id in channels:
        gain_role = "gain_ch1" if ch_id == "ch1" else "gain_ch2"
        if getattr(roles, gain_role) is None:
            errors.append(f"roles.{gain_role}: required because {ch_id} is configured")
    if roles.gain_ch2 and "ch2" not in channels:
        warnings.append("roles.gain_ch2: assigned but ch2 is not configured (ignored)")

    # --- wiring ---
    active_circuits = {ch.circuit for ch in channels.values() if ch.circuit in circuits}
    for circuit_id in circuits:
        if circuit_id not in active_circuits:
            warnings.append(f"circuits.{circuit_id}: not bound to any channel (ignored)")

    # every switchable capacitor of an active circuit: driven by exactly one
    # pin of that channel's gain device
    for ch_id, channel in channels.items():
        circuit = circuits.get(channel.circuit)
        if circuit is None:
            continue
        gain_dev_id = roles.gain_ch1 if ch_id == "ch1" else roles.gain_ch2
        gain_dev = devices.get(gain_dev_id) if gain_dev_id else None
        if gain_dev is None or gain_dev.gain is None:
            continue  # already reported above

        by_capacitor: dict[str, ConnectionConfig] = {}
        pins_in_circuit: dict = {}
        for conn in connections:
            if conn.circuit != circuit.id:
                continue
            if conn.capacitor not in {c.id for c in circuit.switchable_capacitors}:
                errors.append(
                    f"connections: device {conn.device!r} pin {conn.pin!r} -> unknown "
                    f"capacitor {conn.capacitor!r} of circuit {circuit.id!r}"
                )
                continue
            if conn.capacitor in by_capacitor:
                errors.append(
                    f"connections: capacitor {circuit.id}.{conn.capacitor!r} is driven by "
                    f"more than one pin"
                )
                continue
            by_capacitor[conn.capacitor] = conn
            pins_in_circuit.setdefault(conn.pin, []).append(conn.capacitor)

        for pin, caps in pins_in_circuit.items():
            if len(caps) > 1:
                warnings.append(
                    f"connections: pin {pin!r} drives {len(caps)} capacitors of circuit "
                    f"{circuit.id!r}; they switch together"
                )

        for capacitor in circuit.switchable_capacitors:
            conn = by_capacitor.get(capacitor.id)
            if conn is None:
                errors.append(
                    f"connections: capacitor {circuit.id}.{capacitor.id!r} is not driven "
                    f"by any pin"
                )
                continue
            if conn.device != gain_dev_id:
                errors.append(
                    f"connections: capacitor {circuit.id}.{capacitor.id!r} is driven by "
                    f"device {conn.device!r}, but the gain role of {ch_id} is "
                    f"{gain_dev_id!r}"
                )
                continue
            if conn.pin not in gain_dev.gain.pins:
                errors.append(
                    f"connections: device {conn.device!r} pin {conn.pin!r} is not declared "
                    f"in its gain.pins {list(gain_dev.gain.pins)}"
                )

        if circuit.fixed_capacitance_pF <= 0 and not circuit.switchable_capacitors:
            errors.append(f"circuits.{circuit.id}: no fixed and no switchable capacitance")

    # connections referencing inactive circuits are ignored
    for conn in connections:
        if conn.circuit in circuits and conn.circuit not in active_circuits:
            warnings.append(
                f"connections: {conn.device}.{conn.pin} -> {conn.circuit}.{conn.capacitor} "
                f"refers to a circuit not bound to any channel (ignored)"
            )


def _validate_against_registry(
    config: HardwareConfig,
    registry: dict[str, DriverDescriptor],
    errors: list,
    warnings: list,
) -> None:
    for dev_id, device in config.devices.items():
        descriptor = registry.get(device.driver)
        if descriptor is None:
            errors.append(
                f"devices.{dev_id}: driver code {device.driver!r} is not registered in "
                f"this build"
            )
            continue

        for capability in ("timing", "gain", "acquisition"):
            if getattr(device, capability) is not None and not descriptor.supports(capability):
                errors.append(
                    f"devices.{dev_id}: declares '{capability}' but driver "
                    f"{device.driver!r} cannot do it"
                )

        declared = device.timing
        hardcoded = descriptor.timing
        if declared is not None and hardcoded is not None:
            _check_timing_within(dev_id, declared, hardcoded, errors)

        if device.gain is not None and descriptor.gain_max_pins is not None:
            if len(device.gain.pins) > descriptor.gain_max_pins:
                errors.append(
                    f"devices.{dev_id}.gain: {len(device.gain.pins)} pins declared but "
                    f"driver {device.driver!r} can drive at most {descriptor.gain_max_pins}"
                )

        if device.acquisition is not None:
            adc = device.acquisition.adc
            if descriptor.adc_bits is not None and adc.bits != descriptor.adc_bits:
                errors.append(
                    f"devices.{dev_id}.acquisition.adc.bits: {adc.bits} declared but "
                    f"driver {device.driver!r} has a {descriptor.adc_bits}-bit ADC"
                )
            if descriptor.adc_native_range is not None:
                for r in adc.input_ranges_volts:
                    if not descriptor.adc_native_range.contains(r):
                        errors.append(
                            f"devices.{dev_id}.acquisition.adc: range {r} exceeds the "
                            f"native span {descriptor.adc_native_range} of driver "
                            f"{device.driver!r}"
                        )


def _check_timing_within(dev_id: str, declared: TimingCapability, hardcoded: TimingCapability, errors: list) -> None:
    for name in TIMING_PARAMETER_NAMES:
        declared_param = declared.parameter(name)
        hardcoded_param = hardcoded.parameter(name)

        if declared_param is None:
            continue
        if hardcoded_param is None:
            errors.append(
                f"devices.{dev_id}.capabilities.timing.{name}: declared but driver "
                f"hardcodes it as not independently configurable"
            )
            continue

        if isinstance(declared_param, FixedValue):
            if isinstance(hardcoded_param, FixedValue):
                if declared_param.value != hardcoded_param.value:
                    errors.append(
                        f"devices.{dev_id}.capabilities.timing.{name}: fixed at "
                        f"{declared_param.value:g} but the driver fixes it at "
                        f"{hardcoded_param.value:g}"
                    )
            elif not hardcoded_param.contains_value(declared_param.value):
                errors.append(
                    f"devices.{dev_id}.capabilities.timing.{name}: fixed value "
                    f"{declared_param.value:g} outside the hardware limits {hardcoded_param}"
                )
        elif isinstance(hardcoded_param, FixedValue):
            errors.append(
                f"devices.{dev_id}.capabilities.timing.{name}: declared settable "
                f"({declared_param}) but the driver fixes it at {hardcoded_param.value:g}"
            )
        elif not hardcoded_param.contains_bounds(declared_param):
            errors.append(
                f"devices.{dev_id}.capabilities.timing.{name}: declared {declared_param} "
                f"exceeds the hardware limits {hardcoded_param}"
            )

    declared_ramp = declared.ramp_sampling
    hardcoded_ramp = hardcoded.ramp_sampling
    if declared_ramp.supported and not hardcoded_ramp.supported:
        errors.append(
            f"devices.{dev_id}.capabilities.timing.ramp_sampling: declared supported "
            f"but the driver cannot generate ramp triggers"
        )
    elif declared_ramp.supported and hardcoded_ramp.dt_us is not None and declared_ramp.dt_us is not None:
        if not hardcoded_ramp.dt_us.contains_bounds(declared_ramp.dt_us):
            errors.append(
                f"devices.{dev_id}.capabilities.timing.ramp_sampling.dt_us: declared "
                f"{declared_ramp.dt_us} exceeds the hardware limits {hardcoded_ramp.dt_us}"
            )
