# ATOMIQA 3.0

Data acquisition and analysis application for microdosimetry measurements with the
variance–covariance method, applied to proton and carbon ion therapy beams.

## Documentation

- [HARDWARE_CONFIG.md](HARDWARE_CONFIG.md) — the hardware configuration file (structure,
  editor, workflow) and the output-file reference.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the application's architecture: MVC
  layering, backend (models, services, core, hardware abstraction) in detail, concurrency
  model, data-flow walkthroughs.
- [docs/PICO/](docs/PICO/) — Pico 2 DAQ (PICO2DAQ): serial-protocol and pin-map reference
  ([README.md](docs/PICO/README.md)), firmware internals + the `pico_serial_v1` driver
  ([FIRMWARE_AND_DRIVER.md](docs/PICO/FIRMWARE_AND_DRIVER.md)), firmware sources and
  datasheets.
- [docs/PIC/](docs/PIC/) — PIC18F8723: serial-protocol and pin-map reference
  ([README.md](docs/PIC/README.md)), firmware internals + the `pic_serial_v1` driver
  ([FIRMWARE_AND_DRIVER.md](docs/PIC/FIRMWARE_AND_DRIVER.md)), firmware sources,
  datasheets and the (older) code review.
- [docs/NI/DRIVER.md](docs/NI/DRIVER.md) — NI USB-6451: how the simultaneous-sampling
  digitizer works and the `ni_daqmx_v1` driver in detail.

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
| `services/` | `HardwareService`, `DataExporter` (ZIP archives), `ShiftService` (workspace setup.json, crash recovery), `AnalysisService` (FFT, moving average, physics façade), multi-measurement analysis (`measurement_catalog` workspace scan, `variable_catalog`, `multi_analysis_service` pipeline) |
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
collected) without real readout hardware. The start/stop still go to the real
timing device when it does not hold the acquisition role, so its timing
signals run on the bench while nothing is recorded.

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

> **Window markers (split hardware).** When the timing device does not hold
> the acquisition role (e.g. the Pico times while an NI card acquires) and
> ramp sampling is *not* requested, the manager routes the configuration as
> a degenerate ramp — dt = integration time, i.e. two CONV_START edges per
> window (integration start/end) on the trigger output — so the external
> digitizer sees the integration boundaries. The true-values preview, the
> applied configuration and the firmware's `Q` all report the same numbers
> (driver mirror), and the archive records the choice in
> `timing_settings.timing_mode` (`internal` / `ramp` / `markers`).

#### IGainController (`hardware/interfaces/gain.py`)

| Method | Contract |
|---|---|
| `set_gpio_mask(gpio_mask)` | Apply the state of **every declared pin of this device**, one bool per pin in the config's pin order. Which pin switches which capacitor is routing knowledge resolved by the `HardwareManager` from the config's connections. Masks of the wrong length are rejected. |
| `set_event_mode(is_single_event)` | The SE/ME line of the electronics is owned by the *gain* controllers. Switching mode calls this on **all** unique gain controllers (via `HardwareManager.set_event_mode`), so every circuit receives the signal (low = ME, high = SE today). A future extension will allow ME on one detector and SE on the other (per-channel event mode). |

#### IDataAcquisition (`hardware/interfaces/acquisition.py`)

| Method | Contract |
|---|---|
| `start_measurement(session_model, data_model, ramp_sink=None)` | Start the stream. The driver owns its reader thread and appends converted samples directly to `data_model`. `ramp_sink` is non-None only in ramp/markers timing mode: the driver then pushes blocks of normalized uint32 samples (raw ADC counts, with absolute sample indices) to it from its reader thread (protocol: `hardware/interfaces/ramp_sink.py`); drivers that cannot stream ramp samples refuse to start. |
| `stop_measurement()` | Stop the stream and join the reader thread. |
| `configure_channels(channel_plan)` | Optional: `(channel_id, termination)` pairs for the active channels, derived from the config by the `HardwareManager`. No-op on drivers with fixed firmware inputs; instrument drivers map the pairs onto physical input terminals. |
| `set_input_range(min_volts, max_volts)` | Set the ADC voltage input range (for instruments supporting selectable ranges). |
| `get_adc_settings()` | Properties of the ADC. The authoritative values come from the loaded hardware config (`HardwareManager.get_adc_settings()`: bits, effective input range, channel count); the archived snapshot is used when re-loading a measurement. |

