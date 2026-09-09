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
from dataclasses import dataclass

from hardware.config.schema import DriverDescriptor, TimingCapability

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredDriver:
    driver_class: type
    descriptor: DriverDescriptor

    def create(self, connection: dict):
        """Instantiate the driver from its transport connection block."""
        connection_type = connection.get("type")
        if connection_type == "serial":
            return self.driver_class(
                port=connection.get("port"),
                baud_rate=connection.get("baud_rate", 115200),
            )
        # Other transports (e.g. daqmx) are added with their drivers.
        raise ValueError(f"unsupported connection type {connection_type!r}")


def _descriptor_from_class(cls) -> DriverDescriptor:
    return DriverDescriptor(
        capabilities=frozenset(cls.DRIVER_CAPABILITIES),
        timing=cls.HARDWARE_LIMITS,
        gain_max_pins=cls.GAIN_MAX_PINS,
        adc_bits=cls.ADC_BITS,
        adc_native_range=getattr(cls, "ADC_NATIVE_RANGE", None),
    )


def _register(cls, code: str) -> RegisteredDriver:
    return RegisteredDriver(driver_class=cls, descriptor=_descriptor_from_class(cls))


def build_registry() -> dict[str, RegisteredDriver]:
    from hardware.pico.pico_daq import PicoSerialDAQ
    from hardware.pic.pic_daq import PICSerialDAQ

    return {
        "pico_serial_v1": _register(PicoSerialDAQ, "pico_serial_v1"),
        "pic_serial_v1": _register(PICSerialDAQ, "pic_serial_v1"),
    }


DRIVER_REGISTRY: dict[str, RegisteredDriver] = build_registry()


def registry_descriptors() -> dict[str, DriverDescriptor]:
    """Only the descriptors, for the config loader's cross-check."""
    return {code: rd.descriptor for code, rd in DRIVER_REGISTRY.items()}
