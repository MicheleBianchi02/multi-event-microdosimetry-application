# PIC18F8723 firmware (v2.2) — internals and the `pic_serial_v1` driver

This document describes **how the PIC measurement chain works internally** —
the interrupt-driven cycle, the 32.768 kHz timebase and the serial protocol
mechanics — and then, in the same document, **the application driver**
(`pic_serial_v1`, `hardware/pic/pic_daq.py`) that commands it: its timer
quantization math, its transmission-delay compensation and its frame
parser.

- Firmware source: [v2_2/final.asm](v2_2/final.asm) (main),
  [v2_2/lcd.asm](v2_2/lcd.asm) (Microchip PICDEM LCD library)
- Protocol/pin-map quick reference: [README.md](README.md)
- Historical background: `code_report.pdf` (a detailed review of an *earlier*
  revision — the jitter analysis and the EUSART↔Timer1 investigation it
  contains still apply; its pin map and API do not)
- Driver source: `hardware/pic/pic_daq.py`; the quantization constants are
  duplicated in `core/constants.py` (`OSC_FREQ`, `TMREG`, `UREG`, the TX
  delays) for the session model

---

## Part 1 — The firmware (v2.2)

### 1.1 Platform and structure

- **PIC18F8723** (80-pin, 12-bit ADC) on the **PICDEM PIC18 Explorer**
  board; MPASM assembly (`LIST P=18F8723, F=INHX32`), built with the
  classic **MPLAB** (pre-X).
- Clock: `OSC = HSPLL` — 10 MHz crystal × 4 PLL = **40 MHz**; one
  instruction cycle = Fosc/4 = **100 ns** (10 MIPS). Watchdog, LVP,
  power-up timer and Timer1 low-power oscillator disabled.
- **Fully interrupt-driven**: the entire application lives in the
  *high-priority* ISR (vector 0x0008), entered by either the **Timer1
  overflow** (the measurement cycle) or the **EUSART1 receive** interrupt
  (commands). Interrupt priority is enabled but only high-priority sources
  are used, so **one ISR runs at a time** — a timer ISR cannot be
  interrupted by a received byte and vice versa; the protocol's discard
  rules are built on this. The low-priority vector is a bare `RETFIE`.
- `main()` after initialization (`clean`, `init_conf`, `init_port`,
  `init_adc`, `init_lcd`, `init_usb`, `init_timer`) is an **idle NOP sled +
  GOTO**: the long NOP sequence keeps the interrupt latency at one
  instruction cycle (100 ns) except when the interrupt lands on the 2-cycle
  `GOTO` (200 ns) — a deliberate jitter reduction from the code report.
- All state lives in one `flags` byte (`UDATA_ACS`): `measure_flag`
  (acquiring), `type_measure` (ISR phase: 0 = start-of-integration, 1 =
  stop), `discard_measure_ch1/ch2`, `mode_flag` (debug/production),
  `double_ch_config` (two/single channel).

### 1.2 Timebase: Timer1 + the 32.768 kHz crystal

The measurement timing does **not** come from the 40 MHz CPU clock. Timer1
is clocked by an external **32.768 kHz watch crystal** on T1CKI
(`TMR1CS = 1`, `T1SYNC = 1` — asynchronous, prescaler 1:1, 16-bit read,
`TMR1ON` gates counting and is only set by `S`):

- one tick = 1/32768 s ≈ **30.52 µs**; a full 16-bit period (reload
  0x0000) = 65536 ticks ≈ **2.000 s**;
- there is no hardware reload: the ISR writes `TMR1H`/`TMR1L` explicitly
  (H before L, with `RD16 = 1` for the atomic 16-bit write);
- `TMR1U` is an 8-bit RAM cell acting as a **software prescaler**: at each
  overflow during integration it is incremented (`INCFSZ`), and only when
  it wraps past 0xFF is the stop phase executed. With `timerU_int` loaded
  at the start of every integration, the cycle ends at the
  `u = 256 − timerU_int`-th overflow. This extends the reachable
  integration time from 2 s to **512 s**.