> The **NI driver** (`ni_daqmx_v1`, `hardware/ni/ni_daqmx.py`) acquires on a
> NI USB-6451 (20-bit, 1 MS/s per channel simultaneous): every conversion
> happens on a rising edge of the external **CONV_START** signal from the
> timing device (PFI0, hardcoded), so "N = T_int/dt + 1 samples per
> integration window" is a hardware guarantee — the driver only makes sense
> in `ramp`/`markers` mode and refuses to start without a `ramp_sink`.
> Start ordering is an alignment guarantee: `start_measurement` arms the
> task before returning, and only then does the manager send the timing
> 'S' — the 6451 ignores clock edges while not acquiring, so arming first
> makes the first CONV_START edge after 'S' the first sample of window 0.
> `connect()` sets the shared digital logic family to 3.3 V (the 5 V
> default's guaranteed V_IH of 3.66 V would leave the Pico's clock out of
> spec), reads use rate-adapted blocks with a bounded timeout, and stop
> drains the DAQmx buffer before the task is stopped — no measurement rows
> are stranded. The
> config selects per channel only the *termination* (`differential` /
> `rse`); the input terminals are fixed hardware facts hardcoded in the
> driver (ch1 diff = AI0/AI8, ch2 diff = AI1/AI9, ch1 SE = AI2, ch2 SE =
> AI3 — four different ADCs, so any mix is simultaneous). Readings outside
> the applied range simply clamp: railing at ±full scale is the overflow
> indication. The DAQmx device name lives in the config
> (`connection.device`, e.g. `NI6451`); the `nidaqmx` package is imported
> lazily so machines without it still load configs (the driver tests run
> against a fake `nidaqmx` module; live validation needs the DAQmx runtime
> and the card).

> The Pico driver (`pico_serial_v1`) implements the **PICO2DAQ 2.1**
> firmware protocol, documented in
> [raspberry_copy/README.md](raspberry_copy/README.md) (commands, replies,
> binary frame, end-of-stream marker, pin map, validated limits) — the same
> reference lives in [docs/PICO/README.md](docs/PICO/README.md), next to the
> firmware sources; the firmware internals and the driver are described in
> depth in [docs/PICO/FIRMWARE_AND_DRIVER.md](docs/PICO/FIRMWARE_AND_DRIVER.md).
> Its `connect()` runs the `V` identity handshake as a liveness check (wrong
> firmware or a system clock other than 150 MHz refuses the connection),
> and its `calculate_actual_timing` mirrors the firmware's truncation of
> the times to whole 150 MHz cycles. The PIC implementation still speaks
> the legacy protocol; its firmware and driver are now documented in
> [docs/PIC/README.md](docs/PIC/README.md) and
> [docs/PIC/FIRMWARE_AND_DRIVER.md](docs/PIC/FIRMWARE_AND_DRIVER.md).
> `microcontroller_api.md` and `daq_architecture.txt` in this folder describe
> outdated protocols and folder layouts and should not be relied upon.

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
- `var_fft` — frequency-domain method, computed **only on explicit user
  request** from the *FFT Analysis…* dialog in the results panel (values stay
  zero otherwise): the white-noise floor of the one-sided PSD is measured as
  the mean over the frequency bins kept by the user — the shaded regions the
  user draws mark the **excluded** bands, and the floor is measured on their
  complement (optionally clipped at a movable threshold line) —, the variance
  is the PSD integral over the *whole* spectrum (Parseval-exact, Welch
  included), the covariance is everything above the white floor (peaks,
  coloured excess and slow drift), and ȳ_D = floor·f_Nyquist/mean.
  Periodogram or Welch, rectangular or Hann window — the plot always shows
  exactly the spectrum the calculation integrates. Peaks above the threshold
  are listed per channel in ascending frequency; every peak starts
  **unchecked**, and only the ones the user identifies as real are archived.
  Uncertainties are not implemented yet (0). The results are stored in
  `analysis.results` of the archive and the
  dialog parameters (excluded bands, thresholds, method, window, nperseg)
  plus a floor/peaks diagnostic snapshot in `analysis.fft`.

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
   Connection is driven by the loaded hardware configuration JSON and is
   **button-driven** — nothing connects automatically at startup or on config
   load. The step shows a *connection card* with one status row per configured
   device and three actions:
   - **Auto Connect** opens every device's persistent name — `/dev/atomiqa/<device_id>`,
     derived from the config, which itself carries no port (the file stays
     machine-independent). On Linux the names are created by **udev rules —
     one rule per device** (four in the general case: timing µC, two gain
     µCs, readout system; see §5.1 of HARDWARE_CONFIG.md), each matching the
     board by USB serial number. When the rules are missing, a device is
     unplugged or the ports do not exist (e.g. on Windows), the attempt
     fails fast and the *manual-connect dialog* opens automatically with the
     per-device errors.
   - **Manual Connect…** opens the same dialog on demand: one row per
     configured *device* (not per role — one board may hold several roles),
     an available-port combo per serial device (ports taken by another row are
     hidden; the udev names are not listed, they belong to the auto path),
     per-row Connect and Connect All with live status. Virtual devices need
     no port. The mapping is **session-only**: nothing is written back to the
     hardware configuration. On Windows this is the normal way to connect.
   - **Simulate** replaces the hardware with synthetic data.

   "Connected" means the port opened; the wizard gate opens only when every
   role-assigned device is connected (or simulation is active). If **Single
   Event** is chosen, the electronics is switched to SE and the app shows a
   static waiting screen (with a button to switch back to ME); no data is
   collected. If **Multi Event** is chosen, the user proceeds to **Step 2**.
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
   **Multi-Measurement Analysis** (button on the same page, or *Trend…* in the
   running window) opens a non-modal window that plots every measurement of the
   workspace together: custom formulas on both axes built from a searchable
   catalogue of variables (per estimator and channel — each method lists only
   the parameters it actually computes — plus metadata, timing, gain, `index`
   and `timestamp`), combined with `+ - * / **` and parentheses; uncertainties
   propagate to first order and render as error bars. A *units* box switches
   the result variables between raw channels (counts) and charge (pC, converted
   with each measurement's recorded capacitance/ADC range) — the picker shows
   one variant at a time and switching rewrites the open formulas. Several Y
   expressions can be plotted at once (one per line, one colour each) to
   compare estimators; a record is plotted only where *every* expression is
   valid, keeping the series comparable. Measurements are filtered (depth,
   current, energy, detector voltage, integration time, ⚠ flag) and
   individually (de)selected; a *colour-by* variable splits the points into
   labelled series (e.g. one colour per integration time; disabled for
   multi-Y, where colour encodes the expression). The current, possibly still
   running measurement can be included as a distinct open-symbol point and
   refreshed on demand, to catch anomalies during a shift. Formulas can be
   saved as presets in the workspace (`analysis_presets.json`) and the computed
   table exported to CSV. A new estimator appearing in the results dictionary
   is picked up automatically; when raw-counts variables are plotted across
   measurements with different gains/ADCs, the dialog suggests the charge-unit
   variants.

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
  sample frequency), `analysis` (ROIs, bootstrap metadata, results of all five
  estimators, and — when the user ran the FFT analysis — the `fft` block with
  its parameters and a floor/peaks diagnostic snapshot). The hardware section
  is read-only for the offline analysis; a
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

