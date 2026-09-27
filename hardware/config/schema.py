"""
Typed objects describing a hardware configuration file (hardware_config.json).

The file describes the *physical instantiation* of the measurement chain:
which devices exist (uC / instruments), the bounds each one accepts (the
actual behaviour lives in the concrete drivers), the electronic circuits,
the wiring between device pins and circuit capacitors, and the role
assignment.  Timing keys carry their unit in the name; all times are
microseconds, all capacitances picofarad, all voltages volts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

PinId = Union[int, str]
TimingParameter = Union["ParameterBounds", "FixedValue", None]


@dataclass(frozen=True)
class ParameterBounds:
    """A settable parameter: any value in [min, max], with a suggested UI step."""

    min: float
    max: float
    step: float

    def contains_bounds(self, other: "ParameterBounds") -> bool:
        """True if `other` lies entirely within self (used for the driver cross-check)."""
        return self.min <= other.min and other.max <= self.max

    def contains_value(self, value: float) -> bool:
        return self.min <= value <= self.max

    def __str__(self) -> str:
        return f"[{self.min:g}, {self.max:g}] step {self.step:g}"


@dataclass(frozen=True)
class FixedValue:
    """A parameter fixed by the firmware: displayed, not settable."""

    value: float

    def __str__(self) -> str:
        return f"fixed {self.value:g}"


@dataclass(frozen=True)
class RampSamplingConfig:
    supported: bool
    dt_us: Optional[ParameterBounds] = None


@dataclass(frozen=True)
class TimingCapability:
    """Declared timing envelope of one device.

    integration_time_us / reset_time_us: ParameterBounds or FixedValue.
    period_us: additionally None = derived by the firmware (not
    independently configurable).
    """

    integration_time_us: TimingParameter
    reset_time_us: TimingParameter
    period_us: TimingParameter
    ramp_sampling: RampSamplingConfig

    def parameter(self, name: str) -> TimingParameter:
        return getattr(self, name)


@dataclass(frozen=True)
class GainCapability:
    """GPIO pins exposed by the device, in gpio-mask order (mask[i] -> pins[i])."""

    pins: tuple[PinId, ...]


@dataclass(frozen=True)
class VoltageRange:
    min_volts: float
    max_volts: float

    def contains(self, other: "VoltageRange") -> bool:
        return self.min_volts <= other.min_volts and other.max_volts <= self.max_volts

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VoltageRange):
            return NotImplemented
        return self.min_volts == other.min_volts and self.max_volts == other.max_volts

    def __str__(self) -> str:
        return f"[{self.min_volts:g}, {self.max_volts:g}] V"


@dataclass(frozen=True)
class AdcCapability:
    bits: int
    input_ranges_volts: tuple[VoltageRange, ...]
    default_input_range_volts: Optional[VoltageRange] = None
    supports_oversampling: bool = False

    def effective_range(self) -> VoltageRange:
        """The range applied when the config is loaded (default, or the only one)."""
        if self.default_input_range_volts is not None:
            return self.default_input_range_volts
        return self.input_ranges_volts[0]


@dataclass(frozen=True)
class AcquisitionCapability:
    """Maximum number of channels measurable simultaneously + ADC properties.

    num_channels is the binding constraint for the measurement: the active
    channels must not exceed it (a device may e.g. time two channels but
    acquire only one).
    """

    num_channels: int
    adc: AdcCapability


@dataclass
class DeviceConfig:
    id: str
    name: str
    driver: str
    connection: dict  # transport-specific, must contain "type"
    firmware_version: Optional[str] = None
    note: Optional[str] = None
    timing: Optional[TimingCapability] = None
    gain: Optional[GainCapability] = None
    acquisition: Optional[AcquisitionCapability] = None


@dataclass
class CapacitorConfig:
    id: str
    value_pF: float
    note: Optional[str] = None


@dataclass
class CircuitConfig:
    id: str
    name: str
    fixed_capacitance_pF: float = 0.0
    switchable_capacitors: tuple[CapacitorConfig, ...] = ()
    model: Optional[str] = None
    note: Optional[str] = None


@dataclass
class ChannelConfig:
    id: str  # "ch1" or "ch2"
    circuit: str
    adc_input: Optional[str] = None
    # How the channel's signal reaches the acquisition device:
    # "differential" or "rse" (referenced single-ended).  None = the
    # acquisition driver's default.  The concrete input terminals are
    # hardware facts hardcoded in the driver (e.g. the NI USB-6451 maps
    # ch1/ch2 x differential/rse to fixed AI pins).
    termination: Optional[str] = None
    note: Optional[str] = None


@dataclass
class RolesConfig:
    timing: Optional[str] = None
    acquisition: Optional[str] = None
    gain_ch1: Optional[str] = None
    gain_ch2: Optional[str] = None


@dataclass
class ConnectionConfig:
    """One wiring edge: a device pin driving one capacitor of one circuit.

    A pin may appear in several connections (e.g. the same pin routed to a
    capacitor on each of the two circuits); a capacitor may have at most one.
    """

    device: str
    pin: PinId
    circuit: str
    capacitor: str
    note: Optional[str] = None


@dataclass
class HardwareConfig:
    schema_version: str
    devices: dict[str, DeviceConfig]
    circuits: dict[str, CircuitConfig]
    channels: dict[str, ChannelConfig]
    roles: RolesConfig
    connections: list[ConnectionConfig]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DriverDescriptor:
    """What a registered driver code can do — the code-side half of the handshake.

    `timing` (and the gain/ADC limits) are the *hardcoded* layer-1 limits the
    driver enforces at every call; the config's declared envelope must lie
    within them (validated at load time).

    Acquisition-side limits: `acq_max_channels` bounds the declared
    `num_channels`, `acq_max_rate_hz` the per-channel stream rate (the ramp
    dt must satisfy 1/dt <= rate), and `channel_terminations` the channel
    termination values the driver can actually wire.
    """

    capabilities: frozenset  # subset of {"timing", "gain", "acquisition"}
    timing: Optional[TimingCapability] = None
    gain_max_pins: Optional[int] = None
    adc_bits: Optional[int] = None
    adc_native_range: Optional[VoltageRange] = None
    acq_max_channels: Optional[int] = None
    acq_max_rate_hz: Optional[float] = None
    channel_terminations: Optional[tuple] = None

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities
