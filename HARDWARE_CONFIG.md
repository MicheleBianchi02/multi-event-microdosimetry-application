# Hardware configuration — file reference and workflow

This document describes the hardware configuration file (`hardware_config.json`)
of ATOMIQA 3.0: its structure, the workflow around it (campaign setup,
editing, validation), and how the hardware information flows into the
measurement archives. It is written for the user of the application; no
knowledge of the codebase is assumed. A validated, complete example ships
with the app (`examples/hardware_config_all_pico.json`).

---

## 1. What the file is for

The hardware configuration describes **one physical setup of the measurement
chain**: which devices exist (microcontrollers, instruments), which electronic
circuits (integrator boards), how the device pins are wired to the circuit
capacitors, and which device plays which role in the measurement.

It is the single source of truth for:

- the **limits** the user interface enforces (integration/reset time bounds,
  ramp-sampling granularity) — the app refuses values outside them;
- the **gain tables** offered per channel (derived from the circuit's
  capacitors and the wiring);
- the **identity** of the hardware written into every measurement archive
  (which device, which driver, which firmware, which capacitors).

The file is created once per setup and lives **in the workspace folder,
beside the campaign's `setup.json`** (see §5).

---

## 2. File structure

The file is JSON with five flat sections, cross-referenced by identifiers,
plus a schema version:

```json
{
    "schema_version": "1.0",
    "devices":     { "pico_1":   { ... } },
    "circuits":    { "board_ch1": { ... } },
    "channels":    { "ch1": { "circuit": "board_ch1" } },
    "roles":       { "timing": "pico_1", "acquisition": "pico_1", ... },
    "connections": [ { "device": "pico_1", "pin": 2, "circuit": "board_ch1", "capacitor": "c100" } ]
}
```

Identifiers (`pico_1`, `board_ch1`, `c100`, …) are free names chosen by the
user; they must be unique within their section and are used verbatim in the
cross-references. Every object (device, circuit, capacitor, channel,
connection) accepts an optional free-text `"note"`.

### 2.1 `devices` — the microcontrollers / instruments

```json
"pico_1": {
    "name": "Pico 2 — all roles",
    "driver": "pico_serial_v1",
    "firmware_version": "2.0",
    "connection": { "type": "serial", "baud_rate": 115200 },
    "note": "No port here: the app opens /dev/atomiqa/pico_1, the udev rule creates it (§5.1).",
    "capabilities": {
        "timing":      { ... },
        "gain":        { "pins": [17, 18, 19] },
        "acquisition": { ... }
    }
}
```

| Field | Meaning |
|---|---|
| `name` | Human-readable name, shown in the app and written into the archives. |
| `driver` | Stable code selecting the communication implementation (`pico_serial_v1`, `pic_serial_v1`, `dummy_v1`). Must be known to the app. |
| `firmware_version` | Optional, informational; recorded in the archives. |
| `connection` | Transport: `type: "serial"` with `baud_rate`, `type: "daqmx"` with `device` (the DAQmx device name of the NI card, e.g. `NI6451`), or `type: "dummy"` for the virtual acquisition device. **No port**: the file stays machine-independent — the serial persistent name is derived from the device id (`/dev/atomiqa/<id>`, §5.1), the NI card is identified by its name in the DAQmx driver stack. A leftover `port` field from an older file is ignored with a warning. |
| `capabilities` | What this device can do in this setup. Declare a block only if the device is used for it. |

A device is *usable* in a role only if it declares the matching capability
block (see §2.6 for the role → capability mapping).

#### `capabilities.timing`

```json
"timing": {
    "integration_time_us": { "min": 5, "max": 10000000, "step": 1 },
    "reset_time_us":       { "min": 5, "max": 10000000, "step": 1 },
    "period_us":           null,
    "ramp_sampling": {
        "supported": true,
        "dt_us": { "min": 1, "max": 4360, "step": 1 }
    }
}
```

All times are in **microseconds** (the unit is in the name). Each timing
parameter has one of three shapes:

| Shape | Meaning |
|---|---|
| `{ "min": a, "max": b, "step": s }` | Settable between `a` and `b`; the user interface offers `s` as step. |
| `{ "value": x }` | Fixed by the firmware: displayed, not settable. |
| `null` | Derived by the firmware (e.g. the period is the sum of integration and reset), not independently configurable. |