### 4.4 Ramp samples (file format AQR1, version 1)

When a measurement runs with ramp sampling enabled, the ramp
stream bypasses `DataModel` almost entirely: the acquisition driver (or the
simulator) pushes blocks of normalized **uint32** ADC counts per channel into
a `RampStorageService` (`services/ramp_storage.py`), whose single writer
thread appends them to one binary file per channel (fixed 4096-byte header,
then raw little-endian uint32; sequential writes — benchmarked far above the
8 MiB/s spec), keeps a rolling sha256 of the data section, refreshes the
`samples_written` header watermark about once per second (crash recovery),
and chunks the stream into integration windows (N = T_int/dt + 1, exact).
The **first and last sample of every window** feed the classic start/stop
pipeline unchanged, always as two-channel tuples — a single-detector run
fills the absent channel with the overflow sentinel, exactly like the µC
drivers. Ramp data is monitoring-only: the variance–covariance
analysis never sees it.

**Markers mode (ramp sampling *not* requested on split hardware) writes no
files at all**: the timing device emits the two integration-boundary edges
per window (dt = T_int), the same window extraction turns them into the
classic start/stop pairs (`RampPairExtractorService`, in memory), and those
land in `data.csv` exactly like a µC-run's debug stream — binary files would
only duplicate `data.csv`, so none are created and the archive carries no
`ramp` section.

At save time the temp files (`.active_run.ramp.*` in the workspace) move into
the campaign's ramp folder (existence-checked, suffixed `_1`, `_2`… on name
collision):

```
workspace/
├── measurement_xxx.zip          (unchanged; settings.json gains a "ramp"
│                                 section referencing the folder + sha256s)
└── ramp/measurement_xxx/
    ├── ch1.bin, ch2.bin         (header + raw uint32 samples)
    └── index.json               (per-channel totals, gaps, uuid)
```

Each header records the actual timing (dt, period, N), ADC bits and input
range, timing mode, driver/firmware, the hardware-config sha256, a
measurement uuid shared by the two channels, the sample watermark, a
finalized/aborted flag and (once finalized) the data sha256. Reading is
`np.memmap(path, dtype="<u4", offset=4096)` via `open_ramp_file()`. A window
lost to a dropped block is recorded as a gap (whole-block granularity) and
never silently misaligned. Crash recovery trims each file to its last whole
window at the watermark (`recover_ramp_file`, wired into the workspace
recovery flow).

