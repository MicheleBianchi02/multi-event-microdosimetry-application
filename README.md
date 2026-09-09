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

### 2.1 Hardware abstraction — interfaces ("Rules")

The application never talks to concrete hardware directly. It talks to **roles**,
defined in `hardware/interfaces/` and assigned to physical devices in
`hardware_config.json`:

- `timing` — one microcontroller generating the timing signals (integration, reset,
  and optionally ramp sampling triggers).
- `gain_ch1`, `gain_ch2` — one gain controller per channel (may be the same physical
  device, or two different ones).
- `acquisition` — the ADC readout system (a microcontroller or e.g. a NI instrument).

The most general setup is therefore: one µC for the timing, two µC for the gains of
the two channels, and one readout system for the acquisition. The same physical
device may fulfil multiple roles, and conversely a single µC may drive the gain
circuitry of both channels (the same GPIO can be routed to one capacitor on each
circuit). A measurement can also use a single channel only. Devices do not have to
share a transport: the µCs typically use simple serial ports, while the readout
system may use a different USB/instrument protocol (e.g. NI); the transport is
encapsulated by the concrete driver of each device class.

Roles are mapped in `hardware_config.json`:

```json
{
    "devices": {
        "pico_1": { "class": "PicoSerialDAQ", "port": "/dev/pico_daq" }
    },
    "roles": {
        "acquisition": "pico_1",
        "timing": "pico_1",
        "gain_ch1": "pico_1",
        "gain_ch2": "pico_1"
    },
    "gain_settings": {
        "ch1": {
            "fixed_capacitor_pF": 22.0,
            "switchable_capacitors": [ { "value_pF": 100.0 }, ... ]
        },
        "ch2": { ... }
    }
}
```

The gain table (all valid capacitance combinations and their labels) is computed
dynamically from `gain_settings`. In the future this section will also describe
**which GPIO pin is connected to which capacitor** on each channel, and the gain
table will be derived from that routing (schema still to be defined, see §6).

#### ITimingController (`hardware/interfaces/timing.py`)

| Method | Contract |
|---|---|
| `supports_ramp_sampling` | Whether the controller can generate ramp sampling triggers. Gates the ramp UI. |
| `configure_noramp(int_time_us, reset_time_us)` | Configure integration and reset times for a standard measurement. |
| `configure_ramp(ramp_dt_us, int_time_us, reset_time_us)` | Configure ramp sampling: trigger interval, integration and reset times, all in one call. |
| `calculate_actual_timing(int_time_s, reset_time_s, ...)` | Return the *actual* (quantized) integration and reset times the hardware will use, for a target pair of times. |
| `get_timing_limits()` | Physical limits (min/max integration time, min ramp dt). |

#### IGainController (`hardware/interfaces/gain.py`)

| Method | Contract |
|---|---|
| `set_gpio_mask(channel, gpio_mask)` | Apply a per-channel mask of GPIO states, each bit switching a capacitor in/out. |
| `num_gpios_per_channel` | How many GPIO pins this controller can manipulate per channel. The offered gain combinations must be constrained by this (and by the circuit definition). |
| `set_event_mode(is_single_event)` | **Intended contract:** the SE/ME line of the electronics is owned by the *gain* controllers. When the user selects SE (or switches back to ME), `set_event_mode` is called on **both** gain controllers, so both electronics receive the signal. A future extension will allow ME on one detector and SE on the other (per-channel event mode). *Note: the current code does not work like this yet — see §5 / BUG.md (B5).* |

#### IDataAcquisition (`hardware/interfaces/acquisition.py`)

| Method | Contract |
|---|---|
| `arm()` | Prepare the device for measurement (no-op if unsupported). |
| `start_measurement(session_model, data_model)` | Start the stream. The driver owns its reader thread and appends converted samples directly to `data_model`. |
| `stop_measurement()` | Stop the stream and join the reader thread. |
| `get_adc_settings()` | Properties of the ADC (e.g. number of bits, Vref, oversampling). **Intended to be the single source of truth for the ADC bit depth**, which drives histogram bins, the overflow sentinel and value validation. *Currently the bit depth is hardcoded instead — see §5 / BUG.md (B6).* |

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
   connect the hardware. Connection is driven by the loaded hardware configuration
   JSON. On Linux each device gets a persistent name via a **udev rule — one rule
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
   measurement-time limits) helps choosing the integration time; the manual page
   lets the user set integration and reset time (the app shows the *actual* times
   the hardware will use), the operational mode (debug: start/stop stored
   separately; operational: only the difference), single/dual channel, the gain of
   each channel (from the computed gain table), the ramp sampling option (only if
   the timing controller supports it) and the per-measurement metadata (beam
   energy/current, detector voltage, depth, note). "Apply Configuration" sends the
   timing to the `timing` role and the gain masks to the `gain_ch1`/`gain_ch2`
   roles *before* any measurement starts.