`ramp_sampling.supported` is **mandatory** (write `false` explicitly when the
device cannot generate ramp triggers). These values are the **declared
envelope** of this specific unit: they must lie *within* the physical limits
of the driver (which are hardcoded in the app and checked at load time), and
may be *tighter* — useful to restrict a campaign on purpose. The exact
relationship between integration, reset and period (how the times are
actually generated) is firmware behaviour, described by the driver, not in
this file.

> **Ramp sampling and acquisition:** on some devices the two cannot run at
> the same time (the Pico, for instance, has a single ADC). This constraint
> is firmware knowledge and is enforced by the app automatically: when one
> device holds both the timing and the acquisition role, the *Sample Ramp*
> option is disabled with an explanatory tooltip.

#### `capabilities.gain`

```json
"gain": { "pins": [17, 18, 19] }
```

The GPIO pins the device uses to switch capacitors, in **gpio-mask order**:
the first list entry corresponds to bit 0 of the mask. The identifiers are
the pin numbers (or names) of the device. Which pin drives which capacitor
is *wiring*, declared in `connections` (§2.5), not here. The number of pins
is fixed by the device firmware.

#### `capabilities.acquisition`

```json
"acquisition": {
    "num_channels": 1,
    "adc": {
        "bits": 12,
        "input_ranges_volts": [ { "min_volts": 0.0, "max_volts": 3.3 } ],
        "default_input_range_volts": { "min_volts": 0.0, "max_volts": 3.3 },
        "supports_oversampling": false
    }
}
```

| Field | Meaning |
|---|---|
| `num_channels` | How many channels can be measured **simultaneously**. This is the binding constraint: a device may time two channels but acquire one (the Pico does). |
| `adc.bits` | ADC resolution. Fixed by the chip; recorded in every archive. |
| `adc.input_ranges_volts` | Non-empty list of the voltage spans the device accepts. Exactly one entry = fixed range (as on the Pico: 0…3.3 V — the bare ADC pin, no front-end scaler). An instrument such as a NI card lists all selectable ranges (±0.2, ±2.5, ±5, ±10 V, …). |
| `adc.default_input_range_volts` | Required (and must match one listed range) when more than one range is declared; forbidden when the range is fixed. |
| `adc.supports_oversampling` | Informational. |

### 2.2 `circuits` — the electronic circuits

```json
"board_ch1": {
    "name": "Integrator board — detector 1",
    "model": "ATOMIQA integrator rev. B",
    "fixed_capacitance_pF": 22.0,
    "switchable_capacitors": [
        { "id": "c100",  "value_pF": 100.0 },
        { "id": "c560",  "value_pF": 560.0 },
        { "id": "c3300", "value_pF": 3300.0, "note": "NP0, replaced 2026-03." }
    ]
}
```

- `fixed_capacitance_pF` is the always-connected capacitance (0 if none).
- `switchable_capacitors` is the list of GPIO-switched capacitances; use an
  **empty list** `[]` when the circuit has none. Every capacitor needs an
  `id` (used by the wiring).
- The selectable gain values of a channel are computed from this list: all
  combinations of switched-on capacitors, plus the fixed one, sorted by
  total capacitance (largest total = lowest gain = "1x").

### 2.3 `channels` — which detectors are used

```json
"channels": {
    "ch1": { "circuit": "board_ch1", "adc_input": "ai0" }
}
```

The presence of a channel is what makes it active: **a single-detector run
simply omits `ch2`** (and its gain role, circuit and wiring). `ch2` requires
`ch1`. Each channel binds exactly one circuit. `adc_input` is optional and
instrument-specific (e.g. the input terminal of a NI card); with the
microcontrollers it is unnecessary and may be omitted.

`termination` (optional, `"differential"` or `"rse"`) tells the acquisition
device how the channel's signal reaches it — e.g. one channel differential
and the other single-ended is legal. The *physical input pins* behind each
choice are fixed hardware facts hardcoded in the driver: on the NI USB-6451,
ch1 differential = AI0(+)/AI8(−), ch2 differential = AI1(+)/AI9(−), ch1
single-ended = AI2, ch2 single-ended = AI3 (four different ADCs, so every
mix samples simultaneously at 1 MS/s per channel). When omitted, the
acquisition driver's default (`differential`) applies.

### 2.4 `roles` — who does what

```json
"roles": {
    "timing": "pico_1",
    "acquisition": "ni_1",
    "gain_ch1": "pico_g1",
    "gain_ch2": "pic_g2"
}
```