The custom monitoring/FFT UI for these files is future work (§6).

## 5. Intended behaviour vs. current code — biggest differences

The hardware layer now works as described above (validated config, driver
registry, role routing, gain tables from wiring, archives with embedded
provenance). The most important remaining deviations (full list with
severities in [BUG.md](BUG.md)):

| # | Area | Intended | Current | Bug |
|---|---|---|---|---|
| 1 | ADC bit depth in `DataModel` | histogram bins/overflow derived from the ADC bits | done — `SessionModel.adc_bits` (config / archive) drives bins, sentinel and UI scaling everywhere | B6 (fixed) |
| 2 | Ramp sampling | ramp samples acquired and stored | app side done (AQR1 storage §4.4, window extraction, simulation, save/recovery); the real streaming acquisition driver (NI) is future work | — |
| 3 | Dead legacy code | removed | superseded drivers in `services/`, unused ACF/PACF, `if False:` block remain | B17 |

## 6. Open design points

- **Simulation config**: a `hardware_config.json` whose devices use a
  `simulation_*` driver code, so simulation runs through the same config/roles
  path as real hardware.
- **NI (or other) acquisition driver**: *implemented* — `ni_daqmx_v1`
  (USB-6451: external CONV_START sample clock on PFI0, fixed input terminals
  keyed by the channel `termination`, ±0.2/±2.5/±5/±10 V ranges, 20-bit
  counts streamed into the ramp sink; ramp dt validated against the 1 MS/s
  per-channel limit at Apply Configuration). Tested against a fake
  `nidaqmx` module; still open: validation with the real card + DAQmx
  runtime on the measurement PC.
- **Per-channel event mode** (ME on one detector, SE on the other): future
  extension of `set_event_mode`.
- **Ramp sampling acquisition**: *app side done* — the AQR1 streaming storage
  (§4.4), the window extraction feeding the classic pipeline, the simulated
  ramp source, save/crash-recovery and the `ramp_sink` interface exist and are
  tested, and the NI driver streams into the sink (see above). Still open:
  validation with real hardware, and the monitoring/FFT UI reading the files
  via `np.memmap`.
- **Device identity check**: *liveness done* — the PICO2DAQ 2.1 firmware's
  `V` handshake returns a unique per-board id and the driver verifies
  firmware + clock on every connect (wrong board or bare Pico refuses the
  connection). Still open: matching the board id against the hardware
  config (per-board entries) so auto-connect confirms it opened exactly the
  configured board and the manual dialog can suggest mappings.
- **Debug-only run path**: the application always acquires in debug mode (start/stop
  recorded). Operational mode and single-channel toggles have been removed from the
  setup UI: channel count is governed by the hardware config, and acquisition operates
  strictly in debug mode. Archives record `measurement_info.acquisition_mode = "debug"`.
  Old 3-column operational files remain supported by the offline loader.
- **Multi file analysis**: *done* — the multi-measurement analysis window
  (§3.6) plots any archived quantity against any other with custom formulas on
  both axes. Still open for later: more formula functions (`sqrt`, `log`, …),
  unit-aware axis labels for combined formulas, calibrated (physical-unit)
  variables once the calibration workflow is settled, and batch re-analysis
  from raw `data.csv`.
- **Oversampling**: ADC oversampling could be used to reduce noise.
- **Concrete driver documentation**: *done* — the PICO2DAQ protocol
  (commands, binary frame, pin map, limits, timing models) is documented in
  `raspberry_copy/README.md` (= [docs/PICO/README.md](docs/PICO/README.md))
  and mirrored by the driver; both µC firmwares and all three drivers are
  described in depth under [docs/](docs/) (PICO, PIC, NI). The PIC still
  speaks the legacy protocol; `microcontroller_api.md` /
  `daq_architecture.txt` are outdated and should not be relied upon.
- **FFT analysis uncertainties**: the `var_fft` estimator currently reports 0
  for its uncertainties; a bootstrap or propagation scheme is future work.

## 7. Running and building

```bash
python app.py [-s | --simulate] [-b | --baud-rate N] [-c | --single-channel] [--debug]
python build.py          # PyInstaller bundle
sudo ./setup_udev_rules.sh   # persistent device names: /dev/atomiqa/<device_id>
                             # how to write the rules by hand: UDEV_RULES.md
```

Dependencies: see `pyproject.toml` (PyQt6, pyqtgraph, numpy, pandas, pyserial,
arch, statsmodels).
