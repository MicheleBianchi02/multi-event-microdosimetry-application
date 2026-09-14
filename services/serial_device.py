"""
Serial port enumeration for the manual-connect UI.

The old SerialDevice protocol class that lived here was dead code (the
hardware/ drivers own the protocol since the hardware-abstraction rework)
and was removed with the connection-handling rework. The concrete driver
protocols are being reworked and will be documented separately.
"""
import serial.tools.list_ports


def get_port() -> list[dict[str, str]]:
    serial_ports = serial.tools.list_ports.comports()
    port_list = []
    for port in serial_ports:
        port_list.append(
            {
                "name": port.name,
                "description": port.description,
                "manufacturer": port.manufacturer,
                "device": port.device,
            }
        )
    return port_list
