# ATOMIQA 3.0

Data acquisition and analysis application for microdosimetry measurements with the
variance–covariance method, applied to proton and carbon ion therapy beams.

## 1. Overview

ATOMIQA drives the readout chain of a current-mode integrator, collects the charge
measured at each integration cycle on one or two channels, computes the
variance–covariance statistics in real time, saves each measurement as a
self-describing archive (ZIP) and allows offline re-analysis of past measurements.

**Measurement principle.** The current generated in the detector is integrated on a
capacitor; the selectable capacitance is the gain of the chain. Each measurement
cycle consists of an *integration* phase (the integrator output is a voltage ramp)
followed by a *reset* phase (a switch in parallel to the capacitor discharges it).
The voltage is sampled at the start and at the end of the integration phase: the
difference of the two samples is proportional to the charge collected during the
cycle, and the series of consecutive differences feeds the variance–covariance
estimators. Optionally the ramp can be sampled several times per cycle (*ramp
sampling*); see §6 for its current status.

**Event modes.** The electronics can operate in *multi-event* (ME, integration) or
*single-event* (SE) mode. ATOMIQA handles ME measurements only: it can switch the
electronics to SE and back, but does not acquire or process SE data (that is done
by other hardware/software).

## 2. Architecture

The application follows a Model–View–Controller pattern, supplemented by a Services
layer for background tasks and a Core layer for scientific computations, plus a
hardware abstraction layer.

| Package | Responsibility |
|---|---|
| `models/` | Application state: `SessionModel` (configuration, metadata, signals) and `DataModel` (measurement arrays, online statistics) |
| `views/` | PyQt6/pyqtgraph UI (wizard, running dashboard, offline import, workspace and calibration dialogs) |
| `controllers/` | `SetupController` (wizard, connection, timing/gain configuration) and `MeasurementController` (measurement loop, save/load, results) |
| `services/` | `HardwareService`, `DataExporter` (ZIP archives), `ShiftService` (workspace setup.json, crash recovery), `AnalysisService` (FFT, moving average, physics façade) |
| `core/` | Scientific algorithms: `calc_stat.py` (variance–covariance estimators + bootstrap), `bragg_peak.py` (FLUKA curves, integration-time planning), `load_file.py` (archive/legacy readers), `data_utils.py`, `constants.py` |
| `hardware/` | Hardware abstraction: `interfaces/` (the abstract rules), `core/HardwareManager` (role routing), concrete drivers |

### 2.1 Hardware abstraction — configuration, registry, interfaces ("Rules")

> **User-facing reference:** the complete hardware configuration file
> structure, the campaign workflow around it (workspace link, sha256 check,
> gate) and the output-file formats are documented in
> [HARDWARE_CONFIG.md](HARDWARE_CONFIG.md). This section is the technical
> summary for developers.

The application never talks to concrete hardware directly. It talks to **roles**,
assigned to physical devices in a **hardware configuration file**
(`hardware_config.json`, schema version `1.0`; a validated example ships in
`examples/`):

- `timing` — one microcontroller generating the timing signals (integration, reset,
  and optionally ramp sampling triggers).
- `gain_ch1`, `gain_ch2` — one gain controller per channel (may be the same physical
  device, or two different ones). The gain controllers also own the SE/ME line of
  the electronics (low = ME, high = SE on the current hardware).
- `acquisition` — the ADC readout system (a microcontroller or e.g. a NI instrument).

The file has five flat sections, cross-referenced by id: `devices` (the µCs /
instruments: id, name, stable **driver code**, transport, and per-capability
parameter blocks), `circuits` (the integrator boards: fixed + switchable
capacitors), `channels` (which circuits are active — this is where the 1-or-2
detector choice lives; omitting `ch2` means single-detector), `roles` (role →
device), and `connections` (an edge list stating which device pin drives which
capacitor of which circuit — this is where the ganged wiring of the all-Pico
bench is expressed, one pin routed to a capacitor on each board).

Key rules of the schema:

- **Timing parameters** (`integration_time_us`, `reset_time_us`, `period_us`)
  carry the unit in the name and have three shapes: `{min, max, step}` =
  settable within bounds; `{value: x}` = fixed by firmware; `null` = derived
  (e.g. period = int + reset — how the times are actually computed lives in
  the concrete driver). `ramp_sampling.supported` is mandatory.
