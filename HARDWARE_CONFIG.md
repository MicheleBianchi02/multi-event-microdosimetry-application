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
    "firmware_version": "1.4",
    "connection": { "type": "serial", "port": "/dev/pico_daq", "baud_rate": 115200 },
    "note": "udev rule 99-atomiqa-pico.rules creates /dev/pico_daq.",
    "capabilities": {
        "timing":      { ... },
        "gain":        { "pins": [2, 3, 4] },
        "acquisition": { ... }
    }
}
```

| Field | Meaning |
|---|---|
| `name` | Human-readable name, shown in the app and written into the archives. |
| `driver` | Stable code selecting the communication implementation (`pico_serial_v1`, `pic_serial_v1`, `dummy_v1`). Must be known to the app. |
| `firmware_version` | Optional, informational; recorded in the archives. |
| `connection` | Transport: `type: "serial"` with `port` (persistent udev name, e.g. `/dev/pico_daq`) and `baud_rate`; or `type: "dummy"` for the virtual acquisition device (no port). |
| `capabilities` | What this device can do in this setup. Declare a block only if the device is used for it. |

A device is *usable* in a role only if it declares the matching capability
block (see §2.6 for the role → capability mapping).

#### `capabilities.timing`

```json
"timing": {
    "integration_time_us": { "min": 1, "max": 10000000, "step": 1 },
    "reset_time_us":       { "min": 1, "max": 10000000, "step": 1 },
    "period_us":           null,
    "ramp_sampling": {
        "supported": true,
        "dt_us": { "min": 10, "max": 10000, "step": 10 }
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
"gain": { "pins": [2, 3, 4] }
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
        "input_ranges_volts": [ { "min_volts": 0.0, "max_volts": 5.0 } ],
        "default_input_range_volts": { "min_volts": 0.0, "max_volts": 5.0 },
        "supports_oversampling": false
    }
}
```

| Field | Meaning |
|---|---|
| `num_channels` | How many channels can be measured **simultaneously**. This is the binding constraint: a device may time two channels but acquire one (the Pico does). |
| `adc.bits` | ADC resolution. Fixed by the chip; recorded in every archive. |
| `adc.input_ranges_volts` | Non-empty list of the voltage spans the device accepts. Exactly one entry = fixed range (as on the Pico: 0…5 V). An instrument such as a NI card lists all selectable ranges (±0.2, ±2.5, ±5, ±10 V, …). |
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

- every switchable capacitor of every active circuit must be driven by
  **exactly one** pin;
- the pin must belong to the device that holds the channel's gain role;
- a pin **may** drive capacitors of both circuits (first two lines above):
  the two channels' gains are then *ganged* — they always switch together,
  and the app links the two gain selections in the wizard (choosing for CH1
  constrains what CH2 can be).

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
exactly the shape of a `devices`/`circuits` entry. Fragment files for
devices carry an **empty serial port** on purpose: the port is
machine-specific and must be filled in after insertion.

Shipping a new component into the library = dropping a fragment file into
the folder; conversely, the editor's **"Save as Component…"** writes the
selected device or circuit as a new fragment (device fragments are saved
without the serial connection, which is machine-specific).

---

## 4. The editor

Workspace Settings → **Hardware Configuration**:

- **Import / Replace…** — pick an existing `hardware_config.json` anywhere on
  disk. It is validated first (a file with errors is refused with the error
  list, nothing changes), then **copied into the workspace folder** and
  linked. If a file with the same name already exists in the workspace with
  different content, overwriting must be confirmed.
- **Edit…** — opens the linked configuration. Interface-determined values
  (driver, timing envelope, gain pins, ADC properties) are shown **read-only**
  with their physical limits as hints; everything that belongs to the bench —
  capacitor values, wiring, roles, channels, names, notes, serial port — is
  editable. Changes are validated live; the file can only be saved when it
  has no errors.
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
   facility, beam type, shift note are stored in `setup.json`.
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
- `timestamp` is seconds since the start of the measurement; overflowed
  samples are `NaN`.

In `settings.json`, the `hardware_settings` section is a **snapshot of what
the hardware actually did** (read-only for the analysis):

```json
"hardware_settings": {
    "adc": { "bits": 12, "input_range_volts": { "min_volts": 0.0, "max_volts": 5.0 } },
    "hardware_config_sha256": "…",
    "hardware": {
        "config_file": "hardware_config.json",
        "devices": { "pico_1": { "name": "...", "driver": "pico_serial_v1", "firmware_version": "1.4" } },
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

### 6.2 The virtual acquisition

For bench tests of the timing and gain interfaces, the library offers a
**virtual acquisition device** (driver `dummy_v1`). A configuration using it
is fully valid and a measurement can be started — the app notifies clearly
that **no data will be collected** — while the real timing signals and
capacitor switching are verified on the bench.

---

## 7. Pre-3.0 files

- Archives of format "2.0" and legacy CSVs load as before; their hardware
  provenance is unknown (no embedded configuration).
- Old `setup.json` files (with `hardware_settings`, capacitor lists, ADC
  fields) load with those fields ignored; link a hardware configuration to
  migrate the workspace.
