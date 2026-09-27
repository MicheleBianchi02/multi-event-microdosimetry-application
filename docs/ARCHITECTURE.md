# ATOMIQA 3.0 — application architecture

This document describes how the application itself is built: the MVC
layering, how the backend (models, services, core, hardware abstraction)
works in detail, how the frontend is organized, the concurrency model, and
the data-flow walkthroughs that put the pieces together. The hardware
components are documented separately:

- [README.md](../README.md) — the application from the user's/developer's
  point of view (features, file formats, open points)
- [HARDWARE_CONFIG.md](../HARDWARE_CONFIG.md) — the hardware configuration
  file and the campaign workflow around it (user-facing)
- [PICO/FIRMWARE_AND_DRIVER.md](PICO/FIRMWARE_AND_DRIVER.md) ·
  [PICO/README.md](PICO/README.md) — Pico firmware + `pico_serial_v1`
- [PIC/FIRMWARE_AND_DRIVER.md](PIC/FIRMWARE_AND_DRIVER.md) ·
  [PIC/README.md](PIC/README.md) — PIC firmware + `pic_serial_v1`
- [NI/DRIVER.md](NI/DRIVER.md) — USB-6451 + `ni_daqmx_v1`

---

## 1. The layering

```
┌──────────────────────────────────────────────────────────────────┐
│ views/            PyQt6 + pyqtgraph UI (no threads, no I/O)      │
│   main_window (QStackedWidget)                                   │
│   ├── wizard: welcome → step1 → step2 → import / SE-wait         │
│   └── running window: live dashboard, ROIs, FFT, dialogs         │
├──────────────────────────────────────────────────────────────────┤
│ controllers/      Qt objects gluing views to models & services   │
│   SetupController          MeasurementController                 │
├──────────────────────────────────────────────────────────────────┤
│ services/         application services (business + I/O)          │
│   HardwareService   DataExporter   ShiftService                  │
│   RampStorageService / RampPairExtractorService                  │
│   AnalysisService   measurement_catalog   variable_catalog       │
│   multi_analysis_service                                         │
├──────────────────────────────────────────────────────────────────┤
│ core/             pure science & parsing (no Qt, no hardware)    │
│   calc_stat   bragg_peak   load_file   data_utils   formula      │
│   constants                                                      │
├──────────────────────────────────────────────────────────────────┤
│ hardware/         hardware abstraction (roles, registry, drivers)│
│   interfaces/ (ITimingController, IGainController,               │
│               IDataAcquisition, IRampSink)                       │
│   core/HardwareManager    config/ (schema, loader, gain model)   │
│   registry.py  →  pico/pic/ni/dummy drivers                      │
├──────────────────────────────────────────────────────────────────┤
│ models/           application state (Qt signals out, no logic)   │
│   SessionModel (config/metadata)   DataModel (data + statistics) │
└──────────────────────────────────────────────────────────────────┘
```

Dependencies point strictly downwards: views know controllers and models;
controllers know models, services and the `HardwareManager`; services know
models and `core`; `core` and `hardware/drivers` know nothing about the UI.
The one deliberate shortcut: the drivers write **directly into
`DataModel`** from their reader threads (the models are the shared state,
see §8).

`SessionModel`/`DataModel` live at the bottom of the drawing because
everything reads them, but conceptually they are the *M* of MVC: plain
state holders that emit Qt signals when configuration changes.

## 2. Composition root (`app.py`)

`main()` configures logging (`--debug` → DEBUG, else WARNING), creates the
`QApplication`, pins the locale to English/US (decimal-point
normalization for all numeric parsing), and builds `AppController`:

1. `SessionModel` — CLI flags applied: `--baud-rate`, `--single-channel`
   (sets `multiple_channel=False`), and `debug_mode = True` **forced**:
   the app always acquires start/stop pairs; operational mode exists only
   in the offline readers for old files.
2. `DataModel`.
3. `HardwareManager` (imported lazily so headless machines without Qt
   hardware deps still import `app.py` for tests).
4. `HardwareService(hardware_manager, data_model, session_model)`.
5. `MainWindow` (which builds the wizard `SetupWindow` and the
   `RunningWindow`), plus wiring: the manager's config listener → the
   connection widget; `session_model.workspace_changed` →
   `AppController._on_workspace_changed` (loads the workspace's linked
   hardware config, verifies the sha256, warns on mismatch); the four
   hardware-config dialog requests (import/edit/new/remove) →
   `AppController` handlers that run `HardwareConfigEditorDialog` and
   (re)link via `ShiftService`.
6. `SetupController` and `MeasurementController`.
7. `simulate_requested` (wizard button or `-s`) flips every relevant
   component into simulation mode — no hardware is touched, the wizard
   gate opens, `HardwareService` generates synthetic data.

There is no global exception handler; errors are surfaced per-operation
through `QMessageBox.critical` in the controllers/app handlers. The
command-line surface is otherwise just `-s/--simulate`, `-b/--baud-rate`,
`-c/--single-channel`, `--debug`.

## 3. Models

### 3.1 `SessionModel` (`models/session_model.py`)

Application-wide configuration and metadata; a pure state holder with Qt
signals, no logic beyond getters/setters:

- **Timing/state**: `int_time`, `reset_time` (s), `period_s` (the driver's
  applied/quantized period; 0.0 = derive as int + reset), `sample_freq`,
  `timing_mode` ∈ {`internal`, `ramp`, `markers`}, `ramp_dt_s`,
  `debug_mode` (always True), `multiple_channel`, `adc_bits`
  (12-bit default; set from the config at Apply and from archives at
  load — drives histogram bins and the overflow sentinel app-wide,
  signal `adc_bits_changed`).
