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

### B24 — NI stream not aligned to the integration-window grid (start ordering) — **FIXED** *(reported 2026-09-27)*
- **Intended:** the first converted sample of a ramp/markers stream is the
  first CONV_START edge after the timing start — sample 0 of window 0, the
  alignment the window extractor (`services/ramp_storage.py`) and the
  `IRampSink` contract assume.
- **Current (as found):** `HardwareService.start_receiving` started the
  *timing* engine first ('S' to the Pico — edges flow within µs) and only
  then the NI task, whose `task.start()` ran inside the reader thread tens
  of ms later. The USB-6451 ignores AI Sample Clock while no acquisition is
  in progress, so the first converted sample landed at an arbitrary phase
  inside a window: markers mode paired (stop_k, start_{k+1}) — negative
  differences, every row NaN (~50% of runs, the phase is random); ramp mode
  chunked with a permanent phase offset (pairs spanned the reset gap, the
  .bin time grid shifted). Nothing detected or re-synchronized, and the unit
  tests hid it (fake reader and simulator emit window-aligned whole blocks
  starting at index 0).
- **Fixed (2026-09-27):** arm the clock consumer before the clock producer.
  `NIDAQmxAcquisition.start_measurement` now starts the task *before*
  returning (verified: the regression test checks the task is armed at the
  moment the timing 'S' is sent, and fails on the old ordering);
  `start_receiving` arms the acquisition first and starts the timing second,
  stopping the armed acquisition again if the timing start fails; the NI
  reader treats DAQmx read timeouts (-200284) as "no CONV_START edges yet —
  keep waiting" (the armed task legitimately sees no samples until 'S').
  The firmware guarantees alignment: no edges during reset/settle, first
  integration edge at t=0 of the window (raspberry_copy/README.md), so the
  first edge after 'S' defines window 0. All-in-one Pico runs are unaffected
  (firmware-synchronized ADC). Regression tests:
  `tests/test_ni_driver.py` (`test_acquisition_is_armed_before_timing_start`
  and the arming/timeout-tolerance/cleanup tests).

### B25 — Single-channel ramp/markers crashes the measurement loop — **FIXED** *(reported 2026-09-27)*
- **Intended:** the classic pipeline has a fixed two-channel data shape; a
  single-detector run emits the absent CH2 sentinel-filled (NaN after
  conversion), exactly like the µC drivers do — regardless of which
  acquisition hardware produces the stream.
- **Current (as found):** `RampWindowExtractor._drain` built the emitted
  tuple over the *configured* channels only, so a one-detector split-hardware
  run (ch2 omitted, HARDWARE_CONFIG §2.3) emitted 1-tuples
  `((start, stop),)`. `convert_data_debug` unpacks the two-channel shape
  unconditionally and raised `ValueError` — inside `ingest_raw_batch`, called
  from the 50 ms Qt timer slot, where an unhandled exception aborts the
  PyQt6 process. Single-channel *simulation* of ramp mode crashed the same
  way (same sink). The end-to-end tests all used two channels, so the suite
  never exercised the shape.
- **Fixed (2026-09-27):** the extractor gained an optional `pad_sentinel`;
  with it, every emitted pair carries both channel slots and an absent
  channel is filled with `overflow_sentinel(bits)` — the same convention as
  the drivers, NaN-mapped downstream. `RampStorageService` derives the
  sentinel from its channel metadata automatically;
  `RampPairExtractorService` (markers mode) takes `adc_bits`, passed by the
  controller from the session. Two-channel runs emit byte-identical pairs.
  Regression tests: `tests/test_ramp_storage.py`
  (`test_single_channel_pairs_are_sentinel_padded_like_the_drivers` —
  through `convert_data_debug` *and* a full `DataModel.ingest_raw_batch`,
  the reported crash path — plus the markers-sink and two-channel checks).

### B26 — PFI0 clock input out of spec: digital logic family left at the 5 V default — **FIXED** *(reported 2026-09-27)*
- **Intended:** the Pico's 3.3 V CONV_START drives the NI's sample-clock
  input within the guaranteed logic levels.
- **Current (as found):** the USB-6451's shared digital logic family
  defaults to **5 V**, whose guaranteed V_IH is **3.66 V** (specs Table 17) —
  below the Pico's 3.3 V high (the 3.3 V family needs only 2.00 V). The
  driver documented "digital, 3.3 V" but never configured the family: the
  clock was out of spec and would only work marginally (real thresholds sit
  below the worst-case guarantee).
