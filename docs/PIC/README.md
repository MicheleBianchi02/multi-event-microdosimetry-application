# ATOMIQA — PIC18F8723 DAQ firmware (v2.2)

Firmware for the Microchip **PIC18F8723** mounted on the **PICDEM PIC18
Explorer** board that acts as the timing controller, gain controller, ADC
readout and event-mode line of the older ATOMIQA measurement chain.

It is written in **MPASM assembly** and built with the classic **MPLAB**
(pre-X, v8.x). The driver speaking its protocol in the application is
`pic_serial_v1`; the firmware internals and the driver are described together
in [FIRMWARE_AND_DRIVER.md](FIRMWARE_AND_DRIVER.md). An older revision of
this firmware was reviewed in `code_report.pdf` — that report does **not**
describe this source (differences listed at the end).

## Core layout

Fully **interrupt-driven**: every useful action runs inside the
*high-priority* interrupt service routine (vector 0x0008), which serves both
the Timer1 overflow (the measurement cycle) and the EUSART1 receive
interrupt (commands). Interrupt nesting is disabled (only high-priority
sources are enabled), so exactly one ISR runs at a time — a timer ISR blocks
command processing and vice versa, which the protocol relies on. The
low-priority vector is unused; `main()` after initialization is an idle NOP
loop.

## Pin map

Port-pin names as silkscreened on the PICDEM Explorer headers.

| Pin | Function |
|---|---|
| RD7 | Integrator reset, channel 1 (1 = reset asserted, 0 = integrate) |
| RD1 | Integrator reset, channel 2 (**inverted**: 0 = reset asserted, 1 = integrate) |
| RD5 | Event mode line (0 = ME, 1 = SE) |
| RD6 | Gain pin — switches the (single) integration capacitor of each board |
| RD0 | LED, driven high while a measurement runs |
| RD4 | Debug integration gate, channel 1 — defined but **not driven** in v2.2 (reserved) |
| RC6 / RC7 | EUSART1 TX / RX (asynchronous, 115200 8N1) |
| RF5 (AN10) | ADC input, channel 1 |
| RF1 (AN6) | ADC input, channel 2 |
| RC0 (T1CKI) | Timer1 external clock: the measurement **timebase crystal** (32.768 kHz) |
| VREF+ | External ADC positive reference (span 0…5 V) |

The two reset outputs are complementary because the two integrator boards
switch their reset with opposite polarity: in the integration phase RD7 is
low and RD1 high; in the reset phase (and after `A`, and at power-up) RD7 is
high and RD1 low. Both channels are therefore reset and released
simultaneously.

## ADC

- 12-bit SAR, result **right-justified** (`ADFM = 1`): `ADRESL` holds bits
  7…0, `ADRESH` bits 11…8.
- Channel 1 = **AN10** (RF5), channel 2 = **AN6** (RF1) — two inputs far
  apart on the board, chosen to reduce crosstalk. The channels are sampled
  **sequentially** (one ADC, mux switched between conversions).