| Role | Required | Capability needed |
|---|---|---|
| `timing` | always | `timing` |
| `acquisition` | always (use the virtual device for bench tests, §6) | `acquisition` |
| `gain_ch1` / `gain_ch2` | for each active channel | `gain` |

The same device may hold several roles (the all-Pico setup holds all of
them). The gain controllers also carry the event-mode line of the
electronics (single-event / multi-event; low = ME, high = SE on the current
hardware): switching mode signals **all** gain controllers.

### 2.5 `connections` — the wiring

```json
"connections": [
    { "device": "pico_1", "pin": 2, "circuit": "board_ch1", "capacitor": "c100" },
    { "device": "pico_1", "pin": 2, "circuit": "board_ch2", "capacitor": "c100" }
]
```

A flat list of wiring edges: *this pin of this device drives that capacitor
of that circuit*. Rules:

- a switchable capacitor is driven by **at most one** pin; the pin must
  belong to the device that holds the channel's gain role;
- a capacitor left **unwired is allowed**: it is never switched and does not
  appear in the gain table. This is the normal case when a circuit has more
  switchable capacitors than the gain controller can drive — the app loads
  the file with a warning listing the excluded capacitors;
- a pin **may** drive capacitors of both circuits (first two lines above):
  the two channels' gains are then *ganged* — they always switch together,
  and the app links the two gain selections in the wizard (choosing for CH1
  constrains what CH2 can be). A wiring may also mix shared and
  circuit-specific pins; per shared pin, the two channels' selections must
  agree.
- **Shared and grouped pins (atomic apply & summing):** gain selections are
  applied atomically across all active channels: the merged pin state is validated
  before any pin is toggled, preventing conflicting bench states. Multiple
  capacitors wired to the same pin are grouped and contribute the sum of their
  capacitances to the gain table.

### 2.6 Validation

The file is validated **completely at load time** — structure, all
cross-references, the limits against the driver's hardcoded physical limits,
and the wiring. An invalid file is rejected with a popup listing **every**
problem; nothing is loaded, nothing connects. The same validation runs live
in the editor, which therefore cannot save an invalid file. A file that is
merely *unusual* (e.g. a circuit not bound to any channel) loads with a
warning.

---

## 3. The component library

`hardware_library/` contains ready-made **fragments** — one component per
file — that the editor's *"New from Components…"* and *"Add from Library…"*
assemble into complete configurations:

```json
{
    "kind": "device",
    "suggested_id": "pico_1",
    "label": "Raspberry Pi Pico 2 (timing + gain + acquisition)",
    "data": { "name": "Pico 2", "driver": "pico_serial_v1", "connection": {...}, "capabilities": {...} }
}
```

`kind` is `device` or `circuit`; `suggested_id` pre-fills the identifier in
the assembled configuration (made unique if already taken); `data` has
exactly the shape of a `devices`/`circuits` entry. Since the connection
carries no port (§2.1), a device fragment is complete as shipped: there is
nothing machine-specific to fill in after insertion.

Shipping a new component into the library = dropping a fragment file into
the folder; conversely, the editor's **"Save as Component…"** writes the
selected device or circuit as a new fragment (devices keep their whole
connection block; a stale deprecated `port` field is dropped).

Shipped components:

| Fragment | Provides |
|---|---|
| `device_pico2.json` | Raspberry Pi Pico 2 (firmware PICO2DAQ 2.1) — timing (int ≥ 5 µs, reset ≥ 5 µs, dt 1 µs…10 s), gain (3 pins: GP17/18/19), acquisition (1 channel, 12 bit, 0…3.3 V). The dt envelope follows the *connected* firmware: 2.1 lifted the software generator's dt cap (markers mode with long integrations, ramp sampling with coarse dt); on boards still running 2.0 the driver enforces the 4360 µs cap and rejects larger dt with the reason. |
| `device_pic.json` | PIC — timing (no ramp sampling), gain (1 pin), acquisition (2 channels, 12 bit, 0…5 V). |
| `device_usb6451.json` | NI USB-6451 — acquisition only (DAQmx device `NI6451`): 2 simultaneous channels, 20 bit, ±0.2/±2.5/±5/±10 V (default ±5 V), clocked by the external CONV_START from the timing device. Combine with a timing µC (e.g. the Pico fragment without its acquisition block). |
| `device_dummy_acquisition.json` | Virtual acquisition (`dummy_v1`): bench tests only, no data. |
| `circuit_board_rev_b_ch1.json` | Integrator board rev. B: 22 pF fixed + 100/560/3300 pF switchable. |
| `circuit_board_rev_b_ch2.json` | Integrator board rev. B: 100 pF switchable, no fixed. |

