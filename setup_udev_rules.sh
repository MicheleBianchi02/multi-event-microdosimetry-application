#!/bin/bash
# Generate and install the udev rules for one hardware configuration:
# one rule per configured serial device, matched by USB serial number,
# each creating the symlink /dev/atomiqa/<device_id> (the persistent name
# the app derives from the config — the config itself carries no port).
#
#   sudo ./setup_udev_rules.sh <workspace>/hardware_config.json
#
# The mapping to the plugged boards is interactive; see
# tools/generate_udev_rules.py for the non-interactive --map option.
set -e
CONFIG="${1:?usage: sudo ./setup_udev_rules.sh <hardware_config.json>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/tools/generate_udev_rules.py" --config "$CONFIG" --write
