# NI USB-6451 — hardware principle and the `ni_daqmx_v1` driver

The USB-6451 is the **acquisition-only** instrument of the ATOMIQA chain:
it has no firmware of its own in this project (it is programmed through
NI-DAQmx), and it always plays the `acquisition` role while a
microcontroller (typically the Pico) plays the `timing` role — the *split
hardware* setup. Every conversion happens on a rising edge of the
**CONV_START** signal that the timing device emits, which makes the sample
grid a hardware guarantee rather than a software promise.

- Manuals in this folder: `USB-6451 - NI_specs.pdf` (specifications),
  `USB-6451 - NI_manual.pdf` (user manual), `USB-6451 - NI.pdf` (product
  page); the `nidaqmx-python/` subfolder is the vendored Python API
  documentation.
- Driver source: `hardware/ni/ni_daqmx.py` (class `NIDAQmxAcquisition`,
  driver code `ni_daqmx_v1`); driver tests: `tests/test_ni_driver.py`
  (against a fake `nidaqmx` module — live validation on the real card is
  still pending, see the root README's open points).

---

## Part 1 — What kind of device the USB-6451 is

### 1.1 Analog input

| Property | Value (from the specifications) |
|---|---|
| ADCs | **8**, one per differential pair — true simultaneous sampling |
| Resolution | **20 bits**, no missing codes |
| Sample rate | **1 MS/s per channel**, all 8 differential channels simultaneously (or up to 8 single-ended) |
| Single-ended scan caveat | Pairs of SE channels **share one ADC** (AI0/AI8, AI1/AI9, …): sampling two SE channels of the same pair drops the rate to 500 kS/s with bank scanning |
| Input ranges | **±0.2 V, ±2.5 V, ±5 V, ±10 V** (software-selectable per task via the channel's min/max) |
| Coupling / impedance / bias | DC; > 10 GΩ ‖ 35 pF per input; ± 10 pA typical bias |
| Bandwidth | 1.3 MHz (±2.5/5/10 V), 360 kHz (±0.2 V) |
| Timing | 10 ns resolution, 50 ppm of sample rate |
| Overrange behaviour | readings **clamp** at ± full scale (no flag, no sentinel) |

The bench uses **four different ADCs** so that any channel mix converts
simultaneously at 1 MS/s each (never the 500 kS/s scan case): ch1
differential = AI0(+)/AI8(−) → ADC 0, ch2 differential = AI1(+)/AI9(−) →
ADC 1, ch1 single-ended = AI2 → ADC 2, ch2 single-ended = AI3 → ADC 3.

### 1.2 Clocking — the property the whole design hinges on

The AI sample clock can be the device's internal timer **or an external
digital signal routed through a PFI line**. In external-clock mode the ADC
performs one conversion per selected edge of that signal: the sampling
grid is *exactly* the timing device's pulse train, with the 10 ns
resolution / 50 ppm accuracy of the digitizer only stamping the edges.

ATOMIQA exploits this: the Pico's CONV_START output (edges at the
integration start and end, or the full ramp-sampling train at spacing dt —
never any edge during reset) drives **PFI0** (screw terminal 25 on the
AO/DIO connector). Therefore

> **N = T_int/dt + 1 samples per integration window** — the same number the
> Pico firmware programs into its generators and the ramp-storage window
> extractor chunks on — is a hardware guarantee, not a software estimate.

While the task is *not* started, the 6451 ignores clock edges entirely —
which is what makes the driver's arm-before-start ordering meaningful
(§2.5).

### 1.3 Digital side and the logic-family pitfall

All digital functions (port I/O, counters, **and trigger/clock inputs**
like PFI0) share one configurable **logic family**: 1.8 V, 2.5 V,
**3.3 V (LVTTL)** or **5 V (TTL/5 V CMOS)** — configured per port through
the DAQmx `Dig.PortLogicFamily` property, **defaulting to 5 V**.

This matters because the Pico's CONV_START is a 3.3 V signal: in the 5 V
family the guaranteed input-high threshold is **3.66 V** — above what the
Pico can deliver, so the clock would be *electrically out of spec* (the
guaranteed-VIH threshold, not the typical behaviour, is the problem). The
driver therefore switches the PFI0 port's family to **3.3 V** (guaranteed
V_IH 2.0 V) at every connect, before anything is armed.

Power-up state of the digital lines: high-impedance inputs with weak
pull-downs.

### 1.4 Data flow and buffering