- **Limits are enforced twice.** Drivers keep their physical limits hardcoded
  (layer 1, validated on every call — an out-of-bounds value is *rejected*,
  never clamped) and the config declares the envelope for the specific unit
  (layer 2, drives the UI). At load time the declared envelope is checked to
  lie within the hardcoded limits; on mismatch the load fails with an error
  popup listing every problem and nothing connects. Declaring *tighter* bounds
  than the hardware allows is legal (campaign restriction). In the wizard the
  declared envelope bounds the input fields: out-of-range times and ramp dt
  are highlighted and block *Apply Configuration*, and parameters fixed by the
  firmware are shown read-only.
- **Gain pins**: a device declares its GPIO pins in gpio-mask order; the gain
  tables are derived from the connections, so they contain only combinations
  the wiring actually supports. A circuit may hold more switchable capacitors
  than its controller can drive: unwired ones are excluded from the gain
  table (warning at load), while wiring that contradicts the declared pins or
  roles is a load error. When the same device drives both circuits (ganged
  pins), the two channels' masks must agree on shared pins: the pair is
  validated and applied atomically — one `set_gpio_mask` per physical device,
  conflicting pairs rejected before any pin moves. If a device rejects the
  command after another already switched, the bench state is unknown
  (`HardwareManager.gain_state_unknown`) and the configuration must be
  applied again. A pin may also drive several capacitors of the same circuit;
  they switch together and contribute the *sum* of their values to the gain
  table.
- **Acquisition** declares `num_channels` (the binding constraint for how many
  channels can be measured — a device may time two channels but acquire one)
  and the ADC: `bits` and `input_ranges_volts` (a non-empty list; more than one
  entry requires `default_input_range_volts` exactly matching one entry).
- Every component (device, circuit, capacitor, channel, connection) may carry
  a free-text `note`, and devices carry an optional `firmware_version`.

**Driver codes and the registry.** Each device's `driver` code (e.g.
`pico_serial_v1`, `pic_serial_v1`) maps to its implementation class in
`hardware/registry.py`. Codes are stable and versioned — never renamed. The
registry is also the code-side half of the handshake: an unknown code, a
capability the driver cannot fulfil, or declared limits beyond the driver's
hardcoded ones are all load errors.

**Config lifecycle.** The app runs without any config (simulation and offline
analysis only); proceeding to a measurement requires a workspace with a linked,
loaded hardware config — the wizard stops the user with a clear message
otherwise. The config always lives in the workspace folder, beside its
`setup.json`: importing a file copies it there, and the link (file name +
sha256) is recorded in `setup.json` and verified on load (warn on mismatch —
edited outside the app). The workspace dialog's **Hardware Configuration**
section offers Import/Replace, **Edit…**, and **New from Components…**: the
editor works on the validated model with live validation (it cannot save an
invalid file); values fixed by the interface (driver, timing envelope, gain
pins, ADC properties) are read-only there, while capacitor values, wiring,
roles and channels are editable. New configurations are assembled from a
component library (`hardware_library/*.json` fragments: devices and circuits;
"Save as Component…" extends it). The config file used for a run is embedded
verbatim in the measurement archive (§4.2), so every measurement carries its
own provenance.

**Virtual acquisition.** The registered driver `dummy_v1` fills only the
`acquisition` role with a no-op device: for bench tests of the timing and gain
interfaces, a measurement can start (the user is notified that no data will be
collected) without real readout hardware.

**Capability exclusivity.** Some firmware cannot run two operations
concurrently — e.g. the Pico cannot generate ramp triggers while its ADC
acquires. Such constraints are hardcoded in the driver
(`SUPPORTS_RAMP_WHILE_ACQUIRING`) and enforced in the UI: when one device
holds both the timing and the acquisition role, the Sample Ramp option is
disabled with an explanatory tooltip.

#### ITimingController (`hardware/interfaces/timing.py`)

