#!/usr/bin/env python3
"""Cycle the capacitor-bank pins (GP17/18/19) for scope verification.

Usage: python bench_gpio_test.py [port]

Each cycle: first each pin toggled alone (easy to trigger on one channel),
then all 8 combinations in binary counting order. Dwell ~1 s per state.
Stop with Ctrl+C (pins are left in their last state; send C000 to reset).

  C b0 b1 b2  ->  b0 = GP17, b1 = GP18, b2 = GP19 (high = 3.3 V)
"""
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
DWELL = 1.0

# Close any other reader (Arduino monitor etc.) before running this.
s = serial.Serial(PORT, timeout=0.5)
time.sleep(0.3)  # DTR settle
s.reset_input_buffer()


def cmd(c):
    s.write(c.encode() + b"\n")
    time.sleep(0.15)
    r = s.read(64).decode(errors="replace").strip()
    if r != "D":
        sys.exit(f"{c} -> unexpected reply {r!r}, aborting")


# Each pin alone (with return to all-low in between) ...
STATES = ["100", "000", "010", "000", "001", "000"]
# ... then every combination, binary counting order 000 -> 111.
STATES += [format(i, "03b") for i in range(8)]

print(f"cycling {len(STATES)} states, {DWELL:.1f} s each, Ctrl+C to stop")
try:
    while True:
        for state in STATES:
            cmd(f"C{state}")
            b0, b1, b2 = state
            print(f"\rC{state}  GP17={b0}  GP18={b1}  GP19={b2}   ",
                  end="", flush=True)
            time.sleep(DWELL)
except KeyboardInterrupt:
    print("\nstopped; sending C000 (all pins low)")
    cmd("C000")
finally:
    s.close()