- **Signals**: `mode_changed`, `channel_mode_changed`, `file_path_changed`,
  `workspace_changed(str)`, `adc_bits_changed(int)`.
- **Campaign/metadata**: active workspace dir + setup file name, facility
  / beam type / particle / shift note, per-measurement metadata (beam
  energy/current, depth, detector voltage, calibration slope/intercept,
  notes, `is_problematic`), applied per-channel gain state
  (`gpio_mask_ch1/ch2`, `capacitance_pf_ch1/ch2`), ROIs (channel + time),
  `timestamp_start/end`.
- A legacy `_tx_delay` attribute (ticks) is recomputed from the mode ×
  channel matrix (`core.constants.TX_DELAY_*`) — kept for the old display
  path; the live math lives in the drivers (§7.3).

### 3.2 `DataModel` (`models/data_model.py`)

The measurement data and the online statistics. **Plain object, one Qt
signal-free**, thread-safe by construction (§8): producers append, the UI
thread drains.

**Storage** (grown by doubling, `BUFFER_SIZE = 2¹⁷` initial):

- `data` — converted operational samples *or* debug differences
  (`(N,2)` float);
- `start_data` / `stop_data` — the raw debug start/stop values;
- `filtered_data` — after time-ROI + channel-ROI masking (NaN-padded,
  same shape);
- `bar_data_ch1/ch2` — integer histograms of `2**adc_bits` bins
  (`configure_adc(bits)` reallocates and zeroes them; bounds 1…20 bits);
- `result` — the estimator dictionary
  (`result[macro][param] = [ch1_val, ch1_unc, ch2_val, ch2_unc]`, macros
  `var`, `var_cov`, `var_autocov`, `var_doublecov`, `var_fft`);
- `fft_analysis_params` — the last applied FFT-dialog parameters
  (excluded bands, thresholds, method/window/nperseg); archived in
  `analysis.fft` and restored on load.

**Thread handoff**: drivers/simulator call `append_data(tuple)` /
`append_batch(list)` — an extend of a staging list under one
`threading.Lock`. The UI's 50 ms timer calls `get_latest_batch()`: if the
staging list is non-empty, the *whole list object* is swapped out for a
fresh one under the lock and returned — an O(1) handoff where the writer
never blocks and the consumer drains everything accumulated since the last
tick. All heavy numpy work happens afterwards in the UI thread.

**Conversion pipeline** (`ingest_raw_batch`, called once per tick):

1. `core.data_utils.convert_data_debug` / `convert_data`: the sentinel
   (`overflow_sentinel(bits) = 2**bits`, "one past full scale") → NaN;
   debug: `diff = stop − start`, negative diff → NaN.
2. Append to `start_data`/`stop_data` and `data` (doubling as needed).
3. **Time-ROI filter**: sample-index windows `int(t·f_sample)` → rows
   outside the ROIs → NaN (counted in `n_overflow_*` *before* channel
   filtering, since sentinel rows are NaN).
4. **Channel-ROI filter** (`(channel, low, high)` triples, 1-based
   channel): values outside `[low, high]` → NaN (rows are never dropped).
5. `update_histogram` (NaN dropped, indices clipped into
   `[0, 2**bits)`, `np.bincount` add) and `update_online_statistics`.

**Online statistics** — Chan et al. parallel/pairwise formulas, so batches
combine exactly like single samples (`m2` = variance·(n−1) carried per
aggregate):

- per-channel mean/variance (`mean += Δ·(n_b/n)`, `m2 = m2_a + m2_b +
  Δ²·n_a·n_b/n`);
- successive covariance per channel: odd/even decimation across batch
  boundaries via a one-sample `leftover_*` register, NaN pairs dropped,
  Chan update on the two means and the covariance;
- cross-channel covariance: rows finite in both channels, same Chan form.

`rebuild_accumulators(...)` re-runs the whole pipeline over the stored
history (after ROI edits or an archive load), and `reset_accumulators()`
zeroes everything at measurement start.

## 4. Controllers

### 4.1 `SetupController` (`controllers/setup_controller.py`)

Drives the wizard's hardware-facing pages. Connection is **button-driven**
(`auto_connect_requested` → `manager.connect_all()` with fail-fast errors
feeding the manual dialog; `manual_connect_requested` → `ManualConnectDialog`;
`simulate_requested` is re-emitted to the app). The Bragg/SOBP planning
page is served here: FLUKA data loaded via `AnalysisService` at
construction; handlers plot pristine/SOBP curves, compute the gas
multiplication factor `g` (`calc_g_parameter`), integration-time and
measurement-time limits (`calc_integration_times` + the T_m math with
ε² division), and the per-depth voltage statistics.

**Apply Configuration** (`send_timing_config`) is the critical ordered
sequence — each step fails hard and aborts:

1. `timing.set_mode(True)` — debug mode, always;
2. `timing.set_channels(num_active > 1)` — *before* the timing config,
   because the PIC's timer quantization (TX delay) depends on it;
3. ramp feasibility: reject ramp when the timing device cannot combine it
   with acquisition (`can_ramp_while_acquiring`), and reject
   `1/dt > acquisition_max_rate_hz`; store `session.timing_mode` =
   `manager.timing_mode_for(sample_ramp)`;