| Method | Contract |
|---|---|
| `supports_ramp_sampling` | Property. Whether the controller can generate ramp sampling triggers. Gates the ramp UI. |
| `configure_noramp(int_time_us, reset_time_us, period_us=None)` | Configure integration and reset times for a standard measurement. Arguments are validated against the driver's hardcoded limits and rejected if out of bounds. |
| `configure_ramp(ramp_dt_us, int_time_us, reset_time_us, period_us=None)` | Configure ramp sampling: trigger interval, integration and reset times, all in one call. |
| `calculate_actual_timing(int_time_us, reset_time_us, period_us=None, num_channels=None)` | Pure computation (no serial traffic — the UI calls it on every input change). Returns `ActualTiming` with the fields `int_time_us`, `reset_time_us`, `period_us` (µs) and `sample_frequency_hz`; `HardwareManager.calculate_actual_timing` injects the config's active channel count, which the quantization of some firmware depends on (the PIC's TX delay). |
| `arm()` | Prepare the timing system for measurement (acts as an integrator reset to clear residual charge). Devices that need no arming implement it as a no-op (the Pico). |
| `set_channels(multiple_channel)` | Tell the timing controller whether one or both channels are active — the command is sent even though the channel count itself comes from the config. No-op on devices that don't need it (the Pico). |
| `set_mode(debug_mode)` | Select the transmission mode of the electronics (debug: start/stop stored; operational: only the difference). The app always sends debug; devices without a mode command no-op (the Pico). |

#### IGainController (`hardware/interfaces/gain.py`)

| Method | Contract |
|---|---|
| `set_gpio_mask(gpio_mask)` | Apply the state of **every declared pin of this device**, one bool per pin in the config's pin order. Which pin switches which capacitor is routing knowledge resolved by the `HardwareManager` from the config's connections. Masks of the wrong length are rejected. |
| `set_event_mode(is_single_event)` | The SE/ME line of the electronics is owned by the *gain* controllers. Switching mode calls this on **all** unique gain controllers (via `HardwareManager.set_event_mode`), so every circuit receives the signal (low = ME, high = SE today). A future extension will allow ME on one detector and SE on the other (per-channel event mode). |

#### IDataAcquisition (`hardware/interfaces/acquisition.py`)

| Method | Contract |
|---|---|
| `start_measurement(session_model, data_model)` | Start the stream. The driver owns its reader thread and appends converted samples directly to `data_model`. |
| `stop_measurement()` | Stop the stream and join the reader thread. |
| `set_input_range(min_volts, max_volts)` | Set the ADC voltage input range (for instruments supporting selectable ranges). |
| `get_adc_settings()` | Properties of the ADC. The authoritative values come from the loaded hardware config (`HardwareManager.get_adc_settings()`: bits, effective input range, channel count); the archived snapshot is used when re-loading a measurement. |

> The concrete driver implementations (Pico, PIC) are being reworked; their
> protocols will be documented separately once final. `microcontroller_api.md` and
> `daq_architecture.txt` in this folder describe outdated protocols and folder
> layouts and should not be relied upon.

### 2.2 Models, concurrency and performance

- `DataModel` holds the measurement arrays and a thread-safe tuple buffer. The
  driver's reader thread appends raw samples; a 50 ms `QTimer` in the UI thread
  retrieves all accumulated samples with an O(1) buffer swap, converts them
  (overflow → NaN; debug start/stop → difference), applies the active ROIs, and
  updates histograms and online statistics (Welford/Chan mean and variance,
  successive and cross-channel covariance).
- `SessionModel` holds configuration and metadata (timing, channel mode, debug
  mode, calibration, workspace, measurement metadata) and emits Qt signals on
  changes.
- Heavy computation (statistics, bootstrap, FFT) is vectorised with numpy and, for
  the bootstrap, parallelised with a thread pool.

### 2.3 Statistics (core/calc_stat.py)

Four estimators are computed over the (ROI-filtered) data, each with uncertainties
(analytical, or circular block bootstrap with automatically chosen block length):

- `var` — variance method: mean, variance, ȳ_D = var/mean.
- `var_cov` — successive covariance method: statistics on alternating samples,
  ȳ_D = (var − cov)/mean.
- `var_autocov` — lag-1 autocovariance method: ȳ_D = (var − γ₁)/mean.
- `var_doublecov` — cross-channel covariance method (two detectors only).

## 3. User workflow

1. **Workspace (optional, once per campaign).** Before a measuring campaign the
   user creates a `setup.json` via *Workspace Settings*. It contains the
   information shared by all measurements of the campaign (facility, beam type,
   ADC settings, capacitor base values, hardware IDs, shift note). Its folder
   becomes the *active workspace*: measurements are saved there, and orphaned
   temporary files from a crash are offered for recovery on workspace load.