A fragment declares the capability blocks its device uses in the setup, and
a role can only be assigned to devices declaring the matching block (§2.4).
The shipped fragments declare **all** capabilities their driver supports; a
hand-made fragment may declare fewer (e.g. a Pico used only for timing).
To extend an existing configuration with a capability, add the block to the
device entry by editing the workspace JSON directly — the declared values
must lie within the driver's limits, and the app validates them on the next
load.

---

## 4. The editor

Workspace Settings → **Hardware Configuration**:

- **Import / Replace…** — pick an existing `hardware_config.json` anywhere on
  disk. It is validated first (a file with errors is refused with the error
  list, nothing changes), then **copied into the workspace folder** and
  linked. If a file with the same name already exists in the workspace with
  different content, overwriting must be confirmed.
- **Edit…** — opens the linked configuration. Interface-determined values
  (driver, timing envelope, gain pins, ADC properties, the derived persistent
  name) are shown **read-only** with their physical limits as hints;
  everything that belongs to the bench — capacitor values, wiring, roles,
  channels, names, notes, baud rate — is editable. Changes are validated
  live; the file can only be saved when it has no errors.
- **New from Components…** — assembles a fresh configuration from the library.
- **Remove Link** — unlinks the configuration (the file stays on disk);
  real-hardware features are then disabled.

Saving (from Import, Edit or New) always writes into the workspace folder and
updates the link in `setup.json` — see §5. Configurations created by the app
default to the name `hardware_config.json`; imported files keep their own
name.

---

## 5. Workflow and the link to `setup.json`

One measurement **campaign** = one workspace folder:

```
my_campaign/
├── setup.json               campaign metadata + hardware link
└── hardware_config.json     the hardware configuration (this file)
```

1. **Create the workspace** (Workspace Settings → *Create/Save Workspace…*):
   facility, beam type, shift note are stored in `setup.json`. The setup
   file may carry any name (e.g. `campaign_setup.json`): the app remembers
   which one it loaded and writes the hardware link there — it never creates
   a second `setup.json` beside it. The examples below use the conventional
   `setup.json` name.
2. **Import or assemble the hardware configuration** (same dialog): the file
   is copied into the folder and `setup.json` records the link:

   ```json
   "hardware": {
       "config_file": "hardware_config.json",
       "config_sha256": "3f1c9a02e7b4…"
   }
   ```

   `config_sha256` is the fingerprint of the file at link time.
3. **Load the workspace**: the app loads the linked configuration
   automatically, re-verifies the hash and — if the file was edited outside
   the app since the link was created — **warns** that measurements may not be
   directly comparable with earlier ones of the campaign. Loading a file that
   does not validate blocks the hardware with the error list.
4. **Measure**: the wizard cannot proceed past Step 1 without a loaded
   configuration (or simulation) — the gate that guarantees every measurement
   has a known hardware context.
5. **Offline analysis**: works without any workspace or hardware; archives
   carry their own hardware description (§6).

For quick bench tests the expert simply creates a test workspace (e.g.
`test_setup/`) with the same mechanism — no special mode exists.

### 5.1 Persistent port names (udev rules)