Because the crystal is asynchronous, an interrupt can arrive during any
instruction; together with the idle-loop shape this gives ≈ 100–200 ns of
latency jitter on the phase boundaries — negligible against the 30.5 µs
tick.

### 1.3 The measurement cycle (the timer ISR)

After `S`: LED on, LCD "PROCESSING", `measure_flag` set, `FirstMeasure`
selects channel 1 on the ADC mux, preloads the **reset** reload and arms
`type_measure = start`; then `TMR1ON` starts the counting. From then on
the cycle is:

**START phase** (entered on the overflow that ends the reset):

1. Release reset (RD7 low = ch1 integrates; RD1 high = ch2 integrates —
   the two boards switch reset with opposite polarity).
2. **Sample ch1 start**: `GO` starts the conversion ≈ 0.2 µs after release
   (manual acquisition: the hold capacitor disconnects on `GO`).
3. Fixed delays (5 + 5 + 1 µs) and `SetIntTimer` (1 µs) — ≈ **12 µs** after
   release the **integration reload** and `TMR1U` are written.
4. Switch the mux to ch2, save the ch1 result, wait ≈ 2.7 µs acquisition
   guard, **sample ch2 start** (conversion ≈ 11 µs), switch back to ch1.
5. `type_measure = stop`.

**STOP phase** (entered on the overflow that ends the integration):

1. `INCFSZ TMR1U` — if it did **not** wrap, the ISR exits immediately
   (this was one of the `u − 1` extra full 2 s periods; **Timer1 is not
   reloaded between them**: sub-period 1 runs reload→overflow, sub-periods
   2…u each run a full 65536 ticks).
2. On the wrapping overflow (after 3 NOPs of phase-compensation): **sample
   ch1 stop** (conversion started ≈ 0.7 µs after the overflow), then ch2
   stop (≈ 12.5 µs later, same sequential pattern).
3. Assert reset on both boards (RD7 high, RD1 low).
4. **Overflow check**: stop sample = 0x0FFF → discard flag for that
   channel.
5. **Difference** `stop − start` per channel (16-bit `SUBWF`/`SUBWFB`);
   negative result (N flag) → discard flag.
6. **Transmit** (`SendData`, see §1.5) — inside the ISR, so RX cannot
   interleave; discarded channels are replaced by `0x8000` in production
   mode.
7. 10 µs guard, then `SetResetTimer` loads the **reset** reload. The next
   overflow starts the next cycle.

Discard flags are cleared after the send; a cycle hit by a serial command
is discarded for both channels.

**Why the reload comes after the transmission** (the code report's key
finding): the EUSART corrupts the Timer1 registers with spurious increments
during and shortly after a transmission. Deferring `SetResetTimer` until
the TX is fully done (plus a guard delay) pushes the whole interference
window into the **reset phase** — at worst the *period* is affected, never
the integration time.

### 1.4 Sampling instants and the TX delay

Per cycle (relative to the phase boundaries):

| Sample | Instant |
|---|---|
| ch1 start | ≈ 0.2 µs after reset release (acquisition ends ≈ 1.8 µs) |
| ch2 start | conversion begins ≈ 13–14.4 µs after ch1 |
| ch1 stop | ≈ 0.7 µs after the integration overflow |
| ch2 stop | ≈ 12.5 µs after ch1 stop |

Each channel's own start→stop difference is unbiased (the sequence is
identical in both phases); the two channels are mutually skewed by ≈ 13 µs.

The stop-phase ISR additionally spends the **transmission time** before
reloading the timer: 8 bytes (debug, 2 ch) at 115200 8N1 ≈ 694 µs,
4 bytes ≈ 347 µs, 2 bytes ≈ 174 µs. The reset phase therefore never takes
less than the TX time, and the host must compensate for it — this is the
"TX delay" of the driver's timing model (§2.4).

**ADC characteristics**: 12-bit, right-justified; ch1 = AN10 (RF5), ch2 =
AN6 (RF1) sampled sequentially through the mux; external VREF+ (0…5 V
span); TAD = Fosc/32 = 0.8 µs; conversion ≈ 10.4–11.2 µs (1 TAD of
start-alignment jitter); manual acquisition with a ≈ 2.7 µs guard between a
mux change and the next `GO`.