4. **Measurement.** `ARM` then `START` (at least 3 s apart). Live dashboard:
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

Created once per campaign; loaded for every session of that campaign.

```json
{
    "versions": { "app_version": "...", "firmware_version": "...",
                  "hardware_id_ch1": "...", "hardware_id_ch2": "..." },
    "experimental_setup": { "facility": "...", "beam_type": "...", "shift_note": "..." },
    "hardware_settings": {
        "adc_num_bits": 12, "adc_vref_volts": 5.0,
        "channels": {
            "ch1": { "capacitor_base_values_pF": [22.0, 100.0, 560.0, 3300.0] },
            "ch2": { "capacitor_base_values_pF": [ ... ] }
        }
    }
}
```

### 4.2 Measurement archive (format version "2.0")

Each measurement is a ZIP containing two files:

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
  `is_problematic` flag), `experimental_setup` (facility, beam type/energy/current,
  depth, detector voltage, calibration, notes), `hardware_settings` (ADC settings,
  `single_channel_mode`, per-channel gain — target format: applied `gpio_mask` and
  total `capacitance_pF`, see §5/B4), `timing_settings` (integration/reset/period/
  sample frequency), `analysis` (ROIs, bootstrap metadata, results of all four
  estimators).

### 4.3 Legacy CSV (format version 1)

Plain CSV files with the timing parameters encoded in the header line
(`integration time [s]: …`, `reset time [s]: …`, `sample frequency [Hz]: …`) or a
simple `timestamp…` header; 3 or 5 data columns; the overflow sentinel is 32768.
Loaded for offline analysis only.

### 4.4 Ramp samples

Not saved yet: the timing triggers for ramp sampling can be configured, but the
acquisition and storage of the ramp samples is future work (§6).

## 5. Intended behaviour vs. current code — biggest differences

The sections above describe the **intended** behaviour. The most important points
where the current code does not match yet (full list with severities in
[BUG.md](BUG.md)):

| # | Area | Intended | Current | Bug |
|---|---|---|---|---|
| 1 | Saved CSV | raw data; ROIs re-applied at load from settings.json | ROI-**filtered** data is written to the CSV, so out-of-ROI samples are lost in the archive | B1 |
| 2 | Gain persistence | settings.json stores the applied GPIO mask and total capacitance per channel; restored on load | actual gain is not exported (stale `capacitor_code`); charge scaling after load falls back to defaults | B4 |
| 3 | SE/ME switching | `set_event_mode` called on **both gain controllers** | sent to the *acquisition* device only (works today only because one device holds all roles) | B5 |
| 4 | ADC bit depth | dynamic, from `IDataAcquisition.get_adc_settings()` | hardcoded 12 bit / 4096 bins / sentinel 32768 everywhere | B6 |
| 5 | Connection handling | JSON-driven auto-connect (one udev rule per device), per-device manual dialog on failure, failures reported | failures always reported as success; manual mapping passes the port name instead of the device path | B2, B3 |
| 6 | Gain capability matching | offered gain combinations constrained by the µC GPIO count (`num_gpios_per_channel`) | gain table built from the circuit definition only; capability property unused | B7 |
| 7 | Ramp sampling | ramp samples acquired and stored | timing triggers only; acquisition side not implemented (deferred by design, not a bug) | — |

## 6. Open design points

- **GPIO→capacitor mapping schema** for `hardware_config.json` (which pin is wired
  to which capacitor on each channel, including one µC driving both circuits): not
  decided yet; the gain table computation will be based on it once defined.
- **Per-channel event mode** (ME on one detector, SE on the other): future
  extension of `set_event_mode`.
- **Ramp sampling acquisition**: reader and storage for ramp samples (the timing
  side is ready).
- **Connection verification**: whether the `verify_connection` (`V`) handshake is
  part of the connect flow or is removed — to be decided.
- **NI (or other) acquisition driver**: `IDataAcquisition` implementation for an
  external readout system, while a µC keeps the `timing` role.
- **Concrete driver documentation**: the Pico/PIC implementations will be modified;
  their serial protocols will be documented when the firmware sources are final
  (`microcontroller_api.md` / `daq_architecture.txt` are outdated).
- **Multi file analysis**: the user can analyze multiple measurement at the same time 
  (e.g. create a graph of the yD against depth or beam current).
- **Oversampling**: To reduce noise ADC oversampling could be used.

## 7. Running and building

```bash
python app.py [-s | --simulate] [-b | --baud-rate N] [-c | --single-channel] [--debug]
python build.py          # PyInstaller bundle
sudo ./setup_udev_rules.sh   # persistent device names, e.g. /dev/pico_daq
```

Dependencies: see `pyproject.toml` (PyQt6, pyqtgraph, numpy, pandas, pyserial,
arch, statsmodels).
