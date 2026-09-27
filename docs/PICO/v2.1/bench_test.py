#!/usr/bin/env python3
"""Standalone bench test for the PICO2DAQ 2.0 firmware — no app needed.

Runs a short no-ramp acquisition and prints basic sample statistics, so the
ADC data path can be verified by hand (e.g. GP26 tied to GND -> all samples
0; tied to the 3V3 rail -> all samples ~4095; floating -> noise).

Usage: python bench_test.py [port] [duration_s]
"""
import struct
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
MARKER = 0xFFFFFFFF

# Close any other reader (Arduino monitor etc.) before running this.
s = serial.Serial(PORT, timeout=0.2)
time.sleep(0.3)  # DTR settle
s.reset_input_buffer()


def cmd(text):
    s.write(text.encode() + b"\n")
    time.sleep(0.2)
    return s.read(256).decode(errors="replace").strip()


print(f"V -> {cmd('V')}")
print(f"Q -> {cmd('Q')}")

# Make sure we are in no-ramp mode: a leftover 'XR' from manual testing
# would start the PIO trigger generator and stream no data at all.
print(f"XN -> {cmd('XN')}")

print(f"T 1000,1000 -> {cmd('T 1000,1000')}")  # 500 Hz, easy to verify

# 'S' acks with exactly one byte: read only that byte, so the binary stream
# that follows stays word-aligned (a generic read can swallow half a word).
s.write(b"S\n")
print(f"S -> {s.read(1).decode(errors='replace')}")
t0 = time.time()

buf = bytearray()
words = []
marker_seen = False


def parse_available():
    """Consume complete 4-byte words; stop at the end-of-stream marker."""
    global marker_seen
    while len(buf) >= 4 and not marker_seen:
        (w,) = struct.unpack_from("<I", buf)
        del buf[:4]
        if w == MARKER:
            marker_seen = True
        else:
            words.append(w)


while time.time() - t0 < DURATION and not marker_seen:
    buf += s.read(4096)
    parse_available()

t_stream = time.time() - t0  # streaming window, measured before the drain sleep
s.write(b"E\n")
time.sleep(1.0)  # drain: remaining data, marker, then the 'D' ack
buf += s.read(8192)
parse_available()
if words:
    starts = [w >> 16 for w in words]
    stops = [w & 0xFFFF for w in words]
    diffs = [b - a for a, b in zip(starts, stops)]
    print(f"\nsamples: {len(words)} in ~{t_stream:.2f} s "
          f"(~{len(words) / t_stream:.0f} Hz, expect ~500 Hz)")
    print(f"start: min={min(starts)} max={max(starts)} "
          f"mean={sum(starts) / len(starts):.1f}")
    print(f"stop : min={min(stops)} max={max(stops)} "
          f"mean={sum(stops) / len(stops):.1f}")
    print(f"diff : mean={sum(diffs) / len(diffs):+.2f}  "
          f"(~0 for a DC/floating input)")
    print(f"first words: {[hex(w) for w in words[:4]]}")
else:
    print("\nno data words received!")

print("end-of-stream marker:", "OK" if marker_seen else "NOT FOUND")
print(f"Q after stop -> {cmd('Q')}")  # check dropped=0
s.close()