- **Fixed (2026-09-27):** `connect()` sets `dig_port_logic_family` to 3.3 V
  on the clock's physical channel (nidaqmx `LogicFamily.THREE_POINT_THREE_V`,
  shared for all digital lines per the manual) and fails the connection when
  the set is rejected — an out-of-spec clock is never armed. Regression
  tests: `tests/test_ni_driver.py`
  (`test_connect_sets_33v_logic_family_on_pfi0`,
  `test_connect_fails_when_logic_family_cannot_be_set`).

### B27 — Low-rate markers stream: ~100 s delivery lumps and the buffered tail lost at stop — **FIXED** *(reported 2026-09-27)*
- **Intended:** the live pipeline tracks the measurement rate, and every
  acquired sample reaches the sink — in markers mode the samples ARE
  measurement rows.
- **Current (as found):** the read block size was floored at 1024 samples
  and the read timeout scaled as 10 block times: at markers rate = 1/T_int,
  T_int = 100 ms meant one read per **~102 s** (dashboard frozen in between)
  and a ~17 min timeout; at STOP, `task.stop()` discarded whatever sat
  unread in the DAQmx buffer — up to 1023 windows of analysis data, silently.
- **Fixed (2026-09-27):** the block size targets ~20 ms of samples with **no
  floor** (1 sample per read at 10 S/s); the read timeout is bounded to
  [0.5, 2] s; `stop_measurement` joins the reader FIRST — it drains the
  buffer with `READ_ALL_AVAILABLE` before `task.stop()` discards it. The
  drain also exposed (and the test pinned) a second bug: a stop landing
  mid-wait used to re-raise the read timeout and skip the drain entirely.
  Regression tests: `tests/test_ni_driver.py`
  (`test_block_size_and_read_timeout_scale_with_rate`,
  `test_stop_drains_the_buffered_tail_before_task_stop` — with a fake that
  discards its buffer on task stop, so only drain-before-stop passes).

### B28 — Markers mode inherits the firmware's ramp-dt cap; the preview hid it — **FIXED (app side)** *(reported 2026-09-27)*
- **Intended:** window markers (split hardware, ramp sampling off, dt =
  T_int) work for the full validated integration-time range — the software
  generator has no dt-coupled bound — and a configuration the hardware
  cannot realize is rejected with a specific reason, not a generic failure.
- **Current (as found):** firmware 2.0's `P` command validation applies the
  PIO 16.8-divider bound (≈ 4369 µs) to **both** generators, so markers mode
  only worked for T_int ≤ ~4.37 ms; the driver mirrored that cap faithfully.
  The UI preview swallowed the mirror's rejection and fell back to the
  no-ramp numbers: the user saw plausible true values, then a generic
  "Failed to apply configuration".
- **Fixed (2026-09-27, app side):** the driver's ramp-dt bound is now
  version-gated — `RAMP_DT_MAX_SOFT_US` (10 s) from firmware 2.1 on,
  the 4369 µs cap for 2.0 and while not connected — enforced in
  `calculate_ramp_timing` and `configure_ramp` against the *deployed*
  firmware's truth; `HardwareManager.preview_timing` re-raises the mirror's
  rejection instead of falling back; the timing window shows the specific
  reason (`on_configuration_result(False, message)`).
- **Firmware half fixed (2026-09-27):** `raspberry_copy/main.c` bumped to
  **PICO2DAQ 2.1** — the `P` command now bounds dt by the general time
  range (10 s) when the software generator is selected (2.0 wrongly applied
  the PIO 16.8-divider bound to both generators; the PIO path keeps its
  bound, and its `O` application path is untouched). Boards must be
  reflashed to unlock long-integration markers mode; until then the driver
  rejects large dt with the reason, and the workaround is ramp sampling
  with T_int a multiple of dt. A consistency test pins the firmware
  source's `FW_VERSION` against the driver's `RAMP_DT_LARGE_FW` gate.
  Regression tests: `tests/test_pico_driver.py` (dt gate on 2.0 / 2.1 /
  unconnected, `test_shipped_firmware_satisfies_the_large_dt_gate`),
  `tests/test_timing_routing.py`
  (`test_preview_rejection_is_raised_not_swallowed`).

### B30 — The B27 stop-drain was ineffective against the real nidaqmx wrapper — **FIXED** *(reported 2026-09-27)*
- **Intended:** `stop_measurement`'s drain empties the DAQmx buffer into
  the sink before `task.stop()` discards it (B27).