A DAQmx task in continuous mode fills an on-board/PC buffer as conversions
arrive; host reads (`read_many_sample`) pull from that buffer. Calling
`task.stop()` **discards whatever the buffer still holds** — the reason the
driver drains the buffer explicitly before stopping (§2.6). A read with a
timeout that expires while the buffer holds fewer samples than requested
raises DAQmx error **−200284** ("samples requested were not available
before the timeout expired") — under an external clock this is the normal
"no edges yet" state, not a failure.

---

## Part 2 — The application driver (`ni_daqmx_v1`)

### 2.1 Identity and imports

- Registry code `ni_daqmx_v1`; capability **acquisition only**; connection
  type `daqmx` — the config names the card as NI-DAQmx sees it
  (`"connection": {"type": "daqmx", "device": "NI6451"}`), there is no
  serial port.
- The `nidaqmx` Python package (on top of the NI-DAQmx runtime) is
  imported **lazily** inside `connect()`/`start_measurement()`: machines
  without it still load configurations and run the registry/loader; only
  actually using the card fails with a clear message. The test suite runs
  against a hand-built fake `nidaqmx` module.

### 2.2 Layer-1 constants (hardcoded hardware facts)

```python
ADC_BITS = 20
INPUT_RANGES = (±10, ±5, ±2.5, ±0.2 V)     # default selected: ±5 V
MAX_CHANNELS = 2            # exposed by this app (physical: 8 diff / 16 SE)
MAX_RATE_HZ  = 1_000_000    # per channel — the manager gates ramp dt ≥ 1 µs
CHANNEL_TERMINATIONS = ("differential", "rse")
CHANNEL_TERMINALS = {                       # the fixed bench wiring:
    ("ch1", "differential"): "ai0",         #   AI0 (+) / AI8 (−)
    ("ch2", "differential"): "ai1",         #   AI1 (+) / AI9 (−)
    ("ch1", "rse"): "ai2",
    ("ch2", "rse"): "ai3",
}                                           # four different ADCs → simultaneous
CONV_START_TERMINAL = "PFI0", rising edge, logic family THREE_POINT_THREE_V
```

The config selects per channel **only the termination** (`differential` or
`rse`); the physical terminals behind each choice are fixed hardware facts
hardcoded here — change the cabling, change this table. The registry
descriptor publishes `MAX_RATE_HZ` so `Apply Configuration` rejects a ramp
dt whose 1/dt would exceed 1 MS/s before anything reaches the hardware.

### 2.3 `connect()` — enumeration, self-test, logic family

1. Enumerate `System.local().devices`; the configured device name must
   exist (a missing card lists the present names in the log).
2. `self_test_device()` — the card's own health check.
3. Set the **PFI0 port logic family to 3.3 V** through the physical
   channel property (§1.3). Failure of any step is a failed connect.

### 2.4 Channel plan and input range

- `configure_channels([(ch_id, termination), …])` — called by the
  `HardwareManager` at config-load time with the config's active channels
  (missing termination defaults to `differential`). Each pair is mapped
  through the fixed terminal table; unknown pairs or a terminal used twice
  are rejected. The plan is stored for `start_measurement`.
- `set_input_range(min, max)` accepts **only** the four exact USB-6451
  ranges (compared as voltage pairs) and defaults to ±5 V; anything else is
  rejected rather than approximated — a mislabeled range would corrupt
  every charge computation downstream.
- `get_adc_settings()` reports bits (20), the currently applied range and
  `supports_oversampling: False`; the `HardwareManager` overlays the
  config's authoritative values.

### 2.5 `start_measurement()` — task construction and the arming contract

Preconditions: a `ramp_sink` **must** be provided (the card has no
internal-clock mode in this app — without the timing device's edges there
is no stream at all), the driver must not already be measuring, and a
channel plan must exist. Then:

1. Build one `nidaqmx.Task`; for each planned channel add a voltage
   channel on its fixed terminal with the mapped terminal configuration
   (`DIFF`/`RSE`) and the applied min/max.
2. `cfg_samp_clk_timing(rate=expected_rate, source="/<device>/PFI0",
   active_edge=RISING, sample_mode=CONTINUOUS)` — every conversion on a
   rising CONV_START edge. The `rate` argument is only the *expected* rate
   (from the session: dt for ramp mode, 1/T_int for markers mode) used for
   buffer sizing and read timeouts; the true rate is set by the edges.
3. **`task.start()` before returning** — the arming contract (BUG B24):
   the 6451 ignores clock edges while no acquisition is in progress, so
   the caller (`HardwareService.start_receiving`) may only send the timing
   start (`S` to the Pico) *after* the acquisition driver has returned
   successfully. The first edge after `S` — the first integration edge of
   window 0 — is therefore the first converted sample, exactly the
   alignment the ramp window extractor assumes.
4. Only then is the reader thread (`ni-daqmx-reader`, daemon) spawned.

### 2.6 The reader thread: block sizing, timeouts, conversion, drain

- **Block size**: `max(1, min(rate · 20 ms, 2²⁰))` samples per
  `read_many_sample` call. At 1 MS/s that is 20 000 samples (~20 ms of
  latency); in markers mode the rate is 1/T_int — as low as one sample per
  read — so the live pipeline still advances per window instead of
  delivering hundred-second lumps (BUG B27 history).
- **Read timeout**: `min(max(10 · block/rate, 0.5), 2.0)` s. While the run
  is armed but the timing engine has not been started yet, the timeout
  expiry arrives as DAQmx error **−200284**; the reader treats it as "no
  CONV_START edges yet" and keeps waiting — this is the armed-but-no-edges
  watchdog. A stop request landing mid-wait breaks out to the drain below
  (re-raising there would strand the buffer tail).
- **Conversion** (`deliver`): the reader buffers volts as float64; each
  block is converted to **zero-based uint32 counts over the applied
  bipolar range**: `(v − vmin) · (2²⁰−1)/(vmax − vmin)`, rounded, clipped
  to `[0, 2²⁰−1]`. NI clamps out-of-range inputs at ± full scale —
  **railing is the overflow indication**; no sentinel is injected (unlike
  the µC drivers, which emit `2^bits` for missing/overflowed samples).
  Blocks are pushed per channel into the ramp sink with the running
  **absolute sample index** (index 0 = the first sample of window 0), so
  the storage layer's gap detection is fed by the driver's own
  contiguity counters.
- **Final drain**: after the stop event, the reader loops
  `read_many_sample(READ_ALL_AVAILABLE, timeout=0.1)` until the buffer is
  empty, **before** `stop_measurement()` calls `task.stop()` (which would
  discard unread samples — in markers mode those are measurement rows).
  Only then does the main thread stop and close the task (join timeout
  6 s; `task.stop()` also unblocks a wedged reader). Any other reader
  exception marks the run failed (`_error` → `stop_measurement` returns
  False).

### 2.7 The three timing modes from the driver's point of view

| `session.timing_mode` | CONV_START carries | Sink | Files |
|---|---|---|---|
| `ramp` | the ramp-sampling train, dt apart, N = T_int/dt + 1 per window | `RampStorageService` → `ramp/*.bin` (AQR1) | yes |
| `markers` | two edges per window (integration start/end; the Pico runs its generator with dt = T_int) | `RampPairExtractorService` (in-memory) → classic start/stop rows in `data.csv` | no (would duplicate `data.csv`) |
| `internal` | — | **refused at start**: this driver is meaningless without the external clock | — |

The mode itself is decided by the `HardwareManager`
(`timing_mode_for(sample_ramp)`): `ramp` when the user asked for ramp
sampling, `markers` on split hardware with a trigger-capable timing device,
`internal` otherwise (all-in-one setups — never this card's case).

### 2.8 End-to-end sequence for a split measurement

```
connect()                       enumerate, self-test, PFI0 family → 3.3 V
configure_channels(plan)        done by the manager at config load
set_input_range(±5 V)           one of the four exact ranges
configure_timing(...)           goes to the TIMING device (the Pico) — the
                                6451 receives no configuration commands
start_measurement(..., sink)    build task, cfg_samp_clk_timing(PFI0),
                                task.start()   ← armed, waiting for edges
  (only now does the manager send 'S' to the timing device)
  …reader converts edge-clockied blocks into the sink…
stop_measurement()              drain buffer → task.stop() → task.close()
```

### 2.9 Known limitations and open points

- **Validation with the real card** is still open (the driver is tested
  against a fake `nidaqmx` module; live validation needs the DAQmx runtime
  and the USB-6451 on the measurement PC).
- **Overflow is only visible as railing**: a clipped full-scale reading
  cannot be distinguished from a legitimate one — the analysis must treat
  full-scale samples the way it treats the µC sentinel.
- **No hardware overflow flag / no per-sample timestamping**: the sample
  grid is trusted to be exactly the CONV_START edges; a *missing* edge
  (e.g. the timing device stalls) shows up as the reader's timeout, not as
  a gap — the sink's absolute-index contiguity check is the safety net.
- The ±0.2 V range is only available on USB-6451 revision F and later
  (and the OEM variant); on a revision D card the driver's range list
  would be accepted by software but is not supported by the hardware.