The configuration file deliberately carries **no port**: the persistent name
of every serial device is **derived from its id** as `/dev/atomiqa/<id>` —
e.g. the device `pico_1` is always opened at `/dev/atomiqa/pico_1`. The
config therefore stays machine-independent (it can be reused on another
machine or years later); what binds a *physical board* to that name is one
udev rule per device, matched by the board's **USB serial number** (unique
per board; two identical Picos stay distinguishable — and the rule needs no
`ttyACMx`, which changes with the plug order, at all):

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0009", ATTRS{serial}=="E6614C31…", SYMLINK+="atomiqa/pico_1"
```

Write the rules by hand, or generate them **from the hardware
configuration** (the symlink names are exactly the device ids):

```bash
sudo ./setup_udev_rules.sh <workspace>/hardware_config.json
```

The tool lists the currently plugged boards and asks which is which (or use
`--map DEVICE_ID=SERIAL` non-interactively, see
`tools/generate_udev_rules.py`), then writes
`/etc/udev/rules.d/99-atomiqa.rules` and reloads udev. Run it once when a
setup is first cabled, after renaming a device id, or after replacing a
board. **Step-by-step instructions for writing the rules by hand** are in
[UDEV_RULES.md](UDEV_RULES.md). An old `99-daq-devices.rules` from previous
app versions can be deleted; configs written before the port removal still
load (their leftover `connection.port` is ignored with a warning). Access
permissions are unchanged: the user needs the usual serial-port group
(e.g. `dialout`).

Where the rules are missing (another machine, Windows) or a board is
unplugged, auto-connect fails fast and the manual-connect dialog maps each
device to an available port for the current session — nothing is written
back to the configuration.

---

## 6. Output files

### 6.1 Measurement archive (ZIP, format version "3.0")

```
measurement_YYYYMMDDTHHMMSS.zip
├── data.csv              the raw samples
├── settings.json         everything about this measurement
├── hardware_config.json  verbatim copy of the configuration used
└── setup.json            verbatim copy of the campaign setup
```

The two embedded copies make each archive **self-contained**: it can be
analyzed years later, on another machine, without the workspace. Loading an
archive always interprets it with its embedded configuration, so edits made
to the workspace configuration after the run cannot corrupt the
interpretation of old data.

`data.csv` columns:

- operational mode: `timestamp,ch1,ch2` — the start/stop voltage-differences
  of each channel;
- debug mode: `timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop`;

> **Debug vs operational.** In debug mode the raw start/stop of every
> integration is transmitted and stored; in operational mode only the
> difference is. The PIC supports both in firmware (`M D`/`M P`); the Pico
> has no mode command and *always* transmits start/stop — its operational
> mode is derived by the driver on receipt. The mode of each archive is
> recorded in `settings.json` → `measurement_info.acquisition_mode`; for
> files without that field, infer it from the column layout above.
- `timestamp` is seconds since the start of the measurement; overflowed
  samples are `NaN`.

In `settings.json`, the `hardware_settings` section is a **snapshot of what
the hardware actually did** (read-only for the analysis):

```json
"hardware_settings": {
    "adc": { "bits": 12, "input_range_volts": { "min_volts": 0.0, "max_volts": 3.3 } },
    "hardware_config_sha256": "…",
    "hardware": {
        "config_file": "hardware_config.json",
        "devices": { "pico_1": { "name": "...", "driver": "pico_serial_v1", "firmware_version": "2.0" } },
        "circuits": { "board_ch1": { "fixed_capacitance_pF": 22.0, "switchable_capacitors": [...] } },
        "channels": { "ch1": "board_ch1" },
        "roles":   { "timing": "pico_1", "...": "..." }
    },
    "channels": {
        "ch1": { "gpio_mask": [true, false, true], "capacitance_pF": 3422.0 }
    },
    "single_channel_mode": false
}
```

The charge corresponding to a sample is `Q = C · ΔV` with `C` the channel's
`capacitance_pF` and `ΔV` the voltage difference reconstructed from the ADC
counts: `V = V_min + counts / (2^bits − 1) · (V_max − V_min)` over the
recorded input range.

### 6.2 `settings.json` — field reference

Top-level sections: `file_format_version`, `versions`, `measurement_info`,
`experimental_setup`, `hardware_settings`, `timing_settings`, `analysis`.
Sections marked **frozen** document what the hardware actually did: they are
written at measurement time and are not touched by the offline analysis.
Sections marked **editable** can be revised after the fact (the measurement
itself does not change — only its interpretation and bookkeeping).

#### `file_format_version`

`"3.0"` for the current format; `"2.0"` archives and legacy CSVs (format
"1") load with reduced hardware information (see §7).

#### `versions` — *(editable, informational)*

| Field | Meaning |
|---|---|
| `app_version` | Application version that recorded the measurement. |

#### `measurement_info`

| Field | Meaning |
|---|---|
| `timestamp_start` / `timestamp_end` | ISO-8601 timestamps of the first and last sample. |
| `acquisition_mode` | `"debug"` (start/stop of each integration recorded) or `"operational"` (only the difference). **Frozen**. New archives always record it; in older files the mode must be inferred from the `data.csv` column layout. |
| `is_problematic` | Flag set manually by the user to mark a measurement as suspect. *(editable)* |

#### `experimental_setup` — *(editable, metadata)*

| Field | Meaning |
|---|---|
| `facility`, `beam_type` | Where and what was measured (free text or from the predefined lists). |
| `beam_energy_MeV`, `beam_current_nA` | Beam parameters during the run. |
| `depth_cm` | Depth in phantom (for depth-scan campaigns). |
| `detector_voltage_V` | Detector bias. |
| `calibration_slope`, `calibration_intercept` | Linear calibration of the amplitude axis: `value_calibrated = slope · value + intercept`. |
| `shift_note`, `measurement_note` | Free-text notes (campaign-level and per-measurement). |

#### `hardware_settings` — **frozen** (snapshot of what the hardware did)

| Field | Meaning |
|---|---|
| `adc.bits`, `adc.input_range_volts` | ADC resolution and the range **applied** during this run. The counts→volts conversion in §6.1 uses exactly these. |
| `channels.chX.gpio_mask` | Pin states applied for the channel's gain: one boolean per declared pin of the gain device, in the config's pin order. |
| `channels.chX.capacitance_pF` | Total integration capacitance resulting from the mask (fixed + switched-on capacitors), in pF. This is the `C` of the charge formula. |
| `single_channel_mode` | `true` when only CH1 was measured. |
| `hardware_config_sha256` | Fingerprint of the embedded `hardware_config.json`. |
| `hardware` | Resolved summary of the configuration: devices (name / driver / firmware), circuits with their capacitors, channel→circuit binding, role assignment. |

On load, the app verifies that `gpio_mask` still reproduces
`capacitance_pF` against the embedded configuration's gain table, and that
the embedded file matches `hardware_config_sha256`; inconsistencies are
reported as warnings. Pre-3.0 archives instead carry the old
`adc_num_bits` / `capacitor_code` fields (informational only).

#### `timing_settings` — **frozen** (the applied timing)

| Field | Meaning |
|---|---|
| `integration_time_s`, `reset_time_s` | The **actual** (quantized) times the hardware used — the values the wizard reported before the run, in seconds. |
| `period_s` | The measurement period as recorded from the driver's applied-values preview. In `internal` mode it equals int + reset; in `ramp`/`markers` mode it is reset + settle + N·dt (N = int/dt + 1 edges), which differs from the sum of the two times. |
| `sample_frequency_hz` | `1 / period_s`; converts the `timestamp` column and the time ROIs. |
| `timing_mode` | What the timing controller generated: `internal` (classic no-ramp on its own ADC), `ramp` (ramp-sampling triggers at the recorded dt) or `markers` (split hardware without ramp sampling: the ramp generator runs with dt = integration time, i.e. two conversion edges per window — integration start/end — for an external digitizer). Additive field; older archives have no `timing_mode` and are always `internal`. |

#### `analysis` — *(editable: the offline interpretation)*

| Field | Meaning |
|---|---|
| `analysis_timestamp` | When the current analysis was computed. |
| `n_bootstraps` | Number of bootstrap resamples used for the uncertainties (`null` if not computed). |
| `rois_time_s` | Time filters: list of `[t_start, t_stop]` pairs in seconds. Re-applied to the raw data when the archive is loaded. |
| `rois_channel` | Amplitude filters: list of `[channel, min, max]` triples (`channel`: 0 = CH1, 1 = CH2; bounds in ADC counts). Re-applied on load. |
| `results` | The variance–covariance estimators (below). |
| `fft` | Present only when the FFT analysis was run and applied. The parameters as chosen in the dialog — `method` (`periodogram`/`welch`), `window` (`rect`/`hann`), `nperseg`, `excluded_bands` (list of `[f_min, f_max]` Hz pairs the user marked as excluded; the white floor is measured on the complement), `threshold_ch1`/`threshold_ch2` (per-channel clip levels in counts²/Hz; `null` = not used) — plus a `diagnostics` snapshot per channel: `floor_level` (counts²/Hz) and the **selected** peaks (`peaks`: `f_lo`, `f_center`, `f_hi` in Hz, `variance` in counts² — the ones the user identified as real in the dialog's peak panel, listed in ascending frequency) together with `peaks_detected`, the total number found before selection. The selection is documentation: the `Covariance` of `var_fft` is the global non-white excess and does not depend on it. The PSD itself is not stored: it is recomputable from `data.csv` with exactly these parameters. |

`results` contains one entry per estimator — `var` (variance method),
`var_cov` (successive covariance), `var_autocov` (lag-1 autocovariance),
`var_doublecov` (cross-channel covariance, two detectors only) and
`var_fft` (frequency-domain method, present once computed; see `fft` above).
Each
parameter is a 4-element list with the layout
**[CH1 value, CH1 uncertainty, CH2 value, CH2 uncertainty]**:

| Parameter | Meaning (per method) |
|---|---|
| `N` | Number of samples used. |
| `Mean` | Mean amplitude (ADC counts). |
| `Variance` | Variance of the amplitude (counts²). |
| `Covariance` / `AutoCorrelation` | The covariance term the method subtracts (`var_cov`: covariance of successive samples; `var_autocov`: lag-1 autocovariance; `var_doublecov`: covariance between the two detectors). |
| `y_bar_d` | The dose-averaged lineal energy estimate: (Variance − Covariance) / Mean. |
| `cov_mean_sq` / `autocov_mean_sq` | The covariance term relative to the squared mean (used for the uncertainty propagation). |

An `N` of 0 marks a quantity that was not computed (e.g. CH2 in
single-channel mode, or `var_doublecov` without a second detector).
Uncertainties are analytical, or from a circular block bootstrap (then
`n_bootstraps` records the resample count).

Loading an archive restores timing, metadata and ROIs, re-applies the ROIs
to the raw `data.csv` (the stored data is never pre-filtered), and merges
the stored `results` back into the analysis view.

### 6.3 The virtual acquisition

For bench tests of the timing and gain interfaces, the library offers a
**virtual acquisition device** (driver `dummy_v1`). A configuration using it
is fully valid and a measurement can be started — the app notifies clearly
that **no data will be collected** — while the real timing signals and
capacitor switching are verified on the bench.

### 6.4 Ramp-sample files

Measurements taken with *Sample Ramp* enabled additionally record the
**voltage ramp itself**, not just the two integration-boundary samples.
These files are large (2 channels × 1 MSPS × 4 B ≈ 29 GB/hour) and live
**outside** the ZIP, in the campaign's `ramp/` folder:

> Runs with *Sample Ramp* unchecked do **not** create these files. On split
> hardware (µC times, NI acquires) the digitizer still works: it converts
> exactly on the two integration-boundary edges per window, and those
> start/stop values reach `data.csv` through the normal path — nothing is
> duplicated on disk.

```
my_campaign/
├── setup.json
├── measurement_20260924T120000.zip
└── ramp/
    └── measurement_20260924T120000/     (suffix _1, _2… if the name exists)
        ├── ch1.bin                       one binary file per channel
        ├── ch2.bin
        └── index.json                    totals, gaps, run diagnostics
