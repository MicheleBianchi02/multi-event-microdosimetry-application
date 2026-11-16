# PICO2DAQ 2.5 — firmware internals and the `pico_serial_v1` driver

This document describes **how the Pico 2 DAQ works internally** — the
firmware's dual-core architecture, its timing engines and its electrical
interfaces — and then, in the same document, **the application driver**
(`pico_serial_v1`, `hardware/pico/pico_daq.py`) that commands it: its exact
timing mirror, its concurrency model and its error handling.

- Firmware source: [v2.2/main.c](v2.2/main.c), [v2.2/ramp_control.pio](v2.2/ramp_control.pio),
  [v2.2/ramp_fixed.pio](v2.2/ramp_fixed.pio), [v2.2/ramp_transient.pio](v2.2/ramp_transient.pio),
  [v2.2/ramp_transient_reset.pio](v2.2/ramp_transient_reset.pio),
  [v2.2/CMakeLists.txt](v2.2/CMakeLists.txt)
- Protocol/pin-map quick reference: [README.md](README.md) (this file is the
  deep dive; the README stays the compact reference)
- Driver source: `hardware/pico/pico_daq.py`; driver tests: `tests/test_pico_driver.py`

---

## Part 1 — The firmware (PICO2DAQ 2.5)

### 1.1 Platform and build

- Target: Raspberry Pi **Pico 2** — RP2350, dual **Cortex-M33** at
  **150 MHz** (the system clock is read at runtime and reported, never
  assumed).
- Peripherals used: the RP2350 **ADC** (12-bit, GP26 = channel 0), two
  **PIO** state machines of pio0 (SM0: dt-scaled ramp generator; SM1:
  fixed-clock ramp generator — both programs fit in the block's 32 shared
  instructions) plus one SM of **pio1** (transient-test generator; pio0's
  instruction memory is full) — only one SM ever runs, the **DWT cycle counter**
  (`CYCCNT`, the sub-microsecond timing backbone), the **SIO** GPIO block
  (single-cycle pin writes from both cores), **USB CDC** (`stdio`), and the
  64-bit unique board id.
- Build: CMake + Pico SDK, C11. `PICO_BOARD=pico2` **must** be set before
  the SDK is pulled in (the default first-generation Pico target has no DWT
  cycle counter — the timing engine would hang). Output:
  `pico2_daq_firmware.uf2` (BOOTSEL drag-and-drop or `picotool`).

### 1.2 Dual-core architecture

The firmware is built around one hard rule: **the timing engine never
waits for USB.** Everything time-critical runs on core 1; core 0 owns the
USB endpoint.

**Core 1 — timing engine** (`timing_engine_main`):

- Launched with `multicore_launch_core1`; the engine functions are compiled
  `__not_in_flash_func` (strictly **executed from SRAM** — no XIP stalls)
  and run with **interrupts disabled** for the whole run
  (`save_and_disable_interrupts`). Nothing on the chip can preempt a timing
  edge.
- All scheduling uses the **DWT cycle counter** with *absolute* per-cycle
  targets (`wait_until_cycle` compares `(CYCCNT − target) > 2³¹`, so the
  32-bit wrap is handled): every edge re-synchronizes to an absolute
  timeline instead of accumulating relative delays — jitter never adds up.