- References: `VCFG0 = 1` → VREF+ pin (the board's 0…5 V reference),
  `VCFG1 = 0` → AVSS. The application records the span as 0…5 V.
- Conversion clock `Fosc/32` → TAD = 0.8 µs at Fosc = 40 MHz; a full 12-bit
  conversion takes ≈ 10.4–11.2 µs (the conversion start aligns to the A/D
  clock, giving up to 1 TAD of start jitter).
- **Manual acquisition** (`ACQT = 0`): the hold capacitor follows the input
  until `GO` disconnects it and starts the conversion. The firmware leaves
  ≈ 2.7 µs between a mux change and the next `GO` to cover the datasheet's
  minimum acquisition time.

## Serial protocol

EUSART1, asynchronous, **115200 8N1** (`SPBRG = 86` at 40 MHz — the highest
rate this core reaches). Commands are **single bytes**; `R`, `N`, `C`, `M`
take exactly one parameter byte, `T` takes five after a handshake. Replies:
`D` = done, `F` = fault/rejected, `W` = waiting for the timer bytes, `V` =
verify reply.

| Command | Meaning | Reply |
|---|---|---|
| `S` | Start measurement: LCD "PROCESSING", LED on, reset reload preloaded, timer started. First overflow begins cycle 0. | `D`, then the binary stream |
| `E` | Stop: timer off, measure flag cleared, reset lines released (safe state), LED off, LCD "READY". Works at any time. | `D` |
| `A` | Arm: assert the reset on both integrators (RD7 high, RD1 low) to clear residual charge. LCD "ARMED". Refused while measuring. | `D` |
| `R S` / `R M` | Event mode line high (SE) / low (ME). Refused while measuring. | `D` |
| `N Y` / `N N` | Two-channel / single-channel configuration. Refused while measuring. | `D` |
| `C Y` / `C N` | Gain pin high / low (switch the capacitor bank). Refused while measuring. | `D` |
| `T` | Timer configuration: replies `W`, then reads **five raw bytes** in order `timerU_int, timerH_int, timerL_int, timerH_reset, timerL_reset` and programs them. Accepted even mid-run (the in-flight cycle is discarded). | `W` … `D` |
| `V` | Communication check (origin: Linux adapters that silently map a configured 9600 bps to 115200). Accepted even mid-run (in-flight cycle discarded). | `V` |
| `M D` / `M P` | Debug mode (transmit start+stop of every cycle) / production mode (transmit only the difference). Refused while measuring. | `D` |
| anything else, or any of the above while measuring (except `E`, `T`, `V`) | — | `F` (and the in-flight cycle is discarded) |

**Receive pacing.** The EUSART RX FIFO is two bytes deep and the timer
config runs with interrupts disabled inside the ISR, so the host must not
fire all five configuration bytes blindly: it sends the `T`, waits for `W`,
then sends the five bytes in small bursts (two bytes, ~200 µs pause, two
bytes, ~200 µs pause, one byte). A lost byte inside `T` blocks the firmware
in `wait_rx_full` until the next byte arrives — a stalled configuration
needs a reset, which is why every step is acknowledged.

### Data stream

After `S`, one frame per completed integration cycle, all values **16-bit
little-endian** (LSB first):

| Mode | Channels | Frame | Size |
|---|---|---|---|
| debug | 2 | `ch1_start, ch1_stop, ch2_start, ch2_stop` | 8 B |
| debug | 1 | `ch2_start, ch2_stop` | 4 B |
| production | 2 | `ch1 = stop−start, ch2 = stop−start` | 4 B |
| production | 1 | `ch2 = stop−start` | 2 B |

> **Single-channel wiring — by design, take care.** When configured for one
> channel (`N N`), the firmware converts and transmits only its **AN6/RF1**
> input ("ch2" internally), and the application driver maps that stream
> onto its CH1 columns. In a single-detector bench setup the detector
> **must** be connected to RF1: on RF5 (the two-channel CH1 input) it would
> record nothing, and nothing in the data would reveal the miswiring.

Frames are transmitted from inside the end-of-integration ISR (interrupts
are disabled, so the RX side cannot interleave). The transmission happens
**inside** the timed cycle and its duration is part of the reset phase —
see the timing model.

### Overflow and discard

A cycle is discarded per channel when

- the **stop** sample is full scale `0x0FFF` (integrator overflow — the
  start sample cannot overflow because the reset precedes it), or
- the difference `stop − start` is **negative**, or
- a serial command hit the measurement (both channels discarded).

Discarded cycles are signalled **only in production mode**, as `0x8000`
(32768 — not reachable by the 12-bit ADC). In debug mode the raw start/stop
values are sent unchanged; the host detects the conditions itself
(`stop == 0x0FFF`, `stop < start`).

### Validated limits

The firmware performs **no parameter validation**: `T` programs whatever
registers it receives. All limits are enforced by the host driver
(`pic_serial_v1`), which must mirror the timing model below:

| Parameter | Host-enforced range | Why |
|---|---|---|
| integration time | 1 µs … 512 s | 2²⁴ timer ticks at 32768 Hz; the practical minimum is one tick ≈ 30.5 µs |
| reset time | 1 µs … 2 s | **by design**: the reset phase spans a single timer period (no software prescaler on this phase); see the timing model below |
| period | derived = int + reset | — |
| ramp sampling | not supported | the firmware has no trigger output |

## Timing model

**Clock domains.** Timer1 counts rising edges of the external **32.768 kHz
crystal** on T1CKI, asynchronous to the CPU (`T1SYNC = 1`, prescaler 1:1) —
the measurement timebase is completely independent of the 40 MHz CPU clock,
which only sets the ISR overheads (1 instruction cycle = 100 ns at
Fosc/4 = 10 MIPS). One timer tick = 1/32768 s ≈ **30.52 µs**; a full 16-bit
period (reload `0x0000`) = 65536 ticks ≈ **2.000 s**.

**The measurement cycle** (all steps inside the high-priority ISR):

```
'S' → preload reset reload, start timer
    → overflow: START phase
        release reset (integration begins),
        sample ch1 start (conversion started ≈0.2 µs after release),
        sample ch2 start (≈13 µs after ch1),
        load integration reload + TMR1U prescaler,
    → integration overflows (see prescaler below): STOP phase
        sample ch1 stop (≈0.7 µs after the overflow),
        sample ch2 stop (≈12.5 µs after ch1),
        assert reset on both boards,
        overflow / negative-difference check, compute difference,
        transmit the frame (inside the ISR),
        10 µs guard, load reset reload,
    → overflow: next START phase …
```

**Integration duration.** `TMR1U` is an 8-bit RAM cell acting as a software
prescaler: at each overflow during integration it is incremented, and only
when it wraps (`0xFF → 0x00`) is the stop phase executed. Timer1 is **not**
reloaded between overflows, so the integration time is

```
(65536 − int_reload) + (u − 1) · 65536   timer ticks,   u = 256 − timerU_int
```

i.e. one *partial* period (the reload) plus `u − 1` *full* 2 s periods.
`TMR1U` extends the reachable integration from 2 s to 512 s.

**Reset duration.** The counted part is `65536 − reset_reload` ticks,
measured from `SetResetTimer` — which deliberately runs **after** the data
transmission. This works around a hardware interaction observed between the
EUSART and Timer1 (spurious timer-register increments during and right after
transmission): the reset phase absorbs the entire TX, so at worst the
*periodicity* is affected while the integration time stays exact. The actual
reset-low time is therefore

```
stop-phase sampling (≈25 µs) + TX time + 10 µs guard + counted part
```

with the TX time bounded by the frame size (≈ 694 µs for 8 debug bytes at
115200). The host folds the transmission into a per-mode tick constant
("TX delay", 7/13/13/24 ticks for production-single / production-double /
debug-single / debug-double) and compensates it in the reload it sends.

**Sampling instants.** Within each phase the two channels are sampled
sequentially: channel 1's conversion starts ≈ 0.2 µs (start phase) /
≈ 0.7 µs (stop phase) after the phase boundary, channel 2's ≈ 12.5–13 µs
later (mux change, ≈ 2.7 µs acquisition guard, fixed delays). Each channel's
own start→stop difference is unbiased; the two channels are mutually skewed
by ≈ 13 µs — worth remembering when cross-channel covariances are
interpreted at very short integration times.

**Jitter.** Timer1 is asynchronous and free-running; the interrupt latency
of the CPU is 1 instruction cycle (100 ns) from the NOP-sled idle loop and
2 cycles (200 ns) if the interrupt lands on the loop's `GOTO`.

## LCD and LED

The 2×16 LCD is driven by the PICDEM HPC Explorer demo library
(`lcd.asm`, Microchip 2007) through a program-memory string table:
`Microchip PICDEM / PIC18 Explorer` at boot, then `READY`, `PROCESSING`
(measuring) and `ARMED` (after `A`). The RD0 LED is on while measuring.

## Building and flashing

Classic **MPLAB (not MPLAB X)** with the MPASM assembler:

- sources: `final.asm` (main), `lcd.asm` (LCD library)
- includes/links: `P18F8723.INC`, `188723_g.lkr`
- output format `INHX32`
- configuration bits: `OSC = HSPLL` (10 MHz crystal × 4 PLL = 40 MHz),
  `WDT = OFF`, `LVP = OFF`, `PWRT = OFF`, `LPT1OSC = OFF`
- programmer: PICkit3

## Version history

- **v2.2** (this source): per the ChangeLog, the event-mode command `R S` /
  `R M` (the RD5 line) was added. Compared with the revision reviewed in
  `code_report.pdf`, this source additionally differs in: the `A` / `N` /
  `C` commands (arm, channel count, gain), the reset lines moved to RD7
  (ch1, active high) and RD1 (ch2, active low) from RD0/RD1/RD2, the ADC
  inputs moved to AN10 (RF5) / AN6 (RF1) from AN5 (RF0) / AN9 (RF4),
  single-channel transmission, and the removal of the 2 s leakage-delay at
  the first cycle. The integration-gate debug outputs RD4/RD6 of the old
  revision are gone (RD4 reserved, RD6 now the gain pin).