### 1.5 Serial protocol mechanics

115200 8N1 on EUSART1 (the fastest this core reaches; `SPBRG = 86`). The
command set and the byte formats are tabulated in the [README](README.md);
the mechanics worth knowing:

- **Two-byte RX FIFO.** The receive interrupt reads `RCREG1` immediately,
  but while a timer ISR runs, received bytes can only pile up two deep.
  Hence the `T` handshake: the host sends `T`, the firmware answers `W`
  *from inside the RX ISR* and then blocks reading the five configuration
  bytes one by one (`wait_rx_full`); the host paces them in bursts of two
  separated by ≈ 200 µs.
- **Commands vs the measurement.** `E` (stop) works at any time. `T` and
  `V` are *processed* mid-run but discard the in-flight cycle (their
  arrival perturbs the timing). Every other command (`S`, `A`, `R`, `N`,
  `C`, `M`) answers `F` and discards the in-flight cycle while
  `measure_flag` is set.
- **Frames** (little-endian, sent from inside the stop ISR): debug 2 ch =
  `ch1_start, ch1_stop, ch2_start, ch2_stop` (8 B); debug 1 ch = ch2's
  start/stop (4 B); production 2 ch = the two differences (4 B); production
  1 ch = one difference (2 B). In single-channel mode the firmware converts
  and sends only the **AN6/RF1** input — the driver maps it onto the app's
  CH1 (by design; see the wiring caution in §2.5).
- **Discard signalling**: production mode sends `0x8000` (32768) for a
  discarded channel; debug mode sends the **raw** values (full-scale stop =
  overflow, `stop < start` = negative) and leaves the detection to the
  host.
- **LCD/LED**: `READY` / `PROCESSING` / `ARMED` states through the PICDEM
  LCD library; RD0 LED on while measuring.

### 1.6 Electrical interface summary

