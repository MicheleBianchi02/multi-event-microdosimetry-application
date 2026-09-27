# Known bugs — ATOMIQA 3.0

Deviations of the current code from the intended behaviour documented in
[README.md](README.md). This file is the tracking list of **open** bugs.

Entries B1–B12, B15, B16 and B18–B20 were marked FIXED and re-verified against
the code on **2026-09-27** (fix confirmed, regression tests present in
`tests/`); they were removed from this list. Their full descriptions and fix
notes remain in this file's git history. The original B-numbers are kept for
the surviving entries (cross-referenced from README.md); new entries continue
at B21.

Severity scale:

- **HIGH** — data loss, silently wrong results, or a core flow (connect/save)
  that does not fail when it should.
- **MEDIUM** — a feature is wrong or missing in realistic configurations
  (split hardware, reload of archives, different ADCs), without direct data loss.
- **LOW** — cosmetic, dead code, fragile coupling, or metadata inconsistency.

---

## MEDIUM

### B21 — Config-less simulation ignores the integration/reset times typed in the wizard *(reported 2026-09-27)*
- **Intended:** the values the user types in Step 2 (integration time, reset
  time) drive the simulated measurement and are recorded in the archive's
  `timing_settings` — the wizard's *Apply Configuration* reports success, so
  the user must be able to trust the reported timing.
- **Current:** without a loaded hardware configuration (e.g. `python app.py
  -s` with no workspace) the timing preview never runs:
  `SetupController.on_timing_calculation_requested` returns early when no
  timing controller is assigned (`controllers/setup_controller.py:164-167`),
  so `session.int_time` / `reset_time` / `sample_freq` are never updated from
  the inputs. The simulate path of *Apply Configuration* also returns success
  without applying anything (`controllers/setup_controller.py:148-151`).
  The measurement then runs with the session defaults (1 s / 1 s, 1 Hz)
  regardless of the typed values, and the archive records those defaults.
- Verified live: typing 100 µs / 50 µs and applying in config-less simulation
  leaves the session at 1 s / 1 s.
- Related: such a run also records the `SessionModel` 100 pF capacitance
  placeholder (`models/session_model.py:57-58`) in
  `hardware_settings.channels` of the archive (`services/data_exporter.py:54-63`),
  since the gain-table values that normally overwrite it are never applied.
- **Fix direction:** update the session timing in the preview/apply paths even
  when no timing controller exists (quantization = identity in simulation), and
  never export the capacitance placeholder when no config was applied.

### B22 — Mid-run acquisition errors are never surfaced; `HardwareService.has_error()` is dead code *(reported 2026-09-27)*
- **Intended:** when the acquisition fails during a run (transport error,
  reader-thread crash), the measurement loop notices, alerts the user and
  stops the run cleanly.
- **Current:** the drivers set their *own* `_error` flag when their reader
  thread dies (`hardware/pico/pico_daq.py:751`, `hardware/pic/pic_daq.py:425`,
  `hardware/ni/ni_daqmx.py:331`), but `HardwareService` never reads it — its
  `_error` is only set in `start_receiving` *before* the stream starts
  (`services/hardware_service.py:29-56`). The error branch in
  `MeasurementController._update_loop`
  (`controllers/measurement_controller.py:851-857`), which is supposed to
  surface the failure, can therefore never fire during a run. A mid-run
  failure leaves the dashboard silently frozen at the last sample with the
  button still showing STOP; the user only finds out at stop time
  ("Stop command failed") or by noticing no new data. The NI driver at least
  reports `_error` at `stop_measurement` (its stop returns failure).
- **Fix direction:** propagate the acquisition driver's error flag into
  `HardwareService` (e.g. `has_error()` consults the active acquisition
  driver), so the existing `_update_loop` error branch becomes reachable; on
  that path also finalize the ramp sink and close the temp CSV.

### B23 — Running window's charge scaling uses a hardcoded 5 V default instead of the applied ADC input range *(reported 2026-09-27)*
- **Intended:** the ADC context is dynamic end-to-end (B6): the charge
  conversion `Q = C · ΔV` uses the input range actually applied during the
  run (known from `HardwareManager.get_adc_settings()` at Apply
  Configuration, and from `hardware_settings.adc.input_range_volts` when an
  archive is loaded).
- **Current:** every charge conversion in the running window uses
  `setting_tab.adc_range` — a class attribute defaulting to 5
  (`views/running_window.py:2766`) bound to the editable "ADC Range [V]"
  spinboxes, which default to 0–5 V (`views/running_window.py:2822-2830`) and
  are never initialized from the hardware config nor from a loaded archive.
  Consequences: with the Pico (fixed 0–3.3 V) the live statistics table's pC
  columns (`fill_result_table`, `views/running_window.py:1788-1824`), the
  charge histogram axis and the channel-ROI region bounds
  (`views/running_window.py:1262-1264`, `1601-1631`) are wrong by 3.3/5; with
  the NI (default ±5 V, counts zero-based at −5 V) the span is wrong by 2×
  and the mean charge carries a C·5 pC offset. The user can type the correct
  values by hand, but nothing tells them the defaults are wrong.
