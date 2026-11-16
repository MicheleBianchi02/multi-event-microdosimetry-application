"""
Pure-logic model behind the hardware config editor.

All editing happens at the JSON-dict level (unknown fields survive a
round-trip) and every mutation keeps the cross-references consistent:
removing a device clears its roles and connections, removing a capacitor
drops its wiring edge, ids are uniquified when inserting fragments.

Validation reuses the production loader, so the editor cannot produce a
file the app would reject.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Optional

from hardware.config.loader import ConfigValidationError, SUPPORTED_SCHEMA_VERSION
from hardware.config.schema import DriverDescriptor

VALID_ROLES = ("timing", "acquisition", "gain_ch1", "gain_ch2")
ROLE_CAPABILITY = {
    "timing": "timing",
    "acquisition": "acquisition",
    "gain_ch1": "gain",
    "gain_ch2": "gain",
}


def new_config() -> dict:
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "devices": {},
        "circuits": {},
        "channels": {},
        "roles": {},
        "connections": [],
    }


def clone_config(config: dict) -> dict:
    return copy.deepcopy(config)


# ---------------------------------------------------------------------------
# Fragments (component library)
# ---------------------------------------------------------------------------

def load_fragment(path: str) -> dict:
    """Load one library fragment: {"kind", "suggested_id", "data"}."""
    with open(path, "r", encoding="utf-8") as f:
        fragment = json.load(f)
    validate_fragment(fragment, where=os.path.basename(path))
    return fragment


def resolve_fragment(entry: dict) -> dict:
    """Accept either a listing entry ({path, kind, suggested_id, label}) or a
    full fragment ({kind, suggested_id, data}) and return a full fragment."""
    if isinstance(entry.get("data"), dict):
        return entry
    path = entry.get("path")
    if not path:
        raise ValueError("fragment entry has neither 'data' nor 'path'")
    return load_fragment(path)


def validate_fragment(fragment: Any, where: str = "fragment") -> None:
    if not isinstance(fragment, dict):
        raise ValueError(f"{where}: must be a JSON object")
    kind = fragment.get("kind")
    if kind not in ("device", "circuit"):
        raise ValueError(f"{where}: 'kind' must be 'device' or 'circuit'")
    data = fragment.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{where}: missing 'data' object")
    if kind == "device":
        if not isinstance(data.get("driver"), str) or not data["driver"]:
            raise ValueError(f"{where}: device fragment needs a 'driver' code")
    else:
        if "fixed_capacitance_pF" not in data and not data.get("switchable_capacitors"):
            raise ValueError(f"{where}: circuit fragment has no capacitance")


def list_fragments(directories: list[str]) -> tuple[list[dict], list[str]]:
    """Collect fragments from the given folders (shipped library first).

    Returns (fragments, problems): each fragment is
    {"path", "kind", "suggested_id", "label"}; unparsable files are skipped
    and reported in problems.
    """
    fragments, problems = [], []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                fragment = load_fragment(path)
            except (ValueError, OSError, json.JSONDecodeError) as e:
                problems.append(str(e))
                continue
            fragments.append(
                {
                    "path": path,
                    "kind": fragment["kind"],
                    "suggested_id": fragment.get("suggested_id", "component"),
                    "label": fragment.get("label")
                    or fragment["data"].get("name")
                    or fragment.get("suggested_id", name),
                }
            )
    return fragments, problems


def fragment_to_json(fragment: dict, kind: str, suggested_id: str) -> dict:
    return {"kind": kind, "suggested_id": suggested_id, "data": fragment}


# ---------------------------------------------------------------------------
# Uniqueness helpers
# ---------------------------------------------------------------------------

def _unique_id(base: str, existing: dict) -> str:
    candidate, n = base, 2
    while candidate in existing:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def add_device(config: dict, data: dict, suggested_id: str = "device") -> str:
    device_id = _unique_id(suggested_id, config["devices"])
    entry = copy.deepcopy(data)
    entry.setdefault("name", device_id)
    config["devices"][device_id] = entry
    return device_id


def add_circuit(config: dict, data: dict, suggested_id: str = "circuit") -> str:
    circuit_id = _unique_id(suggested_id, config["circuits"])
    entry = copy.deepcopy(data)
    entry.setdefault("name", circuit_id)
    config["circuits"][circuit_id] = entry
    return circuit_id


def remove_device(config: dict, device_id: str) -> list[str]:
    """Remove a device and everything that referenced it. Returns notes."""
    notes = []
    config["devices"].pop(device_id, None)
    for role in VALID_ROLES:
        if config.get("roles", {}).get(role) == device_id:
            config["roles"][role] = None
            notes.append(f"role {role} was released (device {device_id} removed)")
    config["connections"] = [
        c for c in config.get("connections", []) if c.get("device") != device_id
    ]
    return notes


def remove_circuit(config: dict, circuit_id: str) -> list[str]:
    notes = []
    config["circuits"].pop(circuit_id, None)
    for ch_id, channel in list(config.get("channels", {}).items()):
        if channel.get("circuit") == circuit_id:
            del config["channels"][ch_id]
            gain_role = "gain_ch1" if ch_id == "ch1" else "gain_ch2"
            if config.get("roles", {}).get(gain_role):
                config["roles"][gain_role] = None
            notes.append(f"channel {ch_id} was removed with its circuit")
    config["connections"] = [
        c for c in config.get("connections", []) if c.get("circuit") != circuit_id
    ]
    return notes


def set_role(config: dict, role: str, device_id: Optional[str]) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role {role!r}")
    config.setdefault("roles", {})[role] = device_id
    if role in ("gain_ch1", "gain_ch2"):
        # wiring edges of the channel's circuit that the new gain device
        # cannot drive are stale: drop them instead of reporting them
        _prune_channel_wiring(config, "ch1" if role == "gain_ch1" else "ch2")


def _prune_channel_wiring(config: dict, channel: str) -> None:
    """Drop wiring edges of the channel's circuit that the current gain
    device cannot drive (edges of another device, or of undeclared pins)."""
    gain_dev_id = config.get("roles", {}).get("gain_ch1" if channel == "ch1" else "gain_ch2")
    circuit_id = config.get("channels", {}).get(channel, {}).get("circuit")
    if not circuit_id:
        return
    pins = set()
    if gain_dev_id:
        dev = config.get("devices", {}).get(gain_dev_id, {})
        pins = set(dev.get("capabilities", {}).get("gain", {}).get("pins", []))
    config["connections"][:] = [
        c
        for c in config.get("connections", [])
        if not (
            c.get("circuit") == circuit_id
            and (c.get("device") != gain_dev_id or c.get("pin") not in pins)
        )
    ]


def set_channel(
    config: dict,
    channel: str,
    circuit_id: Optional[str],
    adc_input: Optional[str] = None,
    termination: Optional[str] = None,
) -> None:
    """Declare a channel.  circuit_id None removes the channel (and clears its
    gain role); the empty string declares it as *not yet assigned* — the
    validator then reports the missing circuit until one is chosen."""
    channels = config.setdefault("channels", {})
    if circuit_id is None:
        if channel in channels:
            del channels[channel]
            gain_role = "gain_ch1" if channel == "ch1" else "gain_ch2"
            config.setdefault("roles", {})[gain_role] = None
        return
    old_circuit = channels.get(channel, {}).get("circuit")
    entry = channels.setdefault(channel, {})
    entry["circuit"] = circuit_id
    if adc_input:
        entry["adc_input"] = adc_input
    else:
        entry.pop("adc_input", None)
    if termination:
        entry["termination"] = termination
    else:
        entry.pop("termination", None)
    if old_circuit and old_circuit != circuit_id:
        # the channel moved to another circuit: its old wiring is stale
        config["connections"][:] = [
            c for c in config.get("connections", []) if c.get("circuit") != old_circuit
        ]


def rename_capacitor(config: dict, circuit_id: str, old_id: str, new_id: str) -> None:
    circuit = config["circuits"][circuit_id]
    for capacitor in circuit.get("switchable_capacitors", []):
        if capacitor.get("id") == old_id:
            capacitor["id"] = new_id
    for connection in config.get("connections", []):
        if connection.get("circuit") == circuit_id and connection.get("capacitor") == old_id:
            connection["capacitor"] = new_id


def remove_capacitor(config: dict, circuit_id: str, capacitor_id: str) -> None:
    circuit = config["circuits"][circuit_id]
    circuit["switchable_capacitors"] = [
        c for c in circuit.get("switchable_capacitors", []) if c.get("id") != capacitor_id
    ]
    config["connections"] = [
        c
        for c in config.get("connections", [])
        if not (c.get("circuit") == circuit_id and c.get("capacitor") == capacitor_id)
    ]


def set_connection(config: dict, channel: str, capacitor_id: str, pin: Any) -> None:
    """Wire `pin` (or None = unwired) of the channel's gain device to one
    capacitor of the channel's circuit, replacing any previous edge."""
    gain_device = config.get("roles", {}).get("gain_ch1" if channel == "ch1" else "gain_ch2")
    circuit_id = config.get("channels", {}).get(channel, {}).get("circuit")
    if not gain_device or not circuit_id:
        raise ValueError(f"channel {channel} has no gain device or circuit assigned")
    connections = config.setdefault("connections", [])
    connections[:] = [
        c
        for c in connections
        if not (c.get("circuit") == circuit_id and c.get("capacitor") == capacitor_id)
    ]
    if pin is not None:
        connections.append({"device": gain_device, "pin": pin, "circuit": circuit_id, "capacitor": capacitor_id})


def connection_of(config: dict, channel: str, capacitor_id: str) -> Optional[Any]:
    circuit_id = config.get("channels", {}).get(channel, {}).get("circuit")
    for connection in config.get("connections", []):
        if connection.get("circuit") == circuit_id and connection.get("capacitor") == capacitor_id:
            return connection.get("pin")
    return None


# ---------------------------------------------------------------------------
# Validation (the production loader is the single source of truth)
# ---------------------------------------------------------------------------

def validate(config: dict, registry: Optional[dict[str, DriverDescriptor]] = None) -> tuple[list[str], list[str]]:
    """Validate a config dict. Returns (errors, warnings), both empty when valid."""
    try:
        from hardware.config.loader import hardware_config_from_dict

        validated = hardware_config_from_dict(copy.deepcopy(config), registry)
        return [], list(validated.warnings)
    except ConfigValidationError as e:
        return list(e.errors), []