2. **Wizard.** *Welcome* → **Step 1**: choose the operation mode (ME or SE) and
   connect the hardware. Proceeding requires a measurement setup: a workspace
   whose linked hardware configuration has been loaded (or simulation).
   Connection is driven by the loaded hardware configuration JSON. On Linux each device gets a persistent name via a **udev rule — one rule
   per device** (four in the general case: timing µC, two gain µCs, readout
   system), and the app **auto-connects** to all configured devices at startup or
   on demand (auto-connect button). When auto-connect fails — e.g. on Windows, a
   device is not connected, or the udev rules are missing — a **manual-connect
   dialog** opens, letting the user map each configured device to an available
   connection. If **Single Event** is chosen, the electronics is switched to SE
   and the app shows a static waiting screen (with a button to switch back to ME);
   no data is collected. If **Multi Event** is chosen, the user proceeds to
   **Step 2**.
3. **Timing, gain and metadata configuration.** A physics planning page (Bragg /
   SOBP curves from FLUKA data, gas multiplication factor g, integration-time and
   measurement-time limits) helps choosing the integration time; the setup page
   lets the user set integration time, reset time, and (if declared configurable in
   the hardware JSON) period. The active channel count is governed directly by the
   configuration's `channels` section, and acquisition operates strictly in debug
   mode (recording start and stop samples). A live summary card displays the *actual*
   (quantized) hardware times ($T_{\text{int}}$, $T_{\text{rst}}$, $T_{\text{period}}$,
   $f_{\text{sample}}$). Gain selection supports independent and shared GPIO topologies:
   choosing CH1 dynamically filters compatible CH2 selections, and ganged pins are
   badged. Ramp sampling is available if supported by the timing controller and
   not mutually exclusive with acquisition. "Apply Configuration" atomically applies
   the merged pin states to `gain_ch1`/`gain_ch2` (updating each physical device once),
   configures ADC input range, and sends the timing parameters and active channel count
   to the `timing` role *before* any measurement starts.
4. **Measurement.** `ARM` then `START` (at least 3 s apart). `ARM` triggers an
   integrator reset via the `timing` controller to clear residual charge. Live dashboard:
   histograms (MCA), time series, rolling statistics table, FFT / moving average,
   interactive time ROIs and channel ROIs, calibration tool, metadata editing.
   `STOP` ends the acquisition.
5. **Save.** Each measurement is stored as a ZIP archive (§4.2) in the workspace.
   During the run the data is already streamed to a temporary CSV for crash
   recovery; saving packages it with the settings JSON.
6. **Offline analysis.** "Analyze Offline Data" opens a workspace browser listing
   all measurement ZIPs with metadata filters, or a single ZIP/CSV can be loaded.
   ROIs stored in the archive are re-applied at load.

A simulation mode (`python app.py -s`, or the *Simulate* button) replaces the
hardware with synthetic data for testing.

## 4. File formats

### 4.1 setup.json (workspace)

Campaign metadata plus the link to the hardware configuration; everything
hardware (ADC settings, capacitors, wiring, firmware) lives in the hardware
config file itself, which is copied into the workspace folder at creation.
The full workflow (including the sha256 check and archive embedding) is
documented in [HARDWARE_CONFIG.md](HARDWARE_CONFIG.md):

```json
{
    "versions": { "app_version": "..." },
    "experimental_setup": { "facility": "...", "beam_type": "...", "shift_note": "..." },
    "hardware": {
        "config_file": "hardware_config.json",
        "config_sha256": "…"
    }
}
```

Loading a workspace verifies the sha256 of the linked config and warns on
mismatch (edited outside the app). Old-format setup.json files load with their
hardware fields ignored.

### 4.2 Measurement archive (format version "3.0")

Each measurement is a self-contained ZIP with four members:

- **`data.csv`** — the **raw** measurement data (ROIs are *not* applied; they are
  stored in the JSON and re-applied at load time):
  - operational mode: `timestamp,ch1,ch2` (the two columns hold the start/stop
    differences of each channel);
  - debug mode: `timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop`;
  - `timestamp` is the time in seconds since the start of the measurement (sample
    index divided by the sample frequency); samples that overflowed the ADC are
    stored as NaN.