- **Current (as found):** the drain called
  `read_many_sample(buffer, READ_ALL_AVAILABLE, timeout=0.1)` with the
  fixed (chans, block) buffer. On the real wrapper, READ_ALL resolves —
  via `_calculate_num_samps_per_chan` — to `in_stream.avail_samp_per_chan`,
  and `_verify_array` (shape verification ON by default) then demands
  `buffer.shape == (chans, avail)` EXACTLY: any avail != block raises
  `DaqError`, which the drain's `except Exception: break` swallowed —
  nothing was drained, `task.stop()` discarded the tail. The fake
  implemented min(avail, buffer) semantics and skipped verification, so
  its test could not catch this.
- **Fixed (2026-09-27):** the reader is created with
  `verify_array_shape = False` and reads explicit counts
  `n = min(in_stream.avail_samp_per_chan, block)` in a loop (main loop
  and drain) — READ_ALL_AVAILABLE is never requested into a fixed buffer.
  The fake reader now mirrors the real wrapper's contract (READ_ALL
  resolution, exact-shape verification, partial-transfer semantics), so
  the old pattern fails the tests. Regression tests: `tests/test_ni_driver.py`
  (`test_never_reads_all_available_and_disables_shape_verification`,
  `test_stop_drains_the_buffered_tail_before_task_stop`).

### B31 — A partial read timeout silently shifted the window grid — **FIXED** *(reported 2026-09-27)*
- **Intended:** every sample DAQmx transfers reaches the sink with a
  contiguous index — a lost batch must be recorded as a gap, never
  absorbed silently.
- **Current (as found):** on a read timeout with partial data (transport
  hiccup mid-run), DAQmx has already removed the transferred samples from
  its buffer and written them into the caller's array, raising
  `DaqReadError` with the count. The driver matched only the error code
  and `continue`d: the partial samples were discarded, the counters did
  not advance, and the next block was delivered under the old contiguous
  index — no gap recorded, every subsequent window misaligned (the B24
  failure mode reintroduced). The pre-'S' wait was benign (partial = 0).
- **Fixed (2026-09-27):** timeouts are handled in one place
  (`read_count`): a partial count is delivered from the buffer and the
  counters advance before the wait continues. With B30's avail-based
  reads, mid-run partial timeouts are structurally impossible (the
  requested samples are already in the buffer when the read is issued);
  the delivery path is the defensive net. Regression test:
  `tests/test_ni_driver.py`
  (`test_partial_timeout_delivers_transferred_samples`).

### B32 — Stop order undercut the drain (stale comment after B24) — **FIXED** *(reported 2026-09-27)*
- **Intended:** stop mirrors the B24 start order: timing engine first
  ('E' — the Pico finishes its current cycle, the edge train ends), then
  the acquisition (drain a static buffer, then task.stop()).
- **Current (as found):** `HardwareService.stop_receiving` still stopped
  the acquisition before the timing — its "Symmetric to the start"
  comment was stale after B24 inverted the start. During the drain the
  Pico kept producing edges: the drain never emptied, the 6 s join
  expired, `task.stop()` killed the reader and discarded the residual
  buffer — and with B30 fixed, every stop would have taken ~6 s.
- **Fixed (2026-09-27):** `stop_timing_signal()` runs first, then
  `acquisition.stop_measurement()`. Regression test: `tests/test_ni_driver.py`
  (`test_stop_stops_timing_before_the_acquisition`).

### B33 — The timing window's rejection reason was written to a hidden label — **FIXED** *(reported 2026-09-27)*
- **Intended:** the B28 preview-rejection reason is visible while the
  user edits, and apply-time rejections show it too.
- **Current (as found):** `on_configuration_result(False, msg)` wrote the
  reason into the status frame, which stays invisible until the first
  Apply press (`setVisible(True)` only in `on_send_press`) — during
  editing the reason went to a hidden label; and the Apply-time failure
  passed no message, showing the generic text.
- **Fixed (2026-09-27):** the failure branch of `on_configuration_result`
  makes the frame visible; `HardwareManager.configure_timing` validates
  through the driver's mirror BEFORE touching hardware and raises
  `ValueError` with the reason, which `SetupController` threads into
  `on_configuration_result(False, reason)`. Regression tests:
  `tests/test_ni_driver.py`
  (`test_configure_timing_raises_the_mirror_reason`,
  `test_time_window_failure_branch_makes_the_status_frame_visible`).

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

### B29 — `data.csv` timestamps start at one sample period instead of zero *(reported 2026-09-27; renumbered from a duplicate B24)*
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
