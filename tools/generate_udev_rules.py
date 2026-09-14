#!/usr/bin/env python3
"""
Generate udev rules for the serial devices of a hardware configuration.

The config carries no port: the persistent name of each device is derived from
its id, /dev/atomiqa/<device_id>. This tool reads the configuration, looks at
the serial boards currently plugged in, and emits one udev rule per device —
matched by USB serial number (unique per board) with VID/PID fallback — that
creates exactly that symlink. Run it once when a setup is first cabled, after
editing the device ids, or after replacing a board.

Usage:
    python3 tools/generate_udev_rules.py --config <hardware_config.json>      # print rules
    python3 tools/generate_udev_rules.py --config <...> --write               # install (root)
    sudo ./setup_udev_rules.sh <hardware_config.json>                         # same, via wrapper

The mapping is interactive: for each device the currently plugged candidates
are listed and you pick the right board (unplug/plug or check the USB serial
to be sure). Non-interactive: pass --map DEVICE_ID=SERIAL repeatedly.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

RULES_FILE = "/etc/udev/rules.d/99-atomiqa.rules"


def configured_serial_devices(config: dict) -> list:
    """[(device_id, symlink_name)] for every serial device."""
    result = []
    for dev_id, dev in config.get("devices", {}).items():
        connection = dev.get("connection", {})
        if connection.get("type") == "serial":
            result.append((dev_id, f"atomiqa/{dev_id}"))
    return result


def read_attr(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def usb_serial_devices() -> list:
    """Currently plugged USB-serial devices: (tty, vendor, product, serial)."""
    devices = []
    for tty_dir in sorted(glob.glob("/sys/class/tty/*")):
        device_link = os.path.join(tty_dir, "device")
        if not os.path.islink(device_link):
            continue
        real = os.path.realpath(device_link)
        if "/usb" not in real:
            continue

        vendor = product = serial = None
        level = real
        for _ in range(6):  # walk up the USB tree
            vendor = vendor or read_attr(os.path.join(level, "idVendor"))
            product = product or read_attr(os.path.join(level, "idProduct"))
            serial = serial or read_attr(os.path.join(level, "serial"))
            parent = os.path.dirname(level)
            if parent == level:
                break
            level = parent

        if vendor:
            devices.append(
                {
                    "tty": "/dev/" + os.path.basename(tty_dir),
                    "vendor": vendor,
                    "product": product,
                    "serial": serial,
                }
            )
    return devices


def build_rule(symlink: str, candidate: dict) -> str:
    parts = ['SUBSYSTEM=="tty"', f'ATTRS{{idVendor}}=="{candidate["vendor"]}"']
    if candidate["product"]:
        parts.append(f'ATTRS{{idProduct}}=="{candidate["product"]}"')
    if candidate["serial"]:
        parts.append(f'ATTRS{{serial}}=="{candidate["serial"]}"')
    parts.append(f'SYMLINK+="{symlink}"')
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="hardware_config.json of the setup")
    parser.add_argument("--map", action="append", default=[], metavar="DEVICE_ID=SERIAL",
                        help="non-interactive mapping, e.g. --map pico_1=E6614C311B8B4D34")
    parser.add_argument("--write", action="store_true", help="write the rules file and reload udev (needs root)")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = json.load(f)

    wanted = configured_serial_devices(config)
    if not wanted:
        print("No serial devices in this configuration.")
        return 1

    plugged = usb_serial_devices()
    if not plugged:
        print("No USB-serial devices found: plug the boards in before running this tool.")
        return 1

    print("Plugged USB-serial devices:")
    for i, c in enumerate(plugged):
        print(f"  [{i}] {c['tty']}  VID:PID {c['vendor']}:{c['product'] or '????'}  serial {c['serial'] or '(none)'}")
    print()

    explicit = dict(m.split("=", 1) for m in args.map)
    rules = []
    used = set()
    for dev_id, symlink in wanted:
        if symlink in explicit:
            serial = explicit[symlink]
            candidates = [c for c in plugged if c["serial"] == serial]
            if not candidates:
                print(f"error: no plugged device with serial {serial!r} for {symlink}")
                return 1
            candidate = candidates[0]
        else:
            available = [c for c in plugged if c["tty"] not in used]
            if not available:
                print(f"error: no unassigned plugged device left for {symlink}")
                return 1
            if len(available) == 1:
                candidate = available[0]
            else:
                print(f"Which plugged device is '{dev_id}' (persistent /dev/{symlink})?")
                for i, c in enumerate(available):
                    print(f"  [{i}] {c['tty']}  serial {c['serial'] or '(none)'}")
                while True:
                    choice = input("number: ").strip()
                    if choice.isdigit() and int(choice) < len(available):
                        candidate = available[int(choice)]
                        break
        used.add(candidate["tty"])
        rule = build_rule(symlink, candidate)
        if not candidate["serial"]:
            print(f"warning: {candidate['tty']} has no USB serial; the rule for {dev_id} "
                  f"matches VID/PID only and is ambiguous between identical boards")
        rules.append(rule)
        print(f"{dev_id}: /dev/{symlink} <- {candidate['tty']}")

    print()
    print("# " + RULES_FILE)
    for rule in rules:
        print(rule)

    if args.write:
        if os.geteuid() != 0:
            print("\nerror: --write needs root (run via sudo ./setup_udev_rules.sh)")
            return 1
        with open(RULES_FILE, "w") as f:
            f.write("\n".join(rules) + "\n")
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
        subprocess.run(["udevadm", "trigger"], check=True)
        print(f"\nWritten to {RULES_FILE}; rules reloaded. Unplug/replug the boards "
              f"(or reboot) and the symlinks appear.")
    else:
        print("\nDry run: re-run with --write (or sudo ./setup_udev_rules.sh <config>) to install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