- **Fix direction:** initialize the spinboxes (or the conversion directly)
  from `HardwareManager.get_adc_settings()` at Apply Configuration and from
  the archive's `hardware_settings.adc.input_range_volts` at load, like
  `adc_bits` already is (make the field read-only when the range is fixed).

---

## LOW

### B13 — Frozen build resolves the Bragg/SOBP data against the CWD
- The hardware-config half of this entry is resolved: the config no longer
  lives beside the executable — it is loaded from the workspace by absolute
  path (B2's workspace mechanism), and the component library is located via
  `__file__` (`app.py:166-169`), which survives freezing.
- What remains: the FLUKA planning data is opened with CWD-relative paths
  (`controllers/setup_controller.py:39-40`, `"data/pristine"`, `"data/SOBP"`),
  while `build.py` packs `data/` into the bundle via `--add-data` (resolved
  against the bundle's `_internal` directory, not the CWD). In the frozen app
  the Bragg/SOBP curves therefore never load (`AnalysisService.load_bragg_data`
  logs "Could not load Bragg data" and returns empty — verified), and the
  physics planning page shows no data unless the app is launched from exactly
  the right directory.
- **Fix direction:** resolve the data directory against the bundle
  (`sys._MEIPASS` / the `__file__`-relative pattern already used for
  `hardware_library`) with the source tree as fallback.

### B14 — Legacy "starting time" recovery path is unreachable
- `_read_starting_time` looks for the `starting time :` marker in the first
  line (`controllers/measurement_controller.py:945-960`), but `load_file`
  rejects any first line that has neither the timing header nor a `timestamp`
  prefix (`core/load_file.py:97-103`), so the marker can never be found; the
  fallback is "now".
- **Fix direction:** either teach `_parse_csv_content` to tolerate a leading
  `starting time :` header or drop `_read_starting_time`.

### B17 — Dead code and housekeeping — **PARTIALLY FIXED**
- **Fixed earlier:** `ITimingController.get_timing_limits()` (never called)
  is removed — drivers expose hardcoded limits via `HARDWARE_LIMITS` / the
  registry, and the UI uses the declared envelope from the config.
- Still in the tree:
  - Superseded pre-refactor drivers `services/daq_interface.py`,
    `services/pico_serial_daq.py`, `services/pic_serial_daq.py` (referenced
    only by each other, not by the app) and `core/timing.py` (timing math
    moved into the PIC driver).
  - `AnalysisService.calculate_acf/calculate_pacf`
    (`services/analysis_service.py:77,107`) never called (ACF/PACF not wired
    into the running window; only MA + FFT are).
  - `AppController._on_stop_measurement` (`app.py:390`) is never connected.
  - `MeasurementController.calculate_results` contains an `if False:` block
    (`controllers/measurement_controller.py:1043`) that disables the
    auto-update of analysis results inside a saved ZIP
    (`DataExporter.update_analysis_in_zip`).
  - Duplicated `setCheckable(True)` calls in `views/mode_window.py:37-38,55-56`.
- **Fix direction:** delete the superseded modules and unused methods, remove
  the never-connected handler and the `if False:` block (or re-enable the
  ZIP auto-update deliberately), drop the duplicate calls.

### B24 — `data.csv` timestamps start at one sample period instead of zero *(reported 2026-09-27)*
- **Intended:** the `timestamp` column holds "sample index divided by the
  sample frequency" (README §4.2) — the first sample of the measurement is
  t = 0.
- **Current:** `_write_data_to_file_object` increments the counter *before*
  writing each row (`controllers/measurement_controller.py:903,915`), so the
  first data row is at 1/freq and every timestamp is shifted by one period.
  Consistently, `handle_save_data` computes
  `timestamp_end = start + ptr/freq` (`controllers/measurement_controller.py:144-148`),
  one period past the last sample's own timestamp, so
  `measurement_info.timestamp_end` is inflated by one period. The sample-to-sample
  spacing (what time ROIs mostly use) is unaffected; the absolute time axis,
  the elapsed-time display of a loaded archive and the recorded end time are
  each off by one sample period.
- **Fix direction:** write `timestamp / freq` with the counter incremented
  *after* the row (or start at index 0), and use `(ptr - 1)/freq` for the
  end timestamp (both writers and the reader then agree).
