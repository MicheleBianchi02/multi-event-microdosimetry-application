# ATOMIQA — Pico 2 DAQ firmware (PICO2DAQ 2.1)

Firmware for the Raspberry Pi **Pico 2** (RP2350 / Cortex-M33) that acts as the
timing controller, gain controller and ADC readout of the ATOMIQA
variance–covariance measurement chain.

Core layout: **core 0** runs `main()` — USB CDC, command parsing, binary data
streaming. **Core 1** (launched via `multicore_launch_core1`) runs the timing
engine with interrupts disabled, strictly from SRAM.

## Pin map

GPIO number vs physical pin: "GPxx" is the GPIO the firmware drives, NOT
the pad position on the header. The pads are silkscreen-labeled with their
GP names on the underside of the board — follow those labels (e.g. GP17 is
the pad printed "GP17", the second pad above the GP16 bottom-right corner).
The physical-pin column below is the header position from the datasheet
(pads 1–20 run down the left edge, 21–40 up the right edge, USB at top).

| GPIO | Physical pin | Function |
|---|---|---|
| GP14 | 19 | Integrator reset (0 = reset, 1 = integrate) |
| GP15 | 20 | Ramp-mode external trigger output (PIO) |
| GP16 | 21 | Event mode (0 = ME, 1 = SE) — bottom-right corner pad |
| GP17 | 22 | Capacitor bank bit 0 |
| GP18 | 23 | Capacitor bank bit 1 |
| GP19 | 24 | Capacitor bank bit 2 |
| GP26 | 31 | ADC input (channel 0), 12 bit, single channel |

For bench measurements: 3V3 rail on pad 36, GND on pads 3, 8, 13, 18, 28,
33, 38.

## Serial protocol

USB CDC (baud rate irrelevant). Commands are ASCII, terminated by `\n` or
`\r`; replies are a single byte — `D` = ok, `E` = error / bad parameter,
`?` = unknown command — except `V` and `Q` which reply one line.

| Command | Meaning | Reply |
|---|---|---|
| `V` | Identity probe | `V PICO2DAQ 2.1 <16-hex board id> <clk MHz>\n` |
| `Q` | Status / diagnostics | `Q run=<0\|1> mode=<noramp\|ramp> gen=<pio\|c> int=%.3f reset=%.3f dt=%.3f div=<int>.<frac8> pulses=%lu caps=<b0b1b2> event=<ME\|SE> dropped=%lu\n` — `gen` = active ramp generator; in ramp mode `int`/`reset`/`dt` are the quantized values the generator will actually produce; `div` = PIO clock divider in 16.8 format (fractional part in 1/256 units, zero-padded to 3 digits); `pulses` = CONV_START pulses programmed into the generator (0 in no-ramp mode); `caps` and `event` are read back from the pads, so an externally dragged-down line shows 0 |
| `RS` / `RM` | Event mode pin high (SE) / low (ME) | `D`/`E` |
| `C b0 b1 b2` | Capacitor bits, e.g. `C101` (first char = GP17) | `D`/`E` |
| `XN` / `XR` | Select no-ramp (ADC) / ramp (PIO trigger) mode | `D`/`E` |
| `T <int_us>,<reset_us>` | Integration and reset times in µs (manual path) | `D`/`E` |
| `P <dt_us>` | Ramp pulse spacing in µs (manual path) | `D`/`E` |
| `O <div_i>,<div_f>,<reset_pio>,<pulses>` | Program the ramp generator with **raw** values — the application path: the host computes everything exactly and the firmware programs these numbers verbatim. Bounds: `1 ≤ div_i ≤ 65535`, `0 ≤ div_f ≤ 255`, `reset_pio ≥ 1`, `pulses ≥ 1` | `D`/`E` |
| `GP` / `GC` | Select the ramp generator: **P**IO hardware / software on the timing core (**C**) — for the bench comparison of both; session-only, default PIO | `D`/`E` |
| `S` | Start measurement | `D`, then binary data |
| `E` | Stop measurement | data end-marker, then `D` (`E` on engine timeout) |

**While a measurement is running, every command except `E` is silently
ignored** — its ASCII reply would corrupt the binary stream on the same USB
endpoint.

### Validated limits (`T` / `P` are rejected with `E` outside these)

| Parameter | Range | Why |
|---|---|---|
| integration time | 5 µs … 10 s | ADC conversion ≈ 2 µs + margin |
| reset time | 5 µs … 10 s | ramp v2: 5 µs of reset LOW is meaningful (the settle comes after the phase). No-ramp: the 5 µs settle sits inside the window, so at exactly 5 µs the reset line never asserts LOW (degenerate, allowed) |
| ramp dt (software, `GC`) | > 0 … 10 s | one-clock-cycle resolution, no dt-coupled bound — this is what enables markers mode with long integrations (dt = T_int) and ramp sampling with coarse dt |
| ramp dt (PIO, `GP`) | > 0 … 65536·10/clk ≈ 4369 µs | 16.8 fixed-point clock divider; the app's auto-selection never drives the PIO past 100 µs |