```

- **Format** (`AQR1`, version 1): a fixed 4096-byte header followed by raw
  ADC counts as little-endian 32-bit integers (the driver normalizes the
  ADC's native resolution to 32 bit; the native bit depth is in the header).
  The header records the actual timing (dt, period, samples per window),
  ADC range, timing mode, driver/firmware versions, the hardware-config
  fingerprint, a run uuid shared by the two channels and the number of
  samples written.
- **What they mean**: each integration window spans exactly
  N = T_int/dt + 1 samples; sample *k* of window *w* sits at time
  `w·period + k·dt` (nothing is sampled during reset). Counts→volts uses
  the same conversion as `data.csv`, with the header's ADC bits and range.
- **Provenance**: a rolling sha256 of each file's data section is computed
  during the run and stored in the header at stop — no long re-read at save
  time. `settings.json` of the ZIP gains a `ramp` section (folder name,
  per-channel file names, sample counts, sha256s), so the pair
  ZIP + `ramp/` folder stays linked while the ZIP alone remains fully
  self-contained for the analysis.
- **Robustness**: the sample watermark in each header is refreshed about
  once per second, so a crashed run recovers up to its last whole window
  (the workspace recovery flow does this automatically); dropped blocks are
  recorded as gaps in `index.json` — the window alignment never shifts
  silently.

---

## 7. Pre-3.0 files

- Archives of format "2.0" and legacy CSVs load as before; their hardware
  provenance is unknown (no embedded configuration).
- Old `setup.json` files (with `hardware_settings`, capacitor lists, ADC
  fields) load with those fields ignored; link a hardware configuration to
  migrate the workspace.
