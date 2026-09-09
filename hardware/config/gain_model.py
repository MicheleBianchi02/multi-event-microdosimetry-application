"""
Derivation of the gain tables from a validated HardwareConfig.

For each active channel: take the circuit's switchable capacitors, find the
pin driving each of them (connections), order them by the pin's index in the
device's declared `gain.pins` (the gpio-mask order), and enumerate all 2^n
pin combinations.  Each option carries, per gain device, the *sparse* pin
states (only the pins routed to that channel's circuit), so applying a gain
never depends on the order in which channels are configured, and conflicts
on ganged pins are detectable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from hardware.config.schema import HardwareConfig


@dataclass(frozen=True)
class GainOption:
    """One selectable gain of one channel."""

    label: str
    capacitance_pF: float
    # device_id -> {pin_index (in the device's declared pins order): on/off}
    pin_states: dict

    def mask_for(self, device_id: str, num_pins: int) -> Optional[tuple[bool, ...]]:
        """Dense mask (one bool per declared pin) for one device."""
        states = self.pin_states.get(device_id)
        if states is None:
            return None
        return tuple(states.get(i, False) for i in range(num_pins))


@dataclass
class ChannelGainModel:
    channel: str
    circuit: str
    options: list[GainOption] = field(default_factory=list)
    # device_id -> [(pin_id, capacitor_id, value_pF)] in gpio-mask order
    routing: dict = field(default_factory=dict)

    def devices(self) -> set:
        return set(self.routing)

    def option_by_label(self, label: str) -> Optional[GainOption]:
        for option in self.options:
            if option.label == label:
                return option
        return None


def channel_routing(config: HardwareConfig, channel: str) -> dict:
    """Resolve the pin -> capacitor routing of one channel, in gpio-mask order.

    Returns {device_id: [(pin_id, capacitor_id, value_pF), ...]} with each
    list ordered like the device's declared pins.
    """
    channel_cfg = config.channels[channel]
    circuit = config.circuits[channel_cfg.circuit]
    gain_dev_id = config.roles.gain_ch1 if channel == "ch1" else config.roles.gain_ch2
    gain_dev = config.devices[gain_dev_id]

    capacitors = {c.id: c for c in circuit.switchable_capacitors}
    edges: dict = {pin: None for pin in gain_dev.gain.pins}
    for conn in config.connections:  # validated: exactly one edge per capacitor
        if conn.circuit == circuit.id and conn.capacitor in capacitors:
            edges[conn.pin] = capacitors[conn.capacitor]

    routing = {gain_dev_id: []}
    for pin in gain_dev.gain.pins:  # gpio-mask order
        capacitor = edges.get(pin)
        if capacitor is not None:
            routing[gain_dev_id].append((pin, capacitor.id, capacitor.value_pF))
    return routing


def build_channel_gain_model(config: HardwareConfig, channel: str) -> Optional[ChannelGainModel]:
    """Build the sorted, labeled gain table of one channel."""
    if channel not in config.channels:
        return None

    channel_cfg = config.channels[channel]
    routing = channel_routing(config, channel)
    gain_dev_id = config.roles.gain_ch1 if channel == "ch1" else config.roles.gain_ch2
    gain_dev = config.devices[gain_dev_id]
    circuit = config.circuits[channel_cfg.circuit]

    switched = routing[gain_dev_id]
    pin_index = {pin: i for i, pin in enumerate(gain_dev.gain.pins)}
    totals = []
    for i in range(1 << len(switched)):
        mask = [(i >> bit) & 1 == 1 for bit in range(len(switched))]
        c_total = circuit.fixed_capacitance_pF + sum(
            value for on, (_, _, value) in zip(mask, switched) if on
        )
        if c_total > 0:
            totals.append((c_total, mask))

    totals.sort(key=lambda item: item[0], reverse=True)
    c_max = totals[0][0] if totals else 0.0
    combinations = []
    for c_total, mask in totals:
        gain_val = c_max / c_total
        label = "1x" if gain_val == 1.0 else f"{gain_val:g}x"
        sparse = {gain_dev_id: {pin_index[pin]: on for (pin, _, _), on in zip(switched, mask)}}
        combinations.append(GainOption(label=label, capacitance_pF=c_total, pin_states=sparse))

    return ChannelGainModel(channel=channel, circuit=circuit.id, options=combinations, routing=routing)


def gain_devices(config: HardwareConfig) -> dict:
    """Gain device ids per active channel: {"ch1": {dev}, "ch2": {dev}}."""
    result = {}
    for channel in config.channels:
        model = build_channel_gain_model(config, channel)
        if model:
            result[channel] = model.devices()
    return result


def channels_share_gain_device(config: HardwareConfig) -> bool:
    """True if ch1 and ch2 gain selections are physically coupled.

    Happens when the same device drives capacitors of both circuits (its
    pins are shared): one pin cannot be high for one channel and low for
    the other, so the two channels' masks must agree on the shared pins.
    """
    devices = gain_devices(config)
    ch1, ch2 = devices.get("ch1", set()), devices.get("ch2", set())
    return bool(ch1 & ch2)


def combined_pin_states(options: list) -> Optional[dict]:
    """Merge GainOptions of several channels into one sparse pin state per device.

    Returns None on conflict: two options requesting different states on the
    same pin of the same device (ganged pins with disagreeing channel gains
    — physically impossible).  Otherwise {device_id: {pin_index: bool}}.
    """
    merged: dict = {}
    for option in options:
        for device_id, states in option.pin_states.items():
            current = merged.setdefault(device_id, {})
            for pin_index, on in states.items():
                if pin_index in current and current[pin_index] != on:
                    return None  # conflict on a shared pin
                current[pin_index] = on
    return merged