- **`settings.json`** — everything about the measurement:
  `file_format_version`, `versions`, `measurement_info` (ISO timestamps,
  `acquisition_mode`, `is_problematic` flag), `experimental_setup` (facility, beam type/energy/current,
  depth, detector voltage, calibration, notes), `hardware_settings` — a frozen
  snapshot of the hardware: ADC bits and applied input range, per-channel applied
  `gpio_mask` and total `capacitance_pF`, the resolved hardware summary and the
  config sha256 — `timing_settings` (integration/reset/period/
  sample frequency), `analysis` (ROIs, bootstrap metadata, results of all four
  estimators). The hardware section is read-only for the offline analysis; a
  consistency check on load verifies mask ↔ capacitance against the config.
- **`hardware_config.json`** — verbatim copy of the config used for this run, so
  the archive interprets itself without any external file.
- **`setup.json`** — verbatim copy of the campaign setup.

Both provenance copies are already written to the workspace temp files at
measurement **start** (not only at save), so a crashed run recovers with full
provenance. Pre-3.0 archives load as before (their hardware provenance is
"unknown"); embedded copies always take precedence over the current workspace.

### 4.3 Legacy CSV (format version 1)

Plain CSV files with the timing parameters encoded in the header line
(`integration time [s]: …`, `reset time [s]: …`, `sample frequency [Hz]: …`) or a
simple `timestamp…` header; 3 or 5 data columns; the overflow sentinel is 32768.
Loaded for offline analysis only.

### 4.4 Ramp samples

Not saved yet: the timing triggers for ramp sampling can be configured, but the
acquisition and storage of the ramp samples is future work (§6).

## 5. Intended behaviour vs. current code — biggest differences

The hardware layer now works as described above (validated config, driver
registry, role routing, gain tables from wiring, archives with embedded
provenance). The most important remaining deviations (full list with
severities in [BUG.md](BUG.md)):

| # | Area | Intended | Current | Bug |
|---|---|---|---|---|
| 1 | Connection handling | per-device auto-connect (one udev rule per device) with manual fallback | connect-all only; a manual dialog maps one device at a time; `verify_connection` still unused | B2 |
| 2 | ADC bit depth in `DataModel` | histogram bins/overflow derived from the ADC bits | bits/range flow from the config into the archives, but `DataModel` still uses fixed 12-bit bins | B6 |
| 3 | Ramp sampling | ramp samples acquired and stored | timing triggers only; acquisition side not implemented (deferred by design, not a bug) | — |
| 4 | Dead legacy code | removed | superseded drivers in `services/`, unused ACF/PACF, `if False:` block remain | B17 |

## 6. Open design points

- **Simulation config**: a `hardware_config.json` whose devices use a
  `simulation_*` driver code, so simulation runs through the same config/roles
  path as real hardware.
- **NI (or other) acquisition driver**: `IDataAcquisition` implementation
  registered e.g. as `ni_daqmx_v1` (multi-range ADC, `adc_input` per channel),
  while a µC keeps the `timing` role.
- **Per-channel event mode** (ME on one detector, SE on the other): future
  extension of `set_event_mode`.
- **Ramp sampling acquisition**: reader and storage for ramp samples (the timing
  side is ready).
- **Connection verification**: whether the `verify_connection` (`V`) handshake
  is part of the connect flow or is removed — to be decided.
- **Debug-only run path**: the application always acquires in debug mode (start/stop
  recorded). Operational mode and single-channel toggles have been removed from the
  setup UI: channel count is governed by the hardware config, and acquisition operates
  strictly in debug mode. Archives record `measurement_info.acquisition_mode = "debug"`.
  Old 3-column operational files remain supported by the offline loader.
- **Multi file analysis**: the user can analyze multiple measurements at the same
  time (e.g. create a graph of the yD against depth or beam current).
- **Oversampling**: ADC oversampling could be used to reduce noise.
- **Concrete driver documentation**: the Pico/PIC implementations will be modified;
  their serial protocols will be documented when the firmware sources are final
  (`microcontroller_api.md` / `daq_architecture.txt` are outdated).
- **FFT analysis**: Add FFT analysis custom window for the result panel.

## 7. Running and building

```bash
python app.py [-s | --simulate] [-b | --baud-rate N] [-c | --single-channel] [--debug]
python build.py          # PyInstaller bundle
sudo ./setup_udev_rules.sh   # persistent device names, e.g. /dev/pico_daq
```

Dependencies: see `pyproject.toml` (PyQt6, pyqtgraph, numpy, pandas, pyserial,
arch, statsmodels).
