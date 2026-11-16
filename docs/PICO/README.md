# ATOMIQA — Pico 2 DAQ firmware (PICO2DAQ 2.5)

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
| `V` | Identity probe | `V PICO2DAQ 2.5 <16-hex board id> <clk MHz>\n` |
| `Q` | Status / diagnostics | `Q run=<0\|1> mode=<noramp\|ramp> gen=<pio\|c\|fpio\|tpio\|rpio> int=%.3f reset=%.3f dt=%.3f div=<int>.<frac8> pulses=%lu caps=<b0b1b2> event=<ME\|SE> dropped=%lu\n` — `gen` = active ramp generator (`pio` dt-scaled, `c` software, `fpio` fixed-clock, `tpio` transient-test); in ramp mode `int`/`reset`/`dt` are the quantized values the generator will actually produce; `div` = PIO clock divider in 16.8 format (fractional part in 1/256 units, zero-padded to 3 digits; `1.000` on the fixed-clock generator); `pulses` = CONV_START pulses programmed into the generator (0 in no-ramp mode); `caps` and `event` are read back from the pads, so an externally dragged-down line shows 0 |
| `RS` / `RM` | Event mode pin high (SE) / low (ME) | `D`/`E` |
| `C b0 b1 b2` | Capacitor bits, e.g. `C101` (first char = GP17) | `D`/`E` |
| `XN` / `XR` | Select no-ramp (ADC) / ramp (PIO trigger) mode | `D`/`E` |
| `T <int_us>,<reset_us>` | Integration and reset times in µs (manual path) | `D`/`E` |
| `P <dt_us>` | Ramp pulse spacing in µs (manual path) | `D`/`E` |
| `O <div_i>,<div_f>,<reset_pio>,<pulses>` | Program the **dt-scaled** ramp generator with **raw** values — the application path: the host computes everything exactly and the firmware programs these numbers verbatim. Bounds: `1 ≤ div_i ≤ 65535`, `0 ≤ div_f ≤ 255`, `reset_pio ≥ 1`, `pulses ≥ 1` | `D`/`E` |
| `F <half>,<pulses>` | Program the **fixed-clock** ramp generator with **raw** values (same philosophy as `O`): `half` = dt half-period loop count, one pulse spans `(2·half + 7)` sys-clock cycles, so `dt = (2·half + 7)/clk`. Bounds: `half ≥ 1`, `pulses ≥ 1`; the command is rejected unless the system clock is exactly 150 MHz (the generator's reset/settle are program immediates that do not scale) | `D`/`E` |
| `A <reset_ticks>,<pulses>` | Program the **transient-test** generator with **raw** values (test/study mode): `reset_ticks` = the full reset-LOW phase in ~107 ns ticks (the firmware pushes `reset_ticks − 2`; dt is pinned by the integer divider `TRANSIENT_DIV` = 16 → 1.0667 µs, 937.5 kS/s — below the acquisition card's 1 MS/s spec; the settle before the first CONV_START edge is one tick). Bounds: `reset_ticks ≥ 2`, `pulses ≥ 1` | `D`/`E` |
| `B <reset_pulses>,<int_ticks>` | Program the **reset-sampling** transient-test generator with **raw** values: `reset_pulses` = CONV_START pulses *during* the reset phase (the reset LOW lasts exactly `reset_pulses` × 1.0667 µs, first edge simultaneous with the assertion), `int_ticks` = the silent integration phase in ~107 ns ticks. Bounds: `reset_pulses ≥ 1`, `int_ticks ≥ 3` | `D`/`E` |
| `GP` / `GC` / `GF` / `GT` / `GR` | Select the ramp generator: dt-scaled **P**IO hardware / software on the timing core (**C**) / **f**ixed-clock PIO / **t**ransient-test / transient-test sampling during the **r**eset phase — bench comparison; session-only, default PIO | `D`/`E` |
| `S` | Start measurement | `D`, then binary data |
| `E` | Stop measurement | data end-marker, then `D` (`E` on engine timeout) |

**While a measurement is running, every command except `E` is silently
ignored** — its ASCII reply would corrupt the binary stream on the same USB
endpoint.

### Validated limits (`T` / `P` are rejected with `E` outside these)

| Parameter | Range | Why |
|---|---|---|
| integration time | 5 µs … 10 s | ADC conversion ≈ 2 µs + margin |
| reset time | 5 µs … 10 s | ramp v2: 5 µs of reset LOW is meaningful (the settle comes after the phase). No-ramp: the 5 µs settle sits inside the window, so at exactly 5 µs the reset line never asserts LOW (degenerate, allowed). On the fixed-clock generator the reset is **fixed** at 5 µs (a program immediate), selected when that is exactly what is requested |
| ramp dt (software, `GC`) | > 0 … 10 s | one-clock-cycle resolution, no dt-coupled bound — this is what enables markers mode with long integrations (dt = T_int) and ramp sampling with coarse dt |
| ramp dt (fixed-clock PIO, `GF`) | > 0 … 10 s | dt = (2·half + 7) sys-clock cycles: one-cycle grid, zero jitter, no dt-coupled bound, no divider cap (a 32-bit half covers far more than the time range). Generator available at exactly 150 MHz only |
| ramp dt (dt-scaled PIO, `GP`) | > 0 … 65536·10/clk ≈ 4369 µs | 16.8 fixed-point clock divider; the app's auto-selection never drives this generator past 100 µs |
| reset time, transient test (`GT`) | 1 µs … 10 s (~107 ns steps) | the study mode: deliberately below the validated 5 µs measurement minimum; dt is pinned by the `TRANSIENT_DIV` divider (1.0667 µs) |

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

**Ramp mode — process v2 (all generators, identical structure):**

```
reset phase (RESET LOW, duration = reset time)
  -> settle (RESET HIGH, no pulses: 20 PIO cycles in the dt-scaled PIO
     generator = 2·dt; fixed 2 µs in the software and fixed-clock
     PIO generators; one ~107 ns tick in the transient-test generator)
  -> N = t_int/dt + 1 pulses with edges at 0, dt, 2·dt, …, t_int
     (e.g. t_int = 10 µs, dt = 5 µs → 3 pulses at 0, 5, 10 µs)
  -> restart (PIO generators: the last pulse's period runs out; software
     generator: the last pulse holds only min(dt/2, 1 µs) HIGH and the
     cycle wraps immediately — firmware 2.4)
```

Period = reset + settle + N·dt on the PIO generators; on the software
generator = reset + settle + (N−1)·dt + min(dt/2, 1 µs) — in markers mode
(dt = T_int) the old trailing low half doubled the period. Four generators implement it, selected
with `GP`/`GC`/`GF`/`GT` (session-only, default PIO; `Q` reports `gen=`):

- **Fixed-clock PIO generator (`GF`, 2.2)**: a second state machine whose
  clock is fixed at clk_sys (divider 1 — integer, hence **zero jitter**)
  and whose reset (5 µs) and settle (2 µs) are immediate-coded in the
  program, so the dt half-period and the pulse count are the only
  programmed values. dt = (2·half + 7) sys-clock cycles — the same
  6.67 ns grid as the software generator — with **no dt-coupled
  quantization** (reset and settle no longer scale with dt: the 2·dt
  dead time becomes a fixed 2 µs) and **no dt upper bound**. The
  generator for reset = 5 µs runs (the firmware minimum), including
  markers mode with long integrations, at zero CPU load. 150 MHz only.
- **dt-scaled PIO generator (`GP`)**: zero CPU load, hardware-perfect
  edges — but everything (reset, settle, overhead) is quantized to the
  **dt/10 tick** because the PIO clock scales with dt (clkdiv = clk·dt/10⁷,
  16.8 format, dt ≤ 4369 µs). At dt = 1 ms one tick is 100 µs. The
  divider rounds UP one 1/256 step (strictly greater than requested —
  dt = 1 µs runs at 999.74 kS/s, safely under the card's 1 MS/s spec).
- **Software generator (`GC`)**: the timing core (SRAM code, interrupts
  disabled, DWT absolute anchors) drives both pins directly. dt, reset and
  settle all quantize to **one system clock cycle** (6.67 ns at 150 MHz),
  there is no dt-coupled quantization and no dt upper bound. Jitter is bus
  arbitration from core 0 only (single-digit ns, non-accumulating — every
  edge re-syncs to the absolute timeline). Since 2.4 the cycle wraps right
  after the last pulse's short high (min(dt/2, 1 µs)) instead of running
  its low half out — markers-mode windows come at ~1/T_int, not ~1/2T_int
  (dt ≤ 2 µs runs keep the exact old waveform).

- **Reset-sampling transient-test generator (`GR`, bench)**: the same
  pinned-divider clock (dt = 1.0667 µs), but the pulses run **during the
  reset phase** (side 0, first edge simultaneous with the reset assertion
  — the discharge transient is digitized from its very start) and the
  integration phase is silent. Reset = exactly `reset_pulses` × dt;
  period = reset + int.
- **Transient-test generator (`GT`, 2.3)**: the dt-scaled architecture
  with the clock **pinned by an integer divider** (16 at 150 MHz — zero
  jitter, one tick ≈ 106.7 ns, dt = 10 ticks = 1.0667 µs = 937.5 kS/s;
  divider 15 would sit exactly at the acquisition card's 1 MS/s spec
  with zero margin), so both registers carry run
  parameters: the reset length is sweepable in ~107 ns ticks — **down to
  1 µs, below the validated measurement minimum** (a study mode, selected
  explicitly in the app's timing page; the app's true-values card shows
  the applied dt) — and the settle before the first
  CONV_START edge is a **single tick**, so the ramp is sampled
  right at the reset release and the front-end transient is visible in
  the data. Runs on pio1.

Pulses are ~50 % duty in all generators (the fixed-clock one is 50 % ± 1
cycle — only rising edges matter to the external digitizer), so the
waveforms are directly comparable on the scope.

## Host driver behaviour (pico_serial_v1)

- **Auto selection**: reset = 5 µs (quantized to the cycle grid) →
  fixed-clock generator (`GF`, raw counts via `F`) at **any** dt — zero
  jitter, one-cycle resolution, reset/settle independent of dt, including
  markers mode with long integrations; otherwise dt ≤ 100 µs → dt-scaled
  PIO (`GP`, raw counts via `O`), dt > 100 µs → software generator (`GC`,
  plain `T`/`P`). No manual generator choice in the app.
- **Exact mirror, single source of truth**: the driver's
  `calculate_ramp_timing(dt, int, reset)` computes — in pure Python, never
  by querying the Pico — exactly the values the firmware's `Q` reports
  (int = first-to-last edge, reset = RESET-LOW duration, plus the true
  period = reset + settle + N·dt; on the fixed-clock generator reset is
  the fixed 5 µs and settle 2 µs). The UI's "actual timing" shows these.
- **One-time verification**: after `configure_ramp`, the driver queries `Q`
  once and compares gen/int/reset/dt (and pulses on the `O`/`F` paths)
  against its mirror; a mismatch is a hard configure failure (logged).
  `connect()` refuses any firmware below **2.5** (2.5 fixed the software
  generator's reset phase — a stale DWT anchor ran reset for up to one
  dt; 2.4 shortened its period; 2.3 added `GT`/`A`; 2.2 added `GF`/`F`). The transient test is an explicit UI
  option (never auto-selected): dt pinned to 1 µs, the reset settable
  from 1 µs in 1 µs steps — below the validated measurement minimum —
  with the first edge 100 ns after the reset release.
- **Minimum integration = dt** (2 pulses): a request with int < dt is
  rejected before any serial traffic. int is otherwise quantized down to a
  whole multiple of dt (dt-scaled PIO) or of the cycle-floored dt
  (software/fixed); the reset quantizes to the dt/10 tick (dt-scaled PIO)
  or system cycles / the fixed 5 µs (software/fixed).

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