| Signal | Pin | Polarity / notes |
|---|---|---|
| Reset ch1 | RD7 | 1 = reset asserted, 0 = integrate |
| Reset ch2 | RD1 | **0 = reset asserted, 1 = integrate** (inverse of ch1) |
| Event mode | RD5 | 0 = ME, 1 = SE |
| Gain | RD6 | single capacitor-bank switch per board (`C Y` = high) |
| Measurement LED | RD0 | high while measuring |
| ADC ch1 | RF5 (AN10) | 0…5 V via external VREF+ |
| ADC ch2 | RF1 (AN6) | idem; the single-channel input |
| Timebase | RC0 (T1CKI) | external 32.768 kHz crystal |
| Serial | RC6/RC7 | 115200 8N1 (board's RS-232 port) |
| Debug gate | RD4 | defined, not driven in v2.2 (reserved) |

---

## Part 2 — The application driver (`pic_serial_v1`)

`hardware/pic/pic_daq.py` — class `PICSerialDAQ(IDataAcquisition,
ITimingController, IGainController)`; like the Pico, one board can hold all
three roles. The shipped component (`hardware_library/device_pic.json`)
declares timing + gain (1 pin) + acquisition (2 channels, 12 bit, 0…5 V).

### 2.1 Firmware constants mirrored by the driver

```python
OSC_FREQ = 32768        # the Timer1 crystal, in Hz — one tick = 30.52 µs
TMREG   = 65536         # 16-bit timer modulus
UREG    = 256           # TMR1U software-prescaler modulus
# transmission time folded into the reset phase, in TIMER TICKS:
TX_DELAY_PRO_DOUBLE   = 13    #  4 bytes ≈ 347 µs
TX_DELAY_DEBUG_DOUBLE = 24    #  8 bytes ≈ 694 µs   (the app always uses this
TX_DELAY_PRO_SINGLE   =  7    #  2 bytes ≈ 174 µs    or DEBUG_SINGLE)
TX_DELAY_DEBUG_SINGLE = 13    #  4 bytes ≈ 347 µs
```

The app always acquires in debug mode, so the applied delay is
`TX_DELAY_DEBUG_DOUBLE` (two channels) or `TX_DELAY_DEBUG_SINGLE` (one).

### 2.2 Layer-1 limits and their fine print

`HARDWARE_LIMITS`: integration and reset each `1 µs … 512 s` (step 1 µs),
period derived (int + reset), **ramp sampling not supported**
(`configure_ramp` returns False; the manager therefore runs a PIC-timed
setup in `internal` mode and, on split hardware, warns that no window
markers can be produced).

Fine print (visible in the applied-values preview):

- **Integration**: the quantization search clamps to ≥ 1 tick, so requests
  below ≈ 30.5 µs yield one tick; the 512 s ceiling is 2²⁴ ticks.
- **Reset — maximum 2 s by design**: the reset phase spans a **single
  timer period** (there is deliberately no software prescaler on this
  phase). A request above 65536 ticks is produced as one full period
  (reload 0), i.e. ≈ 2.0007 s including the TX delay. The driver's
  hardcoded `HARDWARE_LIMITS` and the shipped component fragment currently
  still declare the wider 512 s envelope inherited from the integration
  path — a pending tightening; the applied-value preview shows the
  truncation.
- **Minimum reset**: the reload is clamped to ≥ 1 tick after subtracting
  the TX delay, so the shortest reset is `TX_DELAY + 1` ticks (≈ 763 µs in
  debug two-channel mode).

### 2.3 Protocol implementation

Every command method follows the same pattern: validate locally →
`connect()` if needed → under the port lock: `reset_input_buffer()`, write
the byte(s), then poll for the expected reply (`D`, or `W`/`V`) with a
1.5 s deadline. One `threading.Lock` serializes all port access.

| Driver method | Bytes on the wire |
|---|---|
| `set_event_mode(se)` | `R` + `S`/`M` → `D` |
| `set_channels(double)` | `N` + `Y`/`N` → `D`; on success the channel count is remembered — it selects the TX delay for the quantization math |
| `set_gpio_mask([b])` | `C` + `Y`/`N` → `D`; exactly one boolean expected (`GAIN_MAX_PINS = 1`) |
| `set_mode(debug)` | `M` + `D`/`P` → `D` (the app always sends `D`) |
| `arm()` | `A` → `D` (asserts reset on both integrators) |
| `configure_noramp(int, reset)` | `T` → wait `W` → five raw bytes `timerU_int, timerH_int, timerL_int, timerH_reset, timerL_reset` paced as two bursts with 200 µs sleeps (`U,H`, `L,H_rst`, `L`) → `D` |
| `start_measurement(...)` | `S` → `D`, then frames |
| `stop_measurement()` | `E` → `D` |
| `configure_ramp(...)` | refused (logged warning, returns False) |

### 2.4 The quantization math (`_quantize_timing`)

This is the heart of the driver: convert requested µs into the five raw
timer bytes, and report the *actual* times the firmware will produce.

**Integration.** A search over the software prescaler minimizes the
quantization error:

```
for u_bin = 256 … 1:                       # number of sub-periods
    tmr_bin = round(int_time_s · 32768 / u_bin)      # ticks per sub-period
    candidate time = u_bin · tmr_bin / 32768
keep the smallest error (ties → smallest u_bin, tmr_bin ≤ 65536)
registers: timerU_int = 256 − u_bin
           timerH/L_int = 65536 − tmr_bin        (big end first on the wire)
```

For any requested time ≤ 2 s this lands on `u_bin = 1` (a single
round-to-nearest is optimal), so the practical integration quantization is
one **30.52 µs tick**.

> **Model vs firmware — known divergence above 2 s.** The driver's model is
> `u_bin · tmr_bin` ticks, i.e. *every* sub-period the length of the first
> one. The firmware reloads the timer only once per integration (§1.3):
> sub-period 1 runs reload→overflow, sub-periods 2…u each run a **full**
> 65536 ticks. The two agree exactly when `u_bin = 1` (any T ≤ 2 s) and
> when the reload is 0 (`tmr_bin = 65536`, i.e. T a multiple of 2 s). For
> an integration time strictly between 2 s and 512 s that is not a multiple
> of 2 s, the firmware produces a **longer** integration than the preview
> reports (by `(u−1)·(65536 − tmr_bin)` ticks). Typical microdosimetry
> integration times (µs–ms) sit deep inside the safe `u_bin = 1` region;
> the full analysis, verified error table and fix options are in
> [PIC_BUG.md](PIC_BUG.md).

**Reset.** The counted reset must fit one timer period, and the TX time
already spent inside the ISR is compensated exactly:

```
ticks = round(reset_time_s · 32768) − tx_delay        # reload part, ≥ 1
actual reset = (tx_delay + ticks) / 32768 = round(reset·32768)/32768
registers: timerH/L_reset = 65536 − ticks
```

i.e. the TX delay is subtracted from the reload and added back into the
reported actual value: the reset the user asks for is the reset the boards
see (quantized to one tick), regardless of mode or channel count. Requests
beyond 65536 ticks fall back to reload 0 (see §2.2).

**`calculate_actual_timing`** is the pure preview: it re-runs this math
(no serial traffic) with the TX delay selected by *debug mode* (always) and
the **active channel count** injected by the `HardwareManager` from the
config — the same count `set_channels` put the device in. The reported
period is int + reset and `sample_frequency_hz = 1/period`.

### 2.5 Concurrency and the reader thread

`start_measurement` refuses a `ramp_sink` (the legacy protocol has no
rample-sample transport), sends `S`, and spawns a daemon reader thread.
`_read_loop` reads the port byte-by-byte into a frame buffer and dispatches
on the session's mode/channel state — the firmware's frame size is
mode-dependent, so the framing is too:

| Mode | Channels | Frame size | Parsed as |
|---|---|---|---|
| debug | 2 | 8 B | `((s1, e1), (s2, e2))` |
| debug | 1 | 4 B | `((s1, e1), (missing, missing))` |
| production | 2 | 4 B | `(v1, v2)` |
| production | 1 | 2 B | `(v1, missing)` |

`missing = overflow_sentinel(12) = 4096` — the app-side "no channel here"
marker (one past the 12-bit full scale, mapped to NaN by the conversion
layer). Complete frames go to `data_model.append_data(...)`; the buffer
clears per frame. A reader exception sets `_error` and ends the thread.

`stop_measurement` joins the reader (2 s), then sends `E` and waits for the
`D` acknowledgement.

> **Single-channel wiring — by design, take care.** The firmware's
> single-channel mode (`N N`) converts and transmits only its **AN6/RF1**
> input ("ch2" internally, §1.5), and the driver maps that stream onto the
> application's CH1 columns. This is intentional — but the user must wire
> accordingly: in a one-detector setup the detector belongs on **RF1**. On
> RF5 (the two-channel CH1 input) it would record nothing, and nothing in
> the data would reveal the miswiring.

### 2.6 What the driver does not implement

- `start_timing()` / `stop_timing()`: the interface defaults (no-ops)
  apply. A configuration where the PIC holds **only** the timing role
  (e.g. with the virtual acquisition) therefore cannot actually start its
  timer engine — use the PIC with its acquisition role, as the shipped
  component does. (The Pico implements these methods; the PIC does not.)
- Ramp sampling and the ramp-timing mirror (`calculate_ramp_timing` absent
  → the manager's ramp preview returns `None` for this device).
- Oversampling and input-range selection: `get_adc_settings` reports the
  fixed facts (`bits: 12`, `vref: 5.0`).

### 2.7 End-to-end sequence for a PIC measurement

```
connect()                  port open (115200 8N1)                (no handshake:
set_mode(True)             M D → D                                the PIC has no
set_channels(True/False)   N Y/N → D        ( selects the TX delay )  identity
set_gpio_mask([b])         C Y/N → D                              command)
set_input_range(0, 5)      –                (fixed range)
configure_noramp(int, rst) T → W → 5 timer bytes → D
arm()                      A → D            (integrator reset; ≥ 3 s before S)
start_measurement(...)     S → D, then frames per integration cycle
stop_measurement()         E → D
```

### 2.8 Legacy

`services/pic_serial_daq.py` (with `services/daq_interface.py`) is the
pre-hardware-abstraction implementation of the same protocol — superseded by
this driver and no longer imported anywhere (BUG.md B17 proposes deletion).