- Entry point dispatches on `sys_cfg.is_ramp_mode` / `sys_cfg.ramp_gen`
  between the four engines below. When idle it spins on
  `tight_loop_contents()`; between runs the integrator reset line is held
  LOW (capacitor discharged — this is why the driver's `arm()` is a no-op).

**Core 0 — USB engine** (`main` loop):

- Non-blocking command parsing (`getchar_timeout_us(0)`, lines terminated by
  `\n`/`\r`) and, while a no-ramp run is active, streaming of ring contents
  in batches of up to 32 words (`fwrite`) so parsing keeps running between
  batches.
- While a measurement runs every command except `E` is **silently ignored**
  (`process_usb_command`): an ASCII reply would corrupt the binary stream
  on the shared endpoint. The driver mirrors this refusal.

**Producer→consumer handoff — SPSC ring buffer:**

- 1024 × uint32 words (`RING_WORDS`, power of two), `ring_head` written
  only by core 1, `ring_tail` only by core 0, `__dmb()` barriers on both
  sides.
- `ring_push` **never blocks**: if the ring is full (USB stall), the word is
  **dropped and counted** in `dropped_words` — the period is never disturbed
  by the host. `Q` reports the count; the driver warns after every run when
  it is non-zero (a non-zero count means the statistics ran over a gapped
  series).
- Sustained throughput is bounded by USB CDC (≈ 250 ksamples/s); use periods
  of ≳ 5 µs for gap-free streaming.

### 1.3 The three timing engines

#### No-ramp engine (`run_noramp_adc_mode`) — the classic measurement

One integration cycle, on absolute cycle anchors:

```
t1 = anchor
  ADC start-once at t1  (the SAMPLING INSTANT — the hold capacitor
        disconnects when START_ONCE is set), 10-NOP acquisition guard,
        wait READY, read sample1 (≈2 µs conversion)
t2 = t1 + int_cycles
  ADC start-once at t2, same 10-NOP guard  → RESET asserted immediately
        after the guard (outside the SAR acquisition window of sample 2);
        sample2 read during the reset window (packing + ring push overlap it)
t1_next = t2 + reset_cycles
  RESET released exactly at t1_next − 5 µs (RESET_SETTLE_US, part of the
        reset window) so the front-end transient settles before sample 1
repeat with t1 = t1_next
```

Properties:

- **Period = T_int + T_reset, exactly** — the anchors are absolute, so the
  ≈2 µs conversions, guards and ring pushes all hide inside the reset
  window.
- Both samples use an **identical start→guard→act sequence**: the software
  latency of the two sampling instants is common-mode and cancels in the
  start/stop difference.
- On stop (`is_running` cleared by core 0) the engine forces RESET low
  (safe state) and pushes the end-of-stream marker.

#### Ramp mode, dt-scaled PIO generator (`run_ramp_pio_mode` + `ramp_control.pio`, `GP`)

Zero-CPU hardware generation of the CONV_START pulse train for an external
digitizer (the NI card). The PIO program (`side-set` drives the reset line,
`set` drives CONV_START):

1. **Pull phase**: core 1 pushes `reset_pio − 1` and `pulses − 1` into the
   TX FIFO; the SM stores the reset count in its ISR register.
2. **Reset phase**: `jmp x--` loop holds RESET **LOW** for `reset_pio`
   PIO-cycle ticks.
3. **Settle**: RESET released (side 1) for exactly **20 PIO cycles**
   (`RAMP_SETTLE_PIO_CYCLES`) — at 50 % duty this equals 2·dt — with no
   pulses yet.
4. **Pulse train**: `pulses` iterations of high `[4]`/low `[3]`/jump —
   **10 PIO cycles per pulse, 50 % duty** — edges at 0, dt, 2·dt, …,
   (N−1)·dt with N = T_int/dt + 1; the last pulse's low half runs out,
   then the program `.wrap`s to the reset phase.

Because the PIO clock is *derived from dt* (`clkdiv = clk_sys · dt / 10⁷`,
16.8 fixed-point format), everything in this engine is quantized to the
**dt/10 tick** — at dt = 1 ms one tick is 100 µs. The 16.8 divider bounds
dt to 65536·10 sys-clocks ≈ **4369 µs** at 150 MHz. The C side must claim
the pins for the PIO and set pindirs while the SM runs (the program never
sets pindirs); on stop the pins are handed back to the SIO with safe idle
levels. In this mode **no ADC sampling happens** — the Pico's single ADC
cannot acquire while its pins generate triggers
(`SUPPORTS_RAMP_WHILE_ACQUIRING = False`).

#### Ramp mode, fixed-clock PIO generator (`run_ramp_fixed_mode` + `ramp_fixed.pio`, `GF`)

The same v2 process on a **second state machine (pio0 SM1)** whose clock is
fixed at clk_sys (divider 1) and whose reset (**5 µs = 750 cycles**) and
settle (**2 µs = 300 cycles**) are immediate-coded delay loops in the
program. Freeing those phases from any register is what leaves room for
the dt: the half-period count lives in the SM's **ISR** and the pulse count
in the **OSR** (re-read every cycle by `mov y, osr`, which never shifts the
OSR — the same trick the dt-scaled program uses):

1. **Pull phase**: core 1 pushes `half` and `pulses − 1` into the TX FIFO.
2. **Reset phase**: fixed counted loop holds RESET **LOW** exactly 750
   sys-clock cycles.
3. **Settle**: RESET released for exactly 150 cycles, reloading the pulse
   count from the OSR meanwhile.
4. **Pulse train**: `pulses` iterations of
   rising-edge / counted-high / falling-edge / counted-low —
   **2·half + 7 sys-clock cycles per pulse** — rising edges at exactly
   0, dt, 2·dt, …, (N−1)·dt; the last pulse runs out and the program
   `.wrap`s to the reset phase.

Properties that follow:

- **Zero jitter**: the divider is an integer (1) — the dt-scaled
  generator's fractional 16.8 divider jitters every edge by up to one
  sys clock.
- **One-cycle dt resolution with no dt-coupled quantization**: dt rounds
  UP to the (2·half + 7)-cycle grid (6.67 ns at 150 MHz — the same grid
  as the software generator, odd-only, so an even request lands one
  cycle above; the applied dt is never faster than requested, which
  keeps a rate-limited clock consumer such as the NI 1 MS/s card inside
  spec); the reset and settle
  do **not** scale with dt, so the 2·dt dead time becomes a fixed 2 µs
  and the reset is exactly 5 µs instead of a dt/10-quantized phase.
- **No dt upper bound**: a 32-bit half covers far beyond the 10 s time
  range — this generator also serves markers mode (dt = T_int) at zero
  CPU load.
- **150 MHz only**: the reset/settle immediates do not scale with the
  system clock, so `GF` and `F` are rejected on any other clock.

Duty is 50 % ± 1 cycle (only rising edges matter to the digitizer). The
pin claiming/pindirs/hand-back dance is identical to the dt-scaled
generator; the two SMs are never enabled together.

#### Transient-test mode (`run_ramp_transient_mode` + `ramp_transient.pio`, `GT`)

The study mode for the front-end transient: the dt-scaled program's
architecture with the clock **pinned by an integer divider**
(`TRANSIENT_DIV` = 16 at 150 MHz — zero jitter; one tick = 16/150 µs ≈
106.7 ns, one pulse = 10 ticks = **1.0667 µs = 937.5 kS/s**) on a state
machine of **pio1**. Divider 15 would give exactly 1.000 µs — the
acquisition card's absolute 1 MS/s spec with zero margin, and any
positive crystal error of the nominal 150 MHz clocks it above spec (the
B35 failure class; the fixed generator is derated for the same reason) —
hence 16, and the applied (not nominal) dt is shown by the app's
true-values card. With dt riding on the fixed clock, both registers
carry run parameters: core 1 pushes the reset phase in ticks (ISR — the
phase is `pushed + 2` cycles, so the firmware pushes `reset_ticks − 2`)
and the pulse count (OSR). The settle between the reset release and the
first CONV_START edge is **one ~107 ns tick** (`mov y, osr side 1`,
first `set pins, 1` the next cycle), and each pulse is 10 cycles. The
reset is sweepable down to 1 µs — deliberately below the validated
measurement minimum; the app exposes it as an explicit test option
(never auto-selected) with its own mirror
(`calculate_transient_timing`) and raw command (`A <reset_ticks>,<pulses>`,
verified exactly via `Q`'s `gen=tpio` including the pulse count).

#### Reset-sampling transient-test mode (`run_ramp_transient_reset_mode` + `ramp_transient_reset.pio`, `GR`)

The transient study's other half, on pio1 SM1: the same pinned-divider
clock (16 at 150 MHz, zero jitter, ~107 ns ticks), but the pulse train
runs with **side 0** — the CONV_START pulses are generated *during* the
reset phase, the first edge simultaneous with the reset assertion, so
the digitizer captures the integrator's discharge from its start. The
integration phase is silent (reset HIGH, counted in ticks: the phase has
a fixed 3-cycle overhead, so `B` programs `int_ticks` and the firmware
pushes `int_ticks − 3`). Register budget: ISR = the reset-phase pulse
count (the reset lasts exactly pulses × 1.0667 µs — the counter is
reloaded at the *end* of the integration phase, which is why no cycle is
lost inside the reset phase), OSR = the integration ticks. The app
exposes it as the *Sample During Reset* study option (fields swapped);
`Q` reports `gen=rpio` with reset = pulses·dt and
int = ticks·dt/10.

#### Ramp mode, software generator (`run_ramp_soft_mode`, selected with `GC`)

The same v2 process (reset LOW → settle → N pulses at k·dt → restart)
driven from the timing core with DWT absolute anchors. Since 2.4 the
cycle restarts right after the LAST pulse's short high
(min(dt/2, `RAMP_SOFT_LAST_HIGH_US` = 1 µs)) instead of running its low
half out — the trailing low half was pure dead time and, in markers mode
(dt = T_int), doubled the period. Edge positions and the pulse count are
unchanged; dt ≤ 2 µs runs keep the exact old waveform:

- dt, reset and settle quantize to **one system clock cycle** (6.67 ns at
  150 MHz); there is **no dt-coupled quantization and no dt upper bound**.
  This is what makes markers mode possible with long integrations (dt =
  T_int) and allows coarse ramp-sampling steps.
- Settle is a fixed **2 µs** (`RAMP_SETTLE_SOFT_US`).
- Jitter is bus-arbitration from core 0 only — single-digit ns,
  non-accumulating (every edge re-syncs to the absolute timeline).
- This generator is what makes **markers mode** possible with long
  integrations (dt = T_int, two CONV_START edges per window) — see the
  driver's generator auto-selection.

#### Parameter programming: the `O`/`F` commands vs `T`/`P`

The raw generator parameters (`div_int, div_frac8, reset_pio, pulses` for
the dt-scaled PIO; `half, pulses` for the fixed-clock PIO) stored in
`sys_cfg` are the single source of truth for their engines. They are set
either by `recompute_ramp_params()` (from `T`/`P`, the manual bench path,
double-precision math) or — **the application path** — by the `O`/`F`
commands, which program host-computed raw counts *verbatim*, so no
firmware-side rounding can disagree with what the host previewed. Both
recompute the effective `int/reset/dt` display fields from the raw counts,
so `Q` stays truthful in every path.

### 1.4 Timing summary and limits

No-ramp: `T` floors both times to whole system-clock cycles in **double
precision** (`(uint32_t)(us · clk_MHz)`); period = int + reset exactly. The
5 µs settle sits *inside* the reset window — at exactly reset = 5 µs the
reset line never asserts LOW (degenerate but allowed). Ramp: period =
reset + settle + N·dt with N = T_int/dt + 1.

| Parameter | Range | Enforced by |
|---|---|---|
| integration time | 5 µs … 10 s | `T` validation (ADC conversion ≈ 2 µs + margin) |
| reset time | 5 µs … 10 s | `T` validation |
| ramp dt, fixed-clock PIO generator | > 0 … 10 s | `P` validation (one-cycle grid, no dt-coupled bound); `F` trusts the host; generator rejected off-150 MHz |
| ramp dt, software generator | > 0 … 10 s | `P` validation |
| ramp dt, dt-scaled PIO generator | > 0 … 65536·10/clk ≈ 4369 µs | `P` validation; `O` trusts the host |
| reset time, transient test (`GT`) | 1 µs … 10 s (~107 ns steps) | `A`/recompute; a study mode below the validated minimum |

These bounds must be mirrored by the driver's `HARDWARE_LIMITS` (they are).

### 1.5 Electrical interfaces

All GPIO is 3.3 V logic (RP2350). "GPxx" is the GPIO number, not the header
pad position (see the README's pin-map note).

| GPIO | Direction | Function | Idle level |
|---|---|---|---|
| GP14 | out (SIO/PIO side-set) | Integrator reset — **1 = integrate, 0 = reset** | LOW (capacitor discharged) |
| GP15 | out (SIO/PIO set) | CONV_START — external conversion trigger / ramp sampling clock, **rising-edge** active, 50 % duty in ramp mode | LOW |
| GP16 | out | Event mode: **0 = ME, 1 = SE** (the line is owned by the gain role) | LOW (ME) |
| GP17–GP19 | out | Capacitor bank bits 0–2 (gain role; driven as a 3-bit group by `C b0b1b2`) | 000 |
| GP26 | in (ADC0) | Channel-1 analog input, 12-bit, **fixed 0…3.3 V span** (bare ADC pin, no front-end scaler) | — |
| USB | CDC-ACM | Command/reply channel and (no-ramp) binary data stream; baud rate setting is irrelevant | — |

Timing relationships an oscilloscope will show: integration starts when
GP14 rises (≥ 5 µs before the first sample); in ramp/markers mode CONV_START
edges fall inside the integration window at 0, dt, …, T_int — **never**
during reset — which is exactly the contract the NI card and the ramp
window extractor rely on.

> **Edge-time footnote (markers mode).** The "0" of the edge train is the
> *pulse-train* start, which begins one settle after the reset release:
> 2 µs (fixed-clock and software generators) or 2·dt
> (dt-scaled PIO generator, whose settle is 20 PIO cycles). In markers
> mode with the dt-scaled PIO generator and T_int ≤ 100 µs the two edges
> therefore sit at 2·T_int and 3·T_int after GP14 rises — the pair still
> spans exactly T_int (the measurement is unaffected; both samples lie on
> the same linear ramp), but the absolute edge phase differs from the
> naive "at 0 and T_int" reading.

---

## Part 2 — The application driver (`pico_serial_v1`)

`hardware/pico/pico_daq.py` — class `PicoSerialDAQ(IDataAcquisition,
ITimingController, IGainController)`. One physical board can therefore hold
the `timing`, `gain_chX` and `acquisition` roles at once (the shipped
all-Pico setup does), or only a subset.

### 2.1 Identity, connection and the liveness gate

`connect()` opens the serial port (persistent name `/dev/atomiqa/<device_id>`
derived from the config by the `HardwareManager`, or a session-only manual
mapping) and immediately runs the **`V` identity handshake**:

- reply parsed against `^V (\S+) (\S+) (\S+) (\d+(?:\.\d+)?)$` → firmware
  name, version, 16-hex board id, clock MHz;
- the connection **refuses** anything that is not `PICO2DAQ` firmware or
  whose system clock differs from **150 MHz** by more than 0.5 MHz: the
  whole timing mirror below assumes 150 MHz, and a bare Pico / wrong board /
  reflashed clock would silently rescale every period.

The parsed identity is kept (`driver.identity`). **PICO2DAQ 2.5 is the
required firmware**: the handshake confirms the firmware *name*, a version
≥ 2.5 and the 150 MHz clock. The version gate exists because the driver
assumes the newest generator behaviour (2.5 fixed the software generator's
reset phase — the early wrap left a stale DWT anchor that ran reset for up
to one dt; 2.4 shortened its period; 2.3 added the transient-test
generators `GT`/`A`/`GR`/`B` and moved the fixed generator's settle to
2 µs; 2.2 added `GF`/`F`) — an older board would silently run different
timing than the mirror reports. Boards still running 2.4 or older must be
reflashed, and the hardware configuration records
`firmware_version: "2.5"` as required.

### 2.2 Layer-1 limits (hardcoded, mirrored from the firmware)

```python
HARDWARE_LIMITS = TimingCapability(
    integration_time_us = bounds(5, 10_000_000, 1),   # µs
    reset_time_us       = bounds(5, 10_000_000, 1),   # µs
    period_us           = None,                        # derived: int + reset
    ramp_sampling       = supported, dt bounds(1, 10_000_000, 1),
)
GAIN_MAX_PINS = 3          # 'C' carries three bits (GP17..19 in mask order)
ADC_BITS = 12
ADC_NATIVE_RANGE = 0.0 … 3.3 V
SUPPORTS_RAMP_WHILE_ACQUIRING = False   # one ADC: triggers and sampling
                                        # are mutually exclusive on this board
```

Every mutating call re-validates against these bounds and **rejects** (never
clamps) out-of-range values. The hardware config's declared envelope is
checked against them at load time (registry descriptor). The ramp-dt limit
(`RAMP_DT_MAX_US = 10 s`) is simply the general time range: the fixed-clock
and software generators — the app paths for any dt when reset = 5 µs, and
for dt > 100 µs otherwise — have one-clock-cycle resolution and no
dt-coupled bound, and the auto-selection never drives the dt-scaled PIO
generator past 100 µs, so its 16.8-divider constraint (≈ 4369 µs) is
trivially satisfied. `_validate_ramp_dt_us()` is therefore a plain range
check (0 < dt ≤ 10 s). Support for firmware older than 2.5 has been
removed; 2.2 is required.

### 2.3 Command plumbing

- `_send_command(cmd)`: refuses commands while the reader thread is alive
  (the firmware would ignore them; a stray reply would corrupt the stream),
  `reset_input_buffer()` → write `cmd\n` → wait up to `TOTAL_TIMEOUT`
  (1.5 s) for the single-byte reply; anything but `D` (including timeout)
  logs and returns False.
- `_transact_line(cmd)`: same, for the line replies (`V`, `Q`).
- All port access is serialized by one `threading.Lock`; the driver never
  talks to the port from two threads at once.
- Constants: `SERIAL_TIMEOUT` 0.1 s (per-read), `QUERY_TIMEOUT` 0.5 s
  (V/Q), `READER_BATCH` 1000 tuples, `STREAM_MARKER` 0xFFFFFFFF.

### 2.4 Interface implementations

| Interface method | Firmware exchange | Notes |
|---|---|---|
| `set_gpio_mask([b0,b1,b2])` | `C b0b1b2` → `D` | mask order = config pin order = GP17, GP18, GP19; wrong length rejected before any traffic |
| `set_event_mode(se)` | `RS` (SE) / `RM` (ME) → `D` | the SE/ME line belongs to the gain role |
| `configure_noramp(int, reset)` | `XN` then `T int,reset` → `D` | validates limits; ignores a `period_us` argument (the firmware derives the period) |
| `calculate_actual_timing(...)` | **no traffic** | pure mirror: `floor(us · 150)/150` µs per time, period = int + reset, freq = 1/period |
| `configure_ramp(dt, int, reset)` | `XR`, `GF`/`GP`/`GC`, then `F …`/`O …` or `T`+`P` | see §2.5 |
| `set_mode(debug)` | none | **no-op**: the firmware always transmits start/stop; the operational difference is derived by the driver on receipt |
| `set_channels(multiple)` | none | **no-op**: single-ADC firmware; the channel count comes from the config |
| `arm()` | none | **no-op**: while idle the engine holds the integrator in reset, and every run opens with a clean reset→settle→sample sequence |
| `get_adc_settings()` | none | `{bits: 12, input_range_volts: 0…3.3, supports_oversampling: False}` |
| `set_input_range(min, max)` | none | accepts only the native 0…3.3 V (mislabeled ranges would corrupt every charge value) |
| `start_measurement(session, data_model, ramp_sink)` | `S` + stream | refuses a `ramp_sink` (see §2.7) and a double start; spawns the reader thread |
| `stop_measurement()` | `E` → marker → `D` | see §2.6 |

### 2.5 Ramp configuration: the exact mirror and the `O`/`F` paths

`calculate_ramp_timing(dt, int, reset)` is a **pure computation** that
reproduces bit-exactly what the firmware will generate — the same numbers
its `Q` command reports — and decides *representability*:

- **Generator auto-selection**: reset quantizing to exactly **5 µs** (the
  firmware minimum) → fixed-clock PIO (`GF`) at **any** dt; otherwise
  dt ≤ **100 µs** → dt-scaled PIO (`GP`), above it software (`GC`) — a
  bench-measured crossover: below the threshold the PIO's
  hardware-perfect edges win despite the dt/10 tick quantization, above it
  the software engine's one-cycle resolution wins. The fixed generator
  dominates both whenever its fixed reset is acceptable: zero jitter,
  one-cycle resolution, reset/settle independent of dt, zero CPU load.
- **Fixed path** (`_fixed_sm_raw_counts`: `dt_cycles = max(9, ⌊dt·150⌋)`;
  `half = max(1, ⌈(dt_cycles − 7)/2⌉)` — round UP, the grid is odd-only
  so the applied dt is never faster than requested; `dt_eff =
  (2·half + 7)/150 MHz` — the same 6.67 ns grid as the software path;
  `intervals = ⌊int_cycles/dt_eff_cycles⌋` with the markers clamp
  (against the requested cycle count); actual
  reset = the fixed 5 µs; settle = 2 µs; **period = 5 + 2 +
  (intervals+1)·dt_eff µs**).
- **dt-scaled PIO path** (`div = 150e6 · dt / 1e7`, rounded UP to the next
  1/256 step plus one more when the request lands exactly on a step — the
  applied dt is strictly greater than requested, never faster: exactly
  1.000 µs is exactly 1 MS/s and sits at the acquisition card's spec
  limit; one step = 0.26 ns → 999.74 kS/s; `dt_eff = div_actual · 10 µs/…`; `intervals =
  ⌊int_cycles·1e6/(clk·dt)⌋`; `reset_pio = ⌊reset_cycles·1e6/(clk·dt)⌋`;
  actual int = first-to-last edge = `intervals · dt_eff`; actual reset =
  RESET-LOW = `reset_pio · dt_eff/10`; settle = 20 PIO cycles = 2·dt_eff;
  **period = reset + settle + (intervals+1)·dt_eff**).
- **Software path** (`dt_cycles = max(1, ⌊dt·150⌋)`; `intervals =
  ⌊int_cycles/dt_cycles⌋`; actual int = `intervals · dt_eff`; actual reset =
  reset floored to cycles; settle = 2 µs; **period = reset + 2 +
  intervals·dt + min(dt/2, 1 µs)** — the 2.4 short last pulse; the PIO
  paths keep `reset + settle + N·dt`).
- **Transient path** (`_transient_raw_counts`, explicit UI option — never
  auto-selected): dt pinned by the integer divider `TRANSIENT_DIV` = 16
  (1.0667 µs at 150 MHz — nominal "1 µs"; zero jitter; 937.5 kS/s, below
  the acquisition card's 1 MS/s spec — divider 15 would sit exactly at
  it with zero margin); the reset rounds UP to whole ~107 ns ticks
  (never shorter than requested); settle = one tick; **period =
  reset + tick + N·dt µs**. Programmed via `GT` + `A reset_ticks,pulses`,
  verified exactly like the other raw paths (gen `tpio`, pulses
  included). The reset may go down to 1 µs — below the validated
  measurement minimum: the study mode. The applied dt is shown in the
  timing window's TRUE RAMP Δt tile.
- **Rejections** (`ValueError` with the reason, surfaced by the timing
  preview): `int < dt` (the minimum integration is one interval = 2 pulses)
  and dt outside the 1 µs … 10 s range (§2.2).

`configure_ramp` programs exactly the mirror's values: `XR` (mode), the
generator selection, then — fixed — the raw counts via
`F half,intervals+1`, — dt-scaled PIO — the raw counts via
`O div_int,div_frac8,reset_pio,intervals+1` (in both cases no
firmware-side rounding can drift from the preview), or — software — plain
microseconds via `T` + `P`. Afterwards it queries **`Q` once** and
compares `gen`, `int`, `reset`, `dt` against the mirror (± 0.01 µs, plus
the exact pulse count on the `O`/`F` paths): a mismatch is a hard
configure failure (logged with both sides). The UI never queries the
device — the preview values *are* the mirror.

### 2.6 Acquisition: the reader thread

`start_measurement` sends `S` (after refusing a ramp sink and a double
start) and spawns a daemon reader thread:

- Polls `in_waiting`, reads the available chunk under the port lock, and
  parses **4-byte little-endian words** `(sample1 << 16) | sample2`.
- `word == 0xFFFFFFFF` → end-of-stream: any bytes after it (the stop ack)
  are parked in `_tail_bytes` for `stop_measurement`, and `_stream_ended`
  is set.
- A word whose halves exceed 12 bits (and is not the marker) means lost
  byte alignment: **one byte is dropped and parsing retried**
  (`_resync_drops` counter) — should not happen with the marker handshake,
  but cannot silently corrupt the series.
- Per word, in debug mode: `((start, stop), (missing, missing))`; in
  operational mode: `(stop − start, missing)` (negative → missing), where
  `missing = overflow_sentinel(12) = 4096` — one past full scale, mapped to
  NaN by the conversion layer (the Pico has no per-sample overflow flag;
  the single-detector run fills the absent channel this way, exactly like
  the simulator).
- Tuples accumulate into batches of `READER_BATCH = 1000` and are handed to
  `data_model.append_batch(...)` (the thread-safe staging buffer); the
  partial batch is flushed in a `finally` block.

`stop_measurement` writes `E`, waits up to `period + 3 s` for the
end-of-stream marker (the engine finishes its last cycle first — nothing
between "stop" and the halt is lost), joins the thread (2 s), takes the
`D` ack from the tail buffer or the port, and finally reports run
diagnostics: a warning for any resync-dropped bytes and a `Q` query whose
non-zero `dropped=` (firmware ring overflows) is logged loudly.

### 2.7 Timing-only operation (`start_timing` / `stop_timing`)

When the Pico holds **only** the timing role (bench tests with the virtual
acquisition, or a split setup with the NI card), `HardwareManager` starts
the run through `start_timing()` instead of `start_measurement()`:

- `start_timing()` sends `S` exactly like the acquisition path — the
  firmware streams into its ring, where unread words are dropped and
  counted (the timing itself is unaffected) — or, in ramp/markers mode,
  simply enables the trigger generator.
- `stop_timing()` discards the unread stream, sends `E`, then consumes
  everything up to the end-of-stream marker and the `D` ack (bounded by
  `period + 3 s`), and reports the same diagnostics. Returning without the
  marker+ack is an error (stalled/unplugged device).

### 2.8 End-to-end sequence for an all-Pico measurement

```
connect()                 V → "V PICO2DAQ 2.5 <id> 150"        (liveness gate)
set_mode(True)            –           (no-op: always start/stop)
set_channels(True/False)  –           (no-op: single ADC)
set_gpio_mask([...])      C b0b1b2 → D                          (gain)
set_input_range(0, 3.3)   –           (native range accepted)
configure_timing(...)     XN + T int,reset  (internal mode)
                          XR + GF + F …     (ramp/markers, reset = 5 µs)
                          XR + GP + O …     (ramp, dt ≤ 100 µs, other reset)
                          XR + GC + T + P   (ramp/markers, dt > 100 µs, other reset)
                          XR + GT + A …     (transient test: dt pinned 1.0667 µs, reset from 1 µs)
                          + one-time Q verification in ramp modes
arm()                     –           (no-op: idle state = reset)
start_measurement(...)    S → D, then the uint32 word stream
stop_measurement()        E → …words… 0xFFFFFFFF → D, Q diagnostics
```

The Pico **cannot** run ramp triggers and ADC acquisition at the same time
(one ADC): the manager enforces `SUPPORTS_RAMP_WHILE_ACQUIRING = False` and
disables the Sample Ramp option when this board holds both roles.

### 2.9 Known limitations

- **Single channel**: one ADC input (GP26); the config's
  `acquisition.num_channels` is the binding constraint (a Pico can *time*
  two channels for the NI card but *acquire* one).
- **USB backpressure**: ≈ 250 ksamples/s sustained; faster periods gap the
  series (ring drops are counted and reported — check the log after runs).
- **No per-sample overflow flag**: overflow appears simply as a full-scale
  reading; the 0…3.3 V range means the integrator must stay inside the ADC
  span.
- **Ramp sampling is monitoring-only** here: the Pico never streams ramp
  samples itself (it has no second ADC to sample with); the streaming
  acquisition driver for ramp data is the NI card (`ni_daqmx_v1`).
