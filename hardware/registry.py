"""
Driver registry: the code-side half of the handshake.

Every device in hardware_config.json carries a stable driver code; this
module maps each code to its implementation class and to the *hardcoded*
layer-1 limits the driver enforces at every call (the config's declared
envelope is validated against them at load time, see hardware/config).

Codes are stable identifiers: never rename one — if the firmware protocol
breaks compatibility, register a new code (e.g. "..._v2") and migrate old
config files via their schema_version.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from hardware.config.schema import DeviceConfig, DriverDescriptor, TimingCapability

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredDriver:
    driver_class: type
    descriptor: DriverDescriptor
    factory: Callable[[dict, DeviceConfig], Any] = field(kw_only=True)

    def create(self, connection: dict, device_config: DeviceConfig) -> Any:
        return self.factory(connection, device_config)


def _serial_factory(cls) -> Callable[[dict, DeviceConfig], Any]:
    def create(connection: dict, device_config: DeviceConfig) -> Any:
        if connection.get("type") != "serial":
            raise ValueError(
                f"driver {cls.__name__} requires a 'serial' connection, "
                f"got {connection.get('type')!r}"
            )
        # No port here: the persistent name is derived from the device id by
        # the HardwareManager (the config carries no port).
        return cls(baud_rate=connection.get("baud_rate", 115200))

    return create


def _register(cls, code: str, factory: Callable[[dict, DeviceConfig], Any] = None) -> RegisteredDriver:
    descriptor = DriverDescriptor(
        capabilities=frozenset(cls.DRIVER_CAPABILITIES),
        timing=getattr(cls, "HARDWARE_LIMITS", None),
        gain_max_pins=getattr(cls, "GAIN_MAX_PINS", None),
        adc_bits=getattr(cls, "ADC_BITS", None),
        adc_native_range=getattr(cls, "ADC_NATIVE_RANGE", None),
    )
    return RegisteredDriver(
        driver_class=cls,
        descriptor=descriptor,
        factory=factory or _serial_factory(cls),
    )


def build_registry() -> dict[str, RegisteredDriver]:
    from hardware.pico.pico_daq import PicoSerialDAQ
    from hardware.pic.pic_daq import PICSerialDAQ
    from hardware.dummy.dummy_acquisition import DummyAcquisition

    registry = {
        "pico_serial_v1": _register(PicoSerialDAQ, "pico_serial_v1"),
        "pic_serial_v1": _register(PICSerialDAQ, "pic_serial_v1"),
    }
    registry["dummy_v1"] = _register(
        DummyAcquisition,
        "dummy_v1",
        factory=lambda connection, device_config: DummyAcquisition(
            device_config.acquisition if device_config else None
        ),
    )
    return registry


DRIVER_REGISTRY: dict[str, RegisteredDriver] = build_registry()


def registry_descriptors() -> dict[str, DriverDescriptor]:
    """Only the descriptors, for the config loader's cross-check."""
    return {code: rd.descriptor for code, rd in DRIVER_REGISTRY.items()}