4. `manager.configure_timing(...)` — routed to `configure_ramp` /
   degenerate ramp (markers) / `configure_noramp` (§7.4);
5. `manager.apply_gains(ch1, ch2)` — the atomic gain apply (§7.5), then
   session capacitances/masks snapshot;
6. `manager.set_input_range(...)` (acquisition);
7. `session.adc_bits = manager.get_adc_settings()["bits"]`.

In simulation the same sequence runs without hardware; since B34 the
simulate path first calls `manager.validate_timing(...)` — the driver's
mirror as a pure realizability check — so a configuration the mirror
rejects (e.g. an out-of-range dt in markers mode) reports the specific
reason instead of a false "Applied Successfully". Config-less simulation
(no timing controller) remains unvalidated — BUG.md B21.

The **timing preview** (`timing_calculation_requested`) routes through
`manager.preview_timing(...)` — the driver's pure mirror; a mirror
rejection (`ValueError`, e.g. an unrealizable ramp/markers request) is
surfaced verbatim in the wizard instead of being softened.

### 4.2 `MeasurementController` (`controllers/measurement_controller.py`)

Owns the run state machine (`is_armed` / `is_running` / `already_runned` +
the Start button's label: *start* → *stop* → *save/update*) and the 50 ms
update loop.

**ARM** (`arm_device`): `manager.arm()` — the timing controller's
integrator reset (PIC: `A`; Pico: no-op). The arm timestamp gates the
**≥ 3 s** minimum gap before START.

**START** (`start_device`), in exact order:

1. 3 s gap check; virtual-acquisition advisory dialog;
2. (simulation only: compute `rate` from the period);
3. `data_model.configure_adc(session.adc_bits)`, `data_model.clear()`,
   timestamps reset, `is_problematic = False`;
4. **workspace temp streaming**: open `<workspace>/.active_run.csv.tmp`
   (header per debug/operational), write `.active_run.json.tmp` (settings
   snapshot), `.active_run.hardware_config.json.tmp` and
   `.active_run.setup.json.tmp` (verbatim provenance) — a crash at any
   later point is recoverable with full provenance (§5.3);
5. **ramp sink creation** (if `timing_mode` ∈ {ramp, markers} and the
   acquisition is not virtual): `_start_ramp_storage()` builds the
   `RampStorageService` (ramp mode: per-channel `.bin` writers + window
   extractor + `index.json` path) or the in-memory
   `RampPairExtractorService` (markers mode), with metas carrying ADC
   bits/range, timing, driver/firmware, config sha256 and a run uuid;
   `on_window_pairs` → `data_model.append_batch` (§5.2, §6.3);
6. `hardware.start_receiving(simulate, rate, ramp_sink)` — the ordering
   contract: **arm the acquisition before starting the timing engine**
   (§5.1), aborting the run (and aborting the ramp storage) on failure;
7. chart init, immediate `_update_loop`, then `timer.start(50 ms)`.

**The 50 ms loop** (`_update_loop`): (a) if the hardware service reports
an error → stop the run, hide the start button, keep the acquired data;
(b) `raw = data_model.get_latest_batch()`; (c) elapsed-time label;
(d) `ingest_raw_batch(...)`; (e) `running_view.update_dashboard()`;
(f) append the converted rows to the online CSV and flush. The CSV
`timestamp` column is `sample_index / sample_freq` seconds.

**STOP** (`handle_stop_measurement`): `hardware.stop_receiving(send_stop=
not simulate)` (device stop + timing stop), ramp storage
`close(state=FINALIZED/ABORTED)` with a lost-window warning, one final
`_update_loop` drain, CSV close, timer stop, button → *save*.

**Results** (`calculate_results`): slice `filtered_data[start:end]` →
`core.calc_stat.calculate_results(...)` (bootstrap on by default,
resamples = ⌈n/10⌉; `var_fft` deliberately **not** copied — it is owned by
the FFT dialog so a rolling refresh cannot wipe it) → copy the four
classic estimators into `data_model.result` → moving-average and FFT-plot
computation via `AnalysisService` → `running_view.render_analysis(...)`.

**SAVE** (`handle_save_data`): flush the online CSV (or write memory to a
temp CSV when no workspace) → timestamps (`timestamp_end = start +
ptr/f_sample`) → `n_bootstraps` mirrored from the calc rule → ramp files
published *before* packaging (folder name goes into `settings.json`) →
`DataExporter.export_to_zip(...)` with the verbatim config/setup texts →
on success delete all seven `.active_run.*` temps.

**LOAD** (`handle_load_data`): `core.load_file` (ZIP 3.0/2.0 or legacy
CSV) → session timing/metadata/ROIs restored, results zeroed then merged
from `analysis.results` (schema-drift safe) → **embedded-hardware
provenance checks**: embedded `hardware_config.json` sha256 vs the
recorded hash, vs the currently loaded config, embedded setup vs the
workspace's, and per-channel `gpio_mask` ↔ capacitance re-validated
against the embedded config's gain model — every inconsistency becomes a
"Hardware Provenance" warning → `configure_adc` + `append_*` +
`rebuild_accumulators` (ROIs re-applied to the raw data) → running window.

## 5. Services

### 5.1 `HardwareService` (`services/hardware_service.py`)

The thin facade the controllers actually call, and the owner of the
simulation threads.

- `start_receiving(simulate, rate, ramp_sink)` — real hardware: requires
  the manager's `acquisition` role; calls
  `acquisition.start_measurement(session, data_model, ramp_sink)` **first**
  and only then `manager.start_timing_signal()`. This ordering is an
  alignment guarantee for split setups (the NI task must be armed before
  the Pico's first CONV_START edge — the 6451 ignores edges while not
  armed). When one device holds both roles, `start_timing_signal()` is a
  no-op (the acquisition start already sent `S`); when the timing device
  is separate (virtual acquisition, NI card) it sends the real start.
  Failure of the timing start stops the acquisition again.
- `stop_receiving(send_stop)`: `acquisition.stop_measurement()` +
  `manager.stop_timing_signal()` (which drains the Pico's stream to the
  end-of-stream marker when the timing device is separate).
- **Simulator** (`_simulate_data`, `_simulate_ramp`): a daemon thread
  paced at `rate` measurements/ms producing the *same tuple shapes the
  drivers produce* (debug pairs, sentinel-filled absent channel; ramp
  blocks pushed through the same `write_block` protocol) — so every
  downstream path (conversion, ramp storage, window extraction) is
  exercised identically in simulation.

### 5.2 `RampStorageService` / `RampPairExtractorService` (`services/ramp_storage.py`)

The ramp-sample streaming storage — format **AQR1 v1**, deliberately
separate from `DataModel`.

- **Producer side** (`_BlockSinkBase`): drivers call
  `write_block(channel, samples<uint32>, abs_index)` /
  `report_gap(...)` from their reader threads. Blocks enter a bounded
  queue (`QUEUE_DEPTH = 16384` blocks ≈ 512 MiB ≈ 64 s of slack at the
  8 MiB/s spec). Two overflow policies: `"drop"` (ramp/monitoring data —
  on a full queue the whole block is dropped and recorded as a *gap*; the
  next block's `abs_index` re-anchors alignment) and `"block"`
  (markers/analysis data — backpressure, never lose a row).
- **Writer thread** (`RampStorageService`): appends raw little-endian
  uint32 to one file per channel (4096-byte header + data), maintains a
  **rolling sha256 of the data section**, refreshes the `samples_written`
  watermark in the header about once per second (crash recovery), and
  feeds every block to the window extractor. `close(state)` finalizes
  (data sha256 written only for `FINALIZED`), fsyncs, writes
  `index.json` (per-channel totals, gaps, dropped blocks, uuid).
- **Window extraction** (`RampWindowExtractor`): chunks each channel's
  stream into windows of exactly `N = T_int/dt + 1` samples (the hardware
  guarantee, §NI 1.2); emits the **first and last sample of every window**
  as the classic debug tuples — the *only* ramp data the variance–
  covariance estimators ever see. A window missing on any channel is
  dropped as a pair (never silently misaligned); `flush()` at stop counts
  the remainder. Absent channels are padded with the overflow sentinel.
- **AQR1 header** (little-endian, offsets in the header struct): magic
  `AQR1`, header version/size, flags, sample dtype code, channel index,
  state (RUNNING/FINALIZED/ABORTED), timing-mode code (`internal`/`ramp`/
  `markers`), ADC bits, `samples_per_window`, `samples_written`
  (watermark), `data_offset`, then doubles for dt/period/int/reset/sample
  rate/range/start epoch, raw 32-byte `data_sha256` and
  `config_sha256`, a 32-char measurement uuid shared by both channels,
  and strings (start ISO, channel label, driver code, firmware version,
  app version). Read side: `open_ramp_file(path)` → header +
  `np.memmap("<u4", offset=4096)`.
- **Markers mode** (`RampPairExtractorService`): the same block→window
  machinery with **no files at all** — on split hardware without ramp
  sampling the two integration-boundary edges per window *are* the classic
  start/stop stream, and writing binaries would duplicate `data.csv`.
- **Publishing / recovery**: `ramp_temp_paths` (workspace `.active_run.*`
  temps or system temp), `publish_ramp_files` (moves bins + index into
  `<workspace>/ramp/<measurement>/`, `_1`/`_2` suffix on collision,
  returns the `settings.json` "ramp" section),
  `recover_ramp_file` (truncates to the last whole window at the
  watermark, marks the header ABORTED; wired into the workspace recovery
  flow, §5.3).

### 5.3 `ShiftService` (`services/shift_service.py`)

All static methods around the campaign workspace: `save_setup` /
`load_setup` (the `setup.json` sections; old formats tolerated),
`link_hardware_config` (copy into the workspace + record
`{config_file, config_sha256}` in the setup file — any setup file name is
honored), `clear/read_hardware_link`, and **crash recovery**:
`check_for_recovery` (both `.active_run.csv.tmp` and `.active_run.json.tmp`
present) → `recover_data` (packages them into a user-chosen ZIP plus the
provenance temps) → `_recover_ramp_files` (best-effort: trim each ramp
temp to its last whole window, publish into the campaign's `ramp/` folder
with state ABORTED).

### 5.4 `DataExporter` (`services/data_exporter.py`)

- `build_settings_dict(...)` — the `settings.json` sections:
  `file_format_version` ("3.0"), `versions`, `measurement_info`
  (ISO timestamps, `acquisition_mode` = "debug", `is_problematic`),
  `experimental_setup`, **frozen** `hardware_settings` (ADC bits + applied
  range, per-channel applied `gpio_mask` + `capacitance_pF`, the resolved
  hardware summary, config sha256), `timing_settings` (applied times,
  period, sample frequency, `timing_mode`), `analysis` (ROIs, bootstrap
  metadata, results, optional `fft` block), optional `ramp` section.
- `export_to_zip(...)`: ZIP_DEFLATED with members in order
  `settings.json`, `data.csv` (copied from the temp file), verbatim
  `hardware_config.json`, verbatim `setup.json` — the archive
  interprets itself without the workspace (see HARDWARE_CONFIG.md §6).
- CSV columns: debug `timestamp, ch1_start, ch1_stop, ch2_start,
  ch2_stop`; operational `timestamp, ch1, ch2`; NaN written as `nan`;
  overflowed samples stored as NaN.
- `update_analysis_in_zip` exists but is currently dead code (its only
  call site is inside an `if False:` block — BUG.md B17).

### 5.5 `AnalysisService` (`services/analysis_service.py`)

Static-method façade over `core`:

- `calculate_moving_average_variance(data, window)` — valid-mode
  convolution; `mvar = max(0, conv(x²) − conv(x)²)`.
- `calculate_fft(...)` — the *plot* FFT for the results panel (dB axis):
  rfft periodogram, magnitude or PSD normalization matching
  `calc_stat.one_sided_psd`, single-sided folding, downsampling above
  4000 points (max- or mean-pooled bins).
- Physics façade: `load_bragg_data` / `load_sobp_data` /
  `calculate_g_parameter` / `calculate_integration_times` /
  `export_data` (thin delegations to `core.bragg_peak`), plus the custom
  setup import/export used by the planning page (18-column CSV round-trip
  and the single-depth planning math).
- `calculate_acf`/`calculate_pacf` are unused leftovers (B17).

### 5.6 Multi-measurement analysis
(`measurement_catalog`, `variable_catalog`, `multi_analysis_service`)

- `measurement_catalog.scan_workspace(workspace)` — lists the workspace's
  ZIPs chronologically, parsing **only** `settings.json` (fast even for
  hundreds of archives); corrupt archives are skipped. `build_live_record`
  serializes the running measurement through `DataExporter` into the same
  shape, so the running point can be plotted as a first-class record.
- `variable_catalog` — a catalogue of named variables extractable from
  each record: every estimator parameter per channel (`{macro}_{param}_{ch}`,
  the `[ch1_val, ch1_unc, ch2_val, ch2_unc]` layout; `N == 0` = "not
  computed" = NaN), metadata (beam energy/current, depth, voltage),
  timing (`int_time_ms`, …), hardware (`capacitance_chX_pF`, `adc_bits`),
  `timestamp` and pipeline-assigned `index`. Linear quantities also exist
  as charge variants (`_pc`, `_pc2`) converted with *each measurement's
  own* recorded capacitance/ADC range
  (`counts_to_pc_factor = C_pF · ΔV / (2^bits − 1)`); `raw_counts`
  variables drive the gain-mixing warning.
- `multi_analysis_service.run_pipeline(records, x_expr, y_expr(s),
  group_var)` — pure numpy: per-record context (`index` = chronological
  position), expression evaluation via `core.formula` (§6.4) with
  **first-order automatic-differentiation uncertainty propagation**
  (correct for repeated variables), a finite mask over x and *all* y
  expressions (a record plots only where every expression is valid),
  grouping into labelled series (incl. the live record as its own open-
  symbol series), and a mixing warning when raw-counts variables are
  plotted across different gains/ADCs.

## 6. Core (pure science)

### 6.1 `calc_stat.py` — the variance–covariance estimators

Input: the ROI-filtered `(n, 2)` matrix (NaN = excluded sample). Output:
the `result` dict, each parameter a `[ch1_val, ch1_unc, ch2_val,
ch2_unc]` list.

- **`var`** (variance method): `mean`, `var(ddof=1)`,
  `ȳ_D = var/mean`. Analytical uncertainties only (no bootstrap):
  `σ_mean = √(var/n)`, `σ_var = √(2·var²/(n−1))`, and the propagated
  `σ_ȳ_D = ȳ_D·√(2/(n−1) + var/(n·mean²))`.
- **`var_cov`** (successive covariance): the channel is decimated into
  odd/even subsequences (pairs of consecutive samples); statistics on the
  even subsequence, covariance `cov = np.cov(odd, even, ddof=1)`;
  `ȳ_D = (var − cov)/mean`; `cov_mean_sq = cov/mean²` (feeds the
  uncertainty propagation in the UI).
- **`var_autocov`** (lag-1 autocovariance): `γ(1) = Σ(x_i−μ)(x_{i+1}−μ)/(n−1)`;
  `ȳ_D = (var − γ(1))/mean`.
- **`var_doublecov`** (cross-channel, two detectors): covariance between
  the two channels over pairwise-finite rows; same layout as `var_cov`
  (the `Covariance` slot carries the same value in both channel slots).
- **`var_fft`** (frequency-domain; computed **only** through the FFT
  dialog): per channel `calculate_fft_block` —
  `compute_spectrum` (periodogram or Welch with the global mean removed
  once before segmentation, so the PSD integrates Parseval-exactly to the
  time-domain variance), the white-noise **floor** measured as the mean
  PSD over the frequency bins *kept* (the complement of the user's
  excluded bands, optionally clipped at the threshold),
  `Variance = Σ psd·df` over the whole spectrum, `Covariance = Variance −
  floor·f_Nyquist` (everything non-white), `ȳ_D = floor·f_Nyquist/mean`;
  uncertainties are 0 (future work); a diagnostics snapshot (floor,
  peaks above threshold with their variances, kept-mask) accompanies it.

**Uncertainties for the covariance methods** — circular block bootstrap
via the `arch` package:

1. block length: `arch.bootstrap.optimal_block_length(...)["circular"]`,
   clamped to `[1, n/2]`;
2. resample count: `n_bootstraps = max(1, ⌈n/10⌉)`;
3. `get_cbb_indices` draws random-start wrap-around blocks (integer
   arithmetic instead of modulo) building `n_b × n` index matrices;
4. the resampled statistic set {mean, var, cov (or γ(1)) , ȳ_D,
   cov/mean²} is evaluated per resample;
5. uncertainties = `std(ddof=1)` of each bootstrap distribution;
6. failures (NaN block length, exceptions) zero the whole section.

Parallelism: `ThreadPoolExecutor(os.cpu_count())`, futures in batches of
2 resamples (numpy releases the GIL inside the reductions). Constant or
single-sample input short-circuits to zeroed sections; disabled channels
get zeroed `N` (the archive convention for "not computed").

### 6.2 `bragg_peak.py` — physics planning

- `load_fluka_data("data/pristine")`: discovers `energy/step` folders
  with `energy.dat` + `fluence.dat` (depths = bin midpoints), interpolates
  fluence onto the energy grid, derives LET = (dE/dx ratio)·100, applies
  the `ranges.txt` depth truncation.
- `load_sobp_data("data/SOBP")`: dense depth-dose CSV + sparse yF/yD/
  fluence CSV, interpolated onto one grid; keV/µm → eV/µm conversions.
- `calc_g_parameter(let_0, l, i_leakage, rate, E_0, E_zmax)`: with
  `W = 30 eV` per ion pair, `q_0 = LET·l/W·e`; `g = 100·i_leak/(rate·q_0)
  ·(E_0/E_zmax)` (energy-attenuation corrected).
- `calc_integration_times(...)`: current at depth `I_z = rate·q_0·g·(E/E_0)`;
  **minimum** integration from the statistics requirement (`n_min`
  events) and the charge floor (`Q_min`); **maximum** from ADC full scale
  (`Q_max/I_z`) and a 5 % headroom limit on ȳ_D; exposure times
  `T_exp = ȳ_D_charge/I_z`. The controller derives the measurement-time
  limits `T_m = (2·T_int + T_exp)/ε²` and the voltage statistics
  `mean_V = I_z·T_int/C`, `std_V = √(I_z·T_int·ȳ_D_charge)/C`.

### 6.3 `load_file.py` and `data_utils.py`

- `load_file`: ZIP (v3/2.0 — reads `settings.json`, `data.csv` and the
  embedded provenance members at any folder depth) or legacy CSV (header
  regexes `integration time [s]:`, `reset time [s]:`,
  `sample frequency [Hz]:`; 3 columns = operational, 5 = debug; sentinel
  32768 → NaN; negative diffs → NaN). ADC bits come from the archive
  settings (default 12).
- `data_utils`: `convert_data` / `convert_data_debug` (sentinel→NaN,
  negative→NaN) and `filter_data_by_rois` (masking, never row removal).

### 6.4 `formula.py` — the sandbox expression evaluator

`ast.parse` restricted by a node whitelist: `+ - * / **`, unary ±,
numeric constants, variable names (no calls, attributes, subscripts,
comparisons; `FUNCTIONS` ships empty — registering a callable enables
calls). Unicode operators (`−×÷·`) are normalized. Validation never
raises for user input — it returns problem strings with `difflib`
suggestions for unknown variables. Evaluation carries **(values,
gradients)** per node: first-order automatic differentiation w.r.t. every
base variable, so `σ_f = √(Σ (∂f/∂v·σ_v)²)` is exact for repeated
variables (e.g. `(yd/i)*i` keeps ȳ_D's uncertainty exactly). NaN/inf
propagate silently; the caller excludes non-finite points.

## 7. Hardware abstraction

### 7.1 The rules (`hardware/interfaces/`)

Four abstract contracts (see §2 of the root README for the method tables):

- `ITimingController` — timing configuration (`configure_noramp`,
  `configure_ramp`), the **pure** preview `calculate_actual_timing`
  (no serial traffic; the UI calls it on every keystroke), `arm`,
  `set_channels`, `set_mode`, plus the optional `start_timing`/
  `stop_timing` pair for timing-only operation (split hardware, virtual
  acquisition).
- `IGainController` — `set_gpio_mask` (device-centric: one bool per
  declared pin, routing knowledge lives in the manager) and
  `set_event_mode` (the SE/ME line is owned by the gain role).
- `IDataAcquisition` — `start_measurement(session, data_model,
  ramp_sink=None)` / `stop_measurement`, optional `configure_channels`
  and `set_input_range`, `get_adc_settings`.
- `IRampSink` — the protocol a driver pushes normalized uint32 sample
  blocks into (`write_block(channel, samples, abs_index)`,
  `report_gap`); all calls may come from the driver's reader thread.

### 7.2 Registry (`hardware/registry.py`)

Driver **codes are stable, versioned identifiers** (`pico_serial_v1`,
`pic_serial_v1`, `ni_daqmx_v1`, `dummy_v1`) mapping to implementation
classes plus a `DriverDescriptor` extracted from class attributes:
capabilities, `HARDWARE_LIMITS` (the layer-1 limits), `GAIN_MAX_PINS`,
`ADC_BITS`, native range, max channels, **max rate** (gates ramp dt at
apply), channel terminations. The descriptor is the code-side half of the
config handshake: the loader validates the JSON's declared envelope
against it at load time; an unknown code or an envelope beyond the
hardcoded limits is a load error. Factories type-check the connection
block (`serial` / `daqmx` / `dummy`); no port is stored — the manager
derives `/dev/atomiqa/<device_id>`.

### 7.3 The drivers

One paragraph each; the deep dives are the component documents:

- `PicoSerialDAQ` — `pico_serial_v1` — PICO2DAQ 2.x over USB CDC; exact
  quantization mirror (150 MHz floors; ramp: generator auto-selection at
  dt = 100 µs, PIO raw-count programming via `O`, one-time `Q`
  verification), identity handshake as liveness gate, 4-byte-word stream
  with marker handshake and byte-realign recovery, no-op
  mode/channels/arm. → [PICO/FIRMWARE_AND_DRIVER.md](PICO/FIRMWARE_AND_DRIVER.md)
- `PICSerialDAQ` — `pic_serial_v1` — the legacy protocol over RS-232:
  raw timer-register programming behind the `W` handshake with
  byte-pacing, 32768 Hz tick quantization with TX-delay compensation
  (per mode × channels), mode-dependent 8/4/2-byte frame parser.
  → [PIC/FIRMWARE_AND_DRIVER.md](PIC/FIRMWARE_AND_DRIVER.md)
- `NIDAQmxAcquisition` — `ni_daqmx_v1` — USB-6451, external CONV_START
  sample clock on PFI0, fixed terminal table keyed by termination,
  volts→uint32 normalization, arm-before-timing-start contract, drain-
  before-stop. → [NI/DRIVER.md](NI/DRIVER.md)
- `DummyAcquisition` — `dummy_v1` — virtual acquisition for bench tests
  (no data; ADC properties come from the config).

### 7.4 `HardwareManager` (`hardware/core/hardware_manager.py`)

The object between the config and the roles:

- **Config lifecycle**: `load_config` (parse + validate via
  `hardware.config.loader` against the registry descriptors) →
  instantiate devices, assign roles (`timing`, `acquisition`, `gain_ch1/2`
  — one device may hold several), push the channel plan into instrument
  drivers, build the per-channel **gain models**, notify config
  listeners. `unload_config` disconnects everything and resets.
- **Gain models** (`hardware/config/gain_model.py`): from the circuits'
  capacitors and the `connections` wiring — every wired combination
  becomes a gain option with a label, a total capacitance (fixed +
  switched, grouped pins **sum**) and a per-device pin mask; unwired
  capacitors are excluded with a warning; ganged pins link the two
  channels' options (`compatible_gain_options` filters CH2 selections
  against CH1).
- **Atomic gain apply** (`apply_gains`): both channels' merged pin state
  is computed *first*; a shared-pin conflict rejects the pair before any
  pin moves; each physical device receives exactly one `set_gpio_mask`;
  a failure after another device already switched sets
  `gain_state_unknown` until a full re-apply succeeds.
- **Timing-mode routing**: `timing_mode_for(sample_ramp)` →
  `ramp` / `markers` (split hardware, trigger-capable timing device: the
  ramp generator runs with **dt = T_int**, two CONV_START edges per
  window) / `internal` (with a warning when the timing device cannot emit
  markers). `configure_timing` and `preview_timing` route consistently —
  the preview can reject (raising, not swallowing) when the driver's
  mirror says the request is unrealizable. `validate_timing(...)` is the
  same mirror as a pure check (no return value, no-op without a timing
  controller or a mirror-less driver such as the PIC); the simulation
  path of Apply Configuration runs it first, because there is no hardware
  that could refuse an unrealizable request later (B34).
- **Start/stop ownership**: `start_timing_signal()` sends the timing
  start only when the timing device is *not* the acquisition device
  (otherwise the acquisition start already did); symmetric stop drains
  the timing device's stream.
- **Provenance**: `devices_summary()` is the frozen hardware identity
  embedded into every archive; `config_text`/`config_sha256` are the
  verbatim file and its fingerprint.

### 7.5 Config validation (`hardware/config/loader.py`, `schema.py`)

Five flat sections (`devices`, `circuits`, `channels`, `roles`,
`connections`) cross-referenced by ids; validation is **complete at load
time**: structure, cross-references, declared limits ⊆ driver limits,
wiring rules (a capacitor ≤ 1 pin; the pin's device must hold the gain
role; unwired capacitors excluded with warnings; ganged-pin consistency),
ADC requirements (non-empty ranges; a default range required when
several). Errors collect into one `ConfigValidationError` listing every
problem; warnings ride along. The editor (`hardware/config/editor_model.py`)
works on the same validated model with live validation — it cannot save
an invalid file. Full user-facing reference: [HARDWARE_CONFIG.md](../HARDWARE_CONFIG.md).

## 8. Concurrency model

**Threads** (all worker threads are daemons):

| Thread | Owner | Role |
|---|---|---|
| Qt main | app | UI, controllers, all numpy/statistics work, online CSV |
| per-driver reader | driver (`start_measurement`) | bytes/frames → `DataModel.append_batch` (or ramp sink blocks) |
| simulation | `HardwareService` | synthetic tuples/blocks at `rate` |
| `ramp-storage-writer` | `RampStorageService` | queue → file writes + window extraction + pairs callback |
| `ramp-pair-extractor` | `RampPairExtractorService` | markers-mode in-memory windowing |
| bootstrap pool | `calculate_results` | `ThreadPoolExecutor(cpu_count)`, GIL released in numpy |

**Handoff invariants**

- The only cross-thread mutable state between producers and the UI is
  `DataModel._data_buffer` (a list swapped out under one lock —
  O(1), writer never blocked) and the ramp sinks' bounded queues.
- Window pairs enter `DataModel` through the same `append_batch` path
  from the storage writer thread — the estimators cannot tell ramp-run
  rows from µC-run rows.
- No command is sent to a µC while its reader thread runs (the firmware
  would ignore it / the reply would corrupt the stream); the drivers
  refuse such commands themselves.
- **Ordering contracts**: acquisition armed before the timing `S`
  (window-0 alignment); timing configured before start; ramp sink created
  before `start_receiving`; ramp files published before the ZIP is
  written.
- Stop timeouts everywhere: sim 2 s, reader join 2 s, ramp storage 30 s,
  pair extractor 10 s, NI reader 6 s; the Pico stop waits `period + 3 s`
  for the end-of-stream marker.

**Timers**: one 50 ms `QTimer` in `MeasurementController` (the live
pipeline); a debounce timer inside the multi-analysis dialog; nothing
else polls.

## 9. Two data-flow walkthroughs

### 9.1 One tick of a classic (all-Pico, internal mode) measurement

```
Pico core1: sample start → integrate T_int → sample stop → pack word
Pico core0: ring → USB
pico reader thread: 4-byte words → debug tuples → append_batch
   (50 ms boundary)
UI timer → get_latest_batch() (O(1) swap)
   → ingest_raw_batch: sentinel→NaN, diff, ROIs, histogram, Chan stats
   → running_view.update_dashboard()  (MCA / time series / table)
   → .active_run.csv.tmp append + flush
```

### 9.2 One tick of a split (Pico times, NI acquires, markers mode)

```
Pico (timing-only): CONV_START edges at 0 and T_int of each window
NI reader thread: read_many_sample block (may be 1 sample)
   → volts → uint32 counts → sink.write_block(ch, counts, abs_index)
pair-extractor thread: window N=2 → (start, stop) per channel
   → on_window_pairs → DataModel.append_batch     (no files written)
UI timer → … identical to 9.1 from here
```

In `ramp` mode the same path additionally writes `ramp/*.bin` (AQR1) and
the analysis still sees only the window-boundary pairs.

## 10. Known gaps and dead code

The authoritative, severity-tagged list lives in [BUG.md](../BUG.md)
(open at the time of writing: config-less simulation ignores typed times
B21, mid-run acquisition errors are not surfaced B22, a hardcoded 5 V ADC
range in the running window's charge scaling B23, plus low-severity
items). Known dead code pending removal (B17): the superseded drivers
`services/daq_interface.py`, `pico_serial_daq.py`, `pic_serial_daq.py`,
`core/timing.py`, the unused ACF/PACF, `DataExporter.update_analysis_in_zip`
and a disabled `if False:` block in `MeasurementController`.
Documentation-level notes worth knowing: the PIC driver's integration
model diverges from the firmware for integration times above 2 s that are
not multiples of 2 s — analyzed, with a verified error table and fix
options, in [PIC/PIC_BUG.md](PIC/PIC_BUG.md). The PIC's maximum reset time
is 2 s **by design** (the reset phase spans a single timer period,
[PIC/FIRMWARE_AND_DRIVER.md §2.2](PIC/FIRMWARE_AND_DRIVER.md)). In
single-channel mode the PIC's only converted input is RF1 (AN6), which the
driver maps onto the application's CH1 — intentional, but the detector
must be wired there ([PIC/README.md](PIC/README.md)).

## 11. Testing and tooling

- `tests/` (pytest; Qt widgets offscreen, message boxes stubbed): numerical
  correctness of all estimators incl. the FFT block and bootstrap
  (`test_calc_correctness`), hardware-config schema/gain models
  (`test_config`, `test_editor_model`, `test_library`), the manager's
  routing/gain-apply/ordering (`test_hardware_manager`,
  `test_timing_routing`), each driver against fakes (`test_pico_driver`
  — fake serial; `test_ni_driver` — fake `nidaqmx`; the PIC protocol is
  exercised through the manager fakes), ramp storage format/recovery
  (`test_ramp_storage`), archive round-trips (`test_data_exporter`,
  `test_adc_context`), workspace handling (`test_shift_service`), the
  multi-analysis stack (`test_multi_analysis`, `test_multi_analysis_dialog`,
  `test_variable_catalog`, `test_formula`), UI wiring
  (`test_app_handlers`, `test_manual_connect`), udev generation over a
  fake sysfs (`test_udev_rules`). Shared hardware-config builders live in
  `tests/helpers.py`.
- `build.py` — PyInstaller one-dir bundle (collects PyQt6/numpy/pyqtgraph,
  bundles `data/`, `hardware_library/`, `examples/`).
- `setup_udev_rules.sh` + `tools/generate_udev_rules.py` — one udev rule
  per configured serial device (matched by USB serial number) creating
  the persistent `/dev/atomiqa/<device_id>` symlinks; handwritten rules
  are documented in [UDEV_RULES.md](../UDEV_RULES.md).
- Dependencies (unpinned, `pyproject.toml`): PyQt6, PyQt6-Charts,
  pyqtgraph, numpy, pandas, pyserial, `arch` (block bootstrap),
  statsmodels; dev: pytest, black, flake8.
