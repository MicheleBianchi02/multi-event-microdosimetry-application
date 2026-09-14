# udev rules — how to create them (admin how-to)

The app opens every serial device at a persistent name derived from its
hardware-config id: **`/dev/atomiqa/<device_id>`** (e.g. the device `pico_1`
of `hardware_config.json` is always opened at `/dev/atomiqa/pico_1`). On
Linux these names are created by **udev rules**: one rule per physical board,
mapping the hardware (matched by its unique USB serial number) to that name.
The hardware config itself carries no port and nothing machine-specific —
this mapping is the only piece that lives on the machine, in this rules file.

This file explains how to create the rules by hand; the equivalent automatic
way is `sudo ./setup_udev_rules.sh <workspace>/hardware_config.json`
(interactive) or `tools/generate_udev_rules.py --config <...> --map
DEVICE_ID=SERIAL` (non-interactive). Both write exactly the same lines.

---

## 1. Plug in one board and read its identity attributes

```bash
ls /dev/ttyACM*                 # note the tty the board got, e.g. /dev/ttyACM0
udevadm info -a -n /dev/ttyACM0 | grep -E 'idVendor|idProduct|serial' | head -6
```

Three values are needed, all from the USB parent device:

| Attribute | Example (Pico) | Meaning |
|---|---|---|
| `ATTRS{idVendor}` | `2e8a` | USB vendor id (Raspberry Pi) |
| `ATTRS{idProduct}` | `0009` | USB product id (board/firmware family) |
| `ATTRS{serial}` | `E6614C31A7B3` | **unique per board** |

Do not trust the examples — read the real values. If the `serial` attribute
is missing or empty on a board (some clones), see §6.

## 2. Write one rule line per device

Edit `/etc/udev/rules.d/99-atomiqa.rules` (create the file; one file for the
whole bench is fine):

```
# pico_1 — timing + gain + acquisition
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0009", ATTRS{serial}=="E6614C31A7B3", SYMLINK+="atomiqa/pico_1"

# pic_g2 — gain CH2 (second board, its own serial number)
SUBSYSTEM=="tty", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="000a", ATTRS{serial}=="00012345", SYMLINK+="atomiqa/pic_g2"
```

The three constraints that matter:

- **Match by `ATTRS{serial}`.** It is unique per board: two identical Picos
  stay distinguishable. VID/PID alone would match both boards at once.
- **`SYMLINK` = `atomiqa/<device_id>` exactly** — the device id as it appears
  in `hardware_config.json` (the connection card and the config editor both
  show it). A typo means auto-connect opens a name that does not exist.
- The rule never mentions `ttyACMx`: udev matches the hardware itself, so
  the plug order and USB port do not matter — now or after recabling.

With several boards of the same model, do this one board at a time: plug
board A, read its attributes (§1), write its line; unplug, plug board B,
repeat.

## 3. Apply and verify

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger            # or simply unplug/replug the boards
ls -l /dev/atomiqa/             # expect: pico_1 -> ../ttyACM0, pic_g2 -> ../ttyACM1
```

## 4. Permissions (once per machine)

The symlink only helps if the user may open the device:

```bash
sudo usermod -aG dialout $USER  # then log out and back in
```

Alternatively add `MODE:="0660", GROUP:="dialout"` to each rule line instead
of using the group.

## 5. Why the `99-` prefix?

udev collects rule files from `/usr/lib/udev/rules.d` (distribution),
`/run/udev/rules.d` and `/etc/udev/rules.d` (local administrator) and parses
them **in lexicographic filename order** — the numeric prefix is that order.
Within an event, `SYMLINK+=` assignments are additive at any position, but
final assignments (e.g. `NAME`, `MODE`, `OWNER`) are *last match wins*. A
high number therefore means: evaluated **after** every rule the distribution
ships, so nothing system-provided can run afterwards and override what these
rules decided — and these rules see the device with all earlier rules already
applied. `99-` is simply the conventional "local customization, runs last"
slot; `50-` or `70-` would usually work for a pure symlink too, but `99-` is
the safe, self-documenting choice.

## 6. Troubleshooting

- **Symlink does not appear.**
  `udevadm test $(udevadm info -q path -n /dev/ttyACM0) 2>&1 | less` shows
  every rule udev evaluated against the device and why each did or did not
  match — usually a typo in the serial or vendor string.
- **Board has no USB serial.** Fall back to VID/PID plus the physical USB
  port path: add `KERNELS=="1-2.3"` (the exact value is visible in
  `udevadm info -a` output). Note this breaks when the board is moved to
  another USB socket — matching by serial is much sturdier.
- **Renamed a device id in the config?** Update the rule's `SYMLINK` to
  match — that is the only coupling between the two files.
- **Old rules from previous app versions** (a single `pico_daq` symlink, or
  a `99-daq-devices.rules` file) can be deleted once the new file is in
  place.