These bounds must be mirrored by the host driver's `HARDWARE_LIMITS`.

### Timing model

**No-ramp mode:**

- `T` floors each time to whole system-clock cycles (double-precision
  floor, 1/150 µs steps at the default clock). The host mirrors this.
- Period = integration + reset, exactly (absolute cycle anchors, wrap-safe).
- The reset line is released 5 µs (`RESET_SETTLE_US`) before each sample 1 —
  this settle interval is part of the reset window. The effective reset-low
  time is `reset − 5 µs`.
- The system clock is read at runtime (`clock_get_hz`), not assumed; the
  actual value is reported by `V`.
- Both ADC samples use an identical start→guard→act sequence so the software
  latency of the two sampling instants cancels in the difference.

**Ramp mode — process v2 (both generators, identical structure):**

```
reset phase (RESET LOW, duration = reset time)
  -> settle (RESET HIGH, no pulses: 20 PIO cycles in the PIO generator
     = 2·dt; fixed 2 µs in the software generator)
  -> N = t_int/dt + 1 pulses with edges at 0, dt, 2·dt, …, t_int
     (e.g. t_int = 10 µs, dt = 5 µs → 3 pulses at 0, 5, 10 µs)
  -> the last pulse's period runs out -> restart
```

Period = reset + settle + N·dt. Two generators implement it, selected with
`GP`/`GC` (session-only, default PIO; `Q` reports `gen=`):

- **PIO generator (`GP`)**: zero CPU load, hardware-perfect edges — but
  everything (reset, settle, overhead) is quantized to the **dt/10 tick**
  because the PIO clock scales with dt (clkdiv = clk·dt/10⁷, 16.8 format,
  dt ≤ 4369 µs). At dt = 1 ms one tick is 100 µs.
- **Software generator (`GC`)**: the timing core (SRAM code, interrupts
  disabled, DWT absolute anchors) drives both pins directly. dt, reset and
  settle all quantize to **one system clock cycle** (6.67 ns at 150 MHz),
  there is no dt-coupled quantization and no dt upper bound. Jitter is bus
  arbitration from core 0 only (single-digit ns, non-accumulating — every
  edge re-syncs to the absolute timeline).

Pulses are 50 % duty (dt/2 high) in both generators, so the waveforms are
directly comparable on the scope.

## Host driver behaviour (pico_serial_v1)

- **Auto selection:** dt ≤ 100 µs → PIO (`GP`, raw counts via `O`);
  dt > 100 µs → software generator (`GC`, plain `T`/`P`). No manual
  generator choice in the app.
- **Exact mirror, single source of truth:** the driver's
  `calculate_ramp_timing(dt, int, reset)` computes — in pure Python, never
  by querying the Pico — exactly the values the firmware's `Q` reports
  (int = first-to-last edge, reset = RESET-LOW duration, plus the true
  period = reset + settle + N·dt). The UI's "actual timing" shows these.
- **One-time verification:** after `configure_ramp`, the driver queries `Q`
  once and compares gen/int/reset/dt against its mirror; a mismatch is a
  hard configure failure (logged).
- **Minimum integration = dt** (2 pulses): a request with int < dt is
  rejected before any serial traffic. int is otherwise quantized down to a
  whole multiple of dt (PIO) or of the cycle-floored dt (software); the
  reset quantizes to the dt/10 tick (PIO) or system cycles (software).

## Data stream (no-ramp mode)

Raw binary, little-endian 32-bit words, `(sample1 << 16) | sample2`, two
12-bit readings (start and stop of each integration cycle). A stop is
signalled by the word `0xFFFFFFFF` (both halves > 4095: impossible as data).
The `E` acknowledgement is sent only after the marker, and after `fflush`, so
the host can never confuse ack bytes with data.

Ramp mode streams no data (the PIO only generates trigger pulses for an
external digitizer) but performs the same start/stop handshake.

## Overflow handling

The engine pushes samples into a 1024-word single-producer/single-consumer
ring and **never blocks on the host**. If USB stalls longer than the ring
capacity, samples are dropped and counted; `Q` reports `dropped=` — check it
after every run (a non-zero count means the statistics are computed over a
gapped series). Sustained throughput is bounded by USB CDC (~250 ksamples/s);
use periods ≥ ~5 µs for continuous streaming.

## Building and flashing

```bash
export PICO_SDK_PATH=/path/to/pico-sdk
cmake -B build -S . && cmake --build build
# flash build/pico2_daq_firmware.uf2 (BOOTSEL mount or picotool)
```

`PICO_BOARD=pico2` is set in `CMakeLists.txt` — required: the SDK default
target is the first-generation Pico (RP2040 / Cortex-M0+), which has no DWT
cycle counter and the timing engine would hang. This firmware supports the
Pico 2 (RP2350) only.
