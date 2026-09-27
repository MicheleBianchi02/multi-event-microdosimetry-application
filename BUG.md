# Known bugs — ATOMIQA 3.0

Deviations of the current code from the intended behaviour documented in
[README.md](README.md). This file is the tracking list; entries marked **FIXED**
have been resolved (see the note on each entry).

Severity scale:

- **HIGH** — data loss, silently wrong results, or a core flow (connect/save)
  that does not fail when it should.
- **MEDIUM** — a feature is wrong or missing in realistic configurations
  (split hardware, reload of archives, different ADCs), without direct data loss.
- **LOW** — cosmetic, dead code, fragile coupling, or metadata inconsistency.

---

## HIGH

### B1 — Saved CSV contains ROI-filtered data instead of raw data — **FIXED**
- **Intended:** `data.csv` in the measurement ZIP stores the raw samples; time/channel
  ROIs are saved in `settings.json` and re-applied when the file is loaded.
- **Current:** the online stream writes the ROI-filtered batch
  (`controllers/measurement_controller.py:512-519`) and the from-memory dump writes
  `data_model.filtered_data` (`controllers/measurement_controller.py:117`). Samples
  excluded by an active ROI are permanently lost in the archive. Additionally a NaN
  in the CSV is ambiguous (overflow, negative difference, or ROI-excluded).
- **Fix direction:** write the raw converted arrays (`data_model.data` /
  unfiltered batches); the load path already re-applies ROIs from `settings.json`.
- **Fixed (2026-09-09):** `DataModel.ingest_raw_batch` now returns the raw converted
  samples (ROI filtering still feeds histograms/statistics internally), and the
  from-memory dump in `handle_save_data` writes `data_model.data`. NaN in the CSV
  now means only overflow or (debug mode) a negative start→stop difference.
  Regression tests: `tests/test_data_model.py`.

### B2 — Connection failures are always reported as success — **FIXED**
- **Intended:** connection is driven by the loaded hardware configuration JSON.
  On Linux each device gets a persistent name via a udev rule — one rule per
  device (four in the general case: timing µC, two gain µCs, readout system);
  transports may differ per device (plain serial for the µCs, USB/instrument
  protocol e.g. NI for the readout). The app auto-connects to all configured
  devices at startup or via an auto-connect button. If auto-connect fails
  (Windows, device not connected, udev rules missing), a manual-connect dialog
  lets the user map each configured device to an available connection. Whether
  `verify_connection` (the `V` handshake) belongs to this flow or is removed is
  still to be decided.
- **Current:** `HardwareManager.connect_all()` builds the `failed` list but has no
  `return` statement, so it returns `None` (`hardware/core/hardware_manager.py:126-138`).
  `SetupController.auto_connect` / `on_connect_requested` treat `None` as success and
  show "All devices auto-connected successfully" even when nothing is connected.
  The `V` handshake exists on both serial drivers but is never called, so
  "connected" only means "serial port opened". The shipped `setup_udev_rules.sh`
  also creates only a single rule (the Pico), not one per device.
- **Fix direction:** `return failed` in `connect_all`; implement the JSON-driven
  auto-connect and the per-device manual-connect dialog; extend the udev rules to
  one per device; decide the fate of `verify_connection`.
- **Partially fixed (2026-09-09):** `connect_all` now returns the failed device
  list (failures are reported), and connection is driven by the validated
  hardware config (loaded explicitly or via the workspace). Still open: the
  per-device manual-connect dialog for partial setups (the current dialog maps
  one port at a time). One-udev-rule-per-device is now generated from the
  hardware config (`tools/generate_udev_rules.py`).
- **Fixed (2026-09-14):** connection is button-driven (no attempt at startup
  or on config load). Step 1 shows a connection card — one status row per
  configured device — with **Auto Connect** (opens every configured
  `connection.port`, i.e. the udev persistent names), **Manual Connect…** and
  **Simulate**. On auto-connect failure the per-device manual-connect dialog
  opens automatically with the per-device errors: one row per configured
  *device* (roles shown as a column), an available-port combo per serial
  device (ports taken by another row hidden, configured port preselected when
  present), per-row Connect and Connect All with live status; Done requires
  every role-assigned device connected. The mapping is session-only and never
  written to the config; on Windows auto-connect fails fast into the manual
  dialog (same code path, no platform check). `HardwareManager` gained
  `connect_device` / `connection_status` / `all_role_devices_connected`, and
  `connect_all` now returns dev_id → error message. `verify_connection` was
  removed from the drivers (never called; an identity check will return as a
  capability-gated method when the firmware rework provides a unique
  per-board code) and the dead `SerialDevice` class was dropped from
  `services/serial_device.py` (only the port enumeration `get_port()`
  remains).
  Follow-up (same day): the config no longer carries a `connection.port` at
  all — the persistent name is derived from the device id
  (`/dev/atomiqa/<id>`, one udev rule per device creating exactly that
  symlink; `tools/generate_udev_rules.py` is keyed by device id). A leftover
  `port` field in an older file is ignored with a deprecation warning, so
  the config file is fully machine-independent.
  Identity follow-up (2026-09-16): with the PICO2DAQ 2.0 firmware the `V`
  handshake returned as a liveness check inside the Pico driver's
  `connect()` — the firmware replies with name, version, unique per-board
  id and system clock, and a wrong board or bare Pico refuses the
  connection. Full board-id matching against the config remains open
  (README §6).
  Regression tests: `tests/test_hardware_manager.py`, `tests/test_manual_connect.py`.

### B3 — Manual port mapping passes the port name instead of the device path — **FIXED**
- **Intended:** from the manual-connect dialog (see B2), mapping a configured
  device to a serial port connects to that port.
- **Current:** `SettingWindow` stores both the name (e.g. `ttyACM0`) and the device
  path (e.g. `/dev/ttyACM0`, kept in `selected_device`), but emits the *name*
  (`views/setting_window.py:302`); `selected_device` is never read anywhere. On
  Linux the driver then tries to open a relative path and fails, so the manual
  fallback (needed when udev auto-connect fails, i.e. exactly when B2 shows up)
  is unusable.
- **Fix direction:** emit `selected_device` (falling back to the name on platforms
  where they coincide).

## MEDIUM
- **Fixed (2026-09-09):** the connect request now emits the selected device path
  (falling back to the port name when they coincide).

### B4 — Selected gain is not persisted in the measurement archive — **FIXED**
- **Intended:** `settings.json` stores, per channel, the applied GPIO mask and the
  total capacitance; loading a measurement restores them.
- **Current:** `DataExporter.build_settings_dict` exports `capacitor_code_ch1/2`,
  which are never updated from the applied mask and remain the default `"1000"`
  (`services/data_exporter.py:63-71`); the actual capacitances
  (`session.capacitance_pf_ch1/2`) are not exported at all, and
  `handle_load_data` does not restore them. After loading an archive, the
  charge-axis scaling silently uses the current session value (default 100 pF).
- **Fix direction:** export `gpio_mask` + `capacitance_pF` per channel and restore
  them on load.
- **Fixed (2026-09-09):** with the archive v3 format, `settings.json` stores per
  channel the applied `gpio_mask` and total `capacitance_pF`, taken from the
  gain tables derived from the hardware config; loading a measurement restores
  them. The old `capacitor_code` fields are gone.

### B5 — SE/ME command routed to the acquisition device instead of both gain controllers — **FIXED**
- **Intended:** the SE/ME line belongs to the gain controllers; switching mode
  calls `set_event_mode` on **both** `gain_ch1` and `gain_ch2`, so both electronics
  receive the signal. `set_event_mode` is part of the `IGainController` contract.
- **Current:** `SetupController.on_mode_switched` calls `set_event_mode` on the
  *acquisition* role only (`controllers/setup_controller.py:147-151`), and
  `set_event_mode` is in none of the three interfaces. It works today only because
  one device holds all roles. With split hardware (e.g. NI acquisition), the
  command goes to the wrong device or is silently skipped (`hasattr` guard).
- **Fix direction:** move `set_event_mode` into `IGainController`; call it on both
  gain controllers. Future extension: per-channel event mode (ME on one detector,
  SE on the other).
- **Fixed (2026-09-09):** `set_event_mode` is part of `IGainController` and the
  mode switch goes through `HardwareManager.set_event_mode`, which calls it on
  every assigned gain controller.

### B6 — ADC bit depth is hardcoded; should come from the acquisition interface — **FIXED**
- **Intended:** the number of ADC bits (and everything derived from it: histogram
  bins, overflow sentinel, sample validation) is dynamic and provided by
  `IDataAcquisition.get_adc_settings()`.
- **Current:** `NUM_BIT = 12`, `NUM_BINS = 4096`, `OVERFLOW_VALUE = 32768` are
  constants (`core/constants.py`); `DataModel` buffers and histogram clipping, the
  drivers' sample validation, and `load_file` all use them. `get_adc_settings()`
  is implemented by the drivers but never called; `adc_num_bits` in setup.json /
  settings.json is informational only. With a 16-bit ADC, samples ≥ 4096 would be
  dropped from histograms and a legitimate reading of exactly 32768 would be
  treated as overflow (→ NaN on load).
- **Fix direction:** derive bins/sentinel from the acquisition device settings at
  session start; keep a serialized value in the archives for offline loading.
- **Partially fixed (2026-09-09):** the ADC bits and input range now come from
  `hardware_config.json` (validated against the driver), flow through
  `HardwareManager.get_adc_settings()` and are recorded per measurement in the
  archive. Still hardcoded: `DataModel`'s histogram bins / overflow sentinel
  remain fixed at 12 bit — deriving them dynamically at session start is the
  remaining work.
- **Fixed (2026-09-24):** the ADC context is dynamic end-to-end. `SessionModel.adc_bits`
  (signal `adc_bits_changed`) is set at *Apply Configuration* from
  `HardwareManager.get_adc_settings()` and at archive load from
  `hardware_settings.adc.bits` (old `adc_num_bits` also honoured; legacy CSVs fall
  back to 12 bit). `DataModel.configure_adc(bits)` sizes the histogram buffers and
  defines the sentinel; the running window derives axis ranges, charge scaling and
  ROI spinbox bounds from the live bin count (rebuilt on change); the simulator
  scales to the active full scale. New sentinel convention: **2^bits** (one past
  full scale, produced by the drivers and the simulator, mapped to NaN by the
  conversion layer); `OVERFLOW_VALUE = 32768` remains only as the legacy
  CSV-format-1 sentinel in `load_file`. `DataModel.configure_adc` rejects depths
  above 20 bit (monitoring-only ramp data never enters the model).
  Regression tests: `tests/test_adc_context.py`.

### B7 — Gain table is not constrained by the controller's GPIO capability — **FIXED**
- **Intended:** the offered gain combinations depend both on the circuit (which
  capacitors exist) and on the µC (`num_gpios_per_channel`): a µC with fewer GPIOs
  than the circuit's switchable capacitors must offer only the combinations it can
  actually drive, and vice versa.
- **Current:** `HardwareManager._calc_channel_gains` uses only
  `hardware_config.json` `gain_settings`; `num_gpios_per_channel` is never called
  anywhere. E.g. a 3-switch channel assigned to the PIC (1 GPIO) would still list
  all 8 combinations, and only `gpio_mask[0]` would actually be applied.
- **Fix direction:** clip/validate the combination table against the gain
  controller's declared GPIO count (also used by the future GPIO→capacitor
  mapping, see README §6).
- **Fixed (2026-09-09):** the gain tables are derived from the config's
  `connections` (pin → capacitor edges); a pin not declared for the gain
  device, or an edge naming another device, is a load error. A µC with fewer
  pins than the circuit's capacitors can no longer produce an undrivable
  table: unwired capacitors are excluded from it (warning), by design.

### B8 — Dual-channel UI offered with hardware that acquires one channel only — **FIXED**
- **Intended:** the channel selection reflects what the acquisition system can
  actually read (or warns).
- **Current:** the Pico reader fills CH2 with the overflow sentinel in both
  operational and debug mode (`hardware/pico/pico_daq.py:286-293`), consistent with
  its single-ADC firmware, but the UI still offers dual-channel mode and a CH2
  gain combo when the Pico holds the acquisition role. A user measuring "two
  channels" silently gets nothing on CH2. (The general two-detector case works
  only with the PIC or a future NI acquisition driver.)
- **Fix direction:** advertise the channel capability (e.g. via
  `get_adc_settings()`) and gate the single/dual-channel choice on it.

## LOW
- **Fixed (2026-09-09):** the acquisition capability declares `num_channels`;
  the config validator rejects files with more active channels than the
  acquisition device can measure, and the wizard disables the dual-channel
  choice when `max_channels < 2`.

### B18 — "Save as Component…" writes fragments that fail re-import validation — **FIXED**
- **Intended:** a component saved from the hardware config editor can be
  re-imported from the library without errors.
- **Current:** `_save_component` (`views/hardware_config_editor.py`) strips the
  whole `connection` block from device fragments (to drop the machine-specific
  port), but the loader requires `connection` with a `type`; re-adding the
  saved component fails with
  `devices.<id>.connection: must be an object with a 'type'`.
- **Fix direction:** save `{"type": "serial", "port": "", "baud_rate": …}`
  (empty port like the shipped fragments) instead of removing the block.
- Not fixed yet: the connection/transport handling is being reworked first
  (deferred with the auto-connect work, B2).
- **Fixed (2026-09-14):** with the port-less config (see B2) the connection
  block carries nothing machine-specific anymore: "Save as Component…" keeps
  it whole, dropping only a stale deprecated `port` field if the source JSON
  still has one. The shipped fragments lost their empty-port convention with
  the same change. Regression test: `tests/test_library.py`
  (`test_saved_device_fragment_keeps_connection_and_reimports`).

### B19 — Gain application on shared GPIO pins is sequential, not atomic — **FIXED**
- **Intended:** when the same uC controls the gain of two circuits, the
  shared pins must be accounted for as *one physical switch*: the merged pin
  state for the pair (CH1 + CH2) is computed first, validated, and then sent
  once. Mixed wirings — some pins shared between the circuits, some
  circuit-specific — are supported by the same rule: per shared pin, both
  channels must agree; circuit-specific pins are free.
- **Current:** `HardwareManager.apply_gain` stores the requested selection
  *before* the conflict check, and `send_timing_config` applies CH1 then CH2
  sequentially. Consequences (all reproduced):
  - *Apply Configuration* with a conflicting CH2 selection switches CH1's
    capacitors on the bench first and only then fails: the hardware is left
    in a gain state different from the last successful Apply, while the UI
    reports a generic failure.
  - A *rejected* selection stays in the internal book (`_selected_gain`):
    afterwards the other channel's selections are validated against the
    rejected wish instead of the actual hardware state, so valid choices can
    be rejected ("select matching gains") until the offending channel is
    re-applied.
  - With CH2 never selected, any CH1 change freely moves the shared pins;
    a later CH2 selection that needs the old state is then impossible until
    CH1 is re-applied.
- The wizard's linked-gain combos hide most of this in the normal flow
  (choosing for CH1 constrains CH2 before anything is sent), which is why
  the impact is limited there.
- **Fix direction:** compute the merged per-pin state for both channels
  before touching anything; reject the pair as a whole; send each device
  once. Wire the wizard to the pair-level apply.
- **Fixed (2026-09-10):** `HardwareManager.apply_gains(ch1_label, ch2_label)`
  merges requested pin states across all active channels and validates shared-pin
  agreement *before* touching hardware. If a conflict occurs, the request is
  rejected with no pin toggled and internal states remain intact. On success,
  each unique physical device receives `set_gpio_mask` exactly once. The UI
  dynamically filters CH2 options against CH1, requires a selection for every
  active channel, and badges ganged channels. Residual case: if one device
  accepts while another rejects, the bench state is unknowable —
  `HardwareManager.gain_state_unknown` is raised and the configuration must be
  applied again (the wizard shows this in the status bar).
  Regression tests: `tests/test_hardware_manager.py`.

### B20 — Gain table drops capacitors that share a pin — **FIXED**
- **Intended:** a pin may drive several switchable capacitors of a circuit
  (the loader allows it and warns "they switch together"); the gain table
  must treat the pin's capacitors as a group contributing the *sum* of their
  values.
- **Current:** `channel_routing`
  (`hardware/config/gain_model.py`) stores the wiring in a dict keyed by
  pin (`edges[pin] = capacitor`), so when several capacitors are wired to
  the same pin only the **last** edge survives. The gain table is computed
  from that single capacitor: e.g. a circuit with 22 pF fixed and
  c100+c560+c3300 all on one pin yields options 22 / 3322 pF instead of
  22 / 3982 pF. The new archive consistency check (settings.json vs
  embedded configuration) inherits the wrong table.
- Reproduced: gain table of a one-pin gain device with three wired
  capacitors contains 3322 pF instead of 3982 pF.
- **Fix direction:** group capacitors per pin in `channel_routing`
  ({pin: [capacitors]}) and let each pin's combination contribute the sum
  of its group's values; the gpio_mask semantics (one bit per pin) are
  unchanged.
- **Fixed (2026-09-10):** `channel_routing` in `hardware/config/gain_model.py`
  groups capacitors per pin (`edges.setdefault(pin, []).append(...)`). Each
  pin state in a gain option sums all wired capacitors.
  Regression test: `tests/test_hardware_manager.py`
  (`test_b20_multiple_capacitors_summed_per_pin`).

### B9 — `PICSerialDAQ.configure_ramp` is defined twice; the surviving one returns success — **FIXED**
- `hardware/pic/pic_daq.py:171` returns `False` (unsupported), but the second
  definition at `:264` overrides it and returns `True` "Ignoring". Direct calls
  would report success while doing nothing. Currently unreachable from the UI
  (ramp checkbox hidden for `supports_ramp_sampling = False`).
- **Fixed (2026-09-09):** the duplicate definition is gone; `configure_ramp`
  on the PIC returns `False` (unsupported).

### B10 — `PicoSerialDAQ.num_gpios_per_channel` returns 1, contradicting its own protocol — **FIXED**
- The Pico gain command drives 3 bits and `set_gpio_mask` handles 3 bits
  (`hardware/pico/pico_daq.py:94-96` vs `:98-125`). Dead code today (see B7) but
  wrong the moment it is used.
- **Fixed (2026-09-09):** `num_gpios_per_channel` is removed from the interface
  and the drivers; the pin count comes from the device's declared `gain.pins`
  in the hardware config (3 for the Pico, 1 for the PIC), validated against the
  driver's `GAIN_MAX_PINS` at config load.

### B11 — Version/name strings inconsistent — **FIXED**
- Intended app identity: **ATOMIQA 3.0**. `APP_VERSION = "v2.2.0"`
  (`core/constants.py`), `pyproject.toml` said `ATOMIQA 1.5`, and the PyInstaller bundle
  was named `MicroDosimeter` (`build.py`).
- **Fixed (2026-09-09):** single source `APP_VERSION = "3.0"` in
  `core/constants.py` (window title, session default, setup.json and
  settings.json); `pyproject.toml` at 3.0; PyInstaller bundle named
  `ATOMIQA`. Archives keep the version that recorded them (older ones
  legitimately show earlier versions).

### B12 — Default capacitor base values disagree with the shipped config — **FIXED**
- `SessionModel`/`ShiftService` default to `[22, 100, 560, 3000]` pF
  (`models/session_model.py:56-57`) while `hardware_config.json` uses 3300 pF for
  the largest switchable capacitor. Informational field only, but invites wrong
  analysis inputs when no workspace is loaded.
- **Fixed (2026-09-09):** the capacitor/ADC defaults are gone from
  `SessionModel`/`ShiftService`/`setup.json` — the hardware config is the single
  source of truth and every applied value is recorded per measurement.

### B13 — Frozen build resolves config/data paths against the CWD
- `hardware_config.json` and `data/pristine` are opened with relative paths
  (`app.py:44-46`, `controllers/setup_controller.py:39-40`). In the PyInstaller
  bundle `data/` is packed via `--add-data` (resolved against `_MEIPASS`), so the
  frozen app loses the Bragg data (and the hardware config) unless launched from
  the right directory.

### B14 — Legacy "starting time" recovery path is unreachable
- `_read_starting_time` looks for the `starting time :` marker in the first line
  (`controllers/measurement_controller.py:578-593`), but `load_file` rejects any
  first line that has neither the timing header nor a `timestamp` prefix, so the
  marker can never be found; the fallback is "now".

### B15 — Debug `stop_value` overflow handling differs between live and loaded data — **FIXED**
- Live conversion replaces overflow with NaN in the returned stop array
  (`core/data_utils.py:45-46`); the file loader keeps the raw 32768 values in
  `result["stop_value"]` (`core/load_file.py:98`). Cosmetic inconsistency between
  live and reloaded debug sessions.
- **Fixed (2026-09-24, with B6):** the loader now maps the sentinel of the loaded
  archive's own ADC depth to NaN in the returned stop array, matching the live
  conversion. Covered by `tests/test_adc_context.py`
  (`test_load_legacy_csv_keeps_32768_sentinel`).

### B16 — Pico gain state cache assumes CH1 is always configured before CH2 — **FIXED**
- `set_gpio_mask` offsets CH2's bits past `_ch1_mask_len`
  (`hardware/pico/pico_daq.py:106-113`); if CH2 were applied first it would land
  on bits 0-2 and be overwritten by CH1 later. `send_timing_config` happens to
  apply CH1 first, so this works today, but the coupling is fragile.
- **Fixed (2026-09-09):** `set_gpio_mask` is device-centric (one bool per
  declared pin, full state per call); the per-channel/per-bit offset cache is
  gone. The manager merges both channels' selections into a per-device pin
  state, so the call order no longer matters.

### B17 — Dead code and housekeeping — **PARTIALLY FIXED**
- Superseded pre-refactor drivers still in the tree: `services/daq_interface.py`,
  `services/pico_serial_daq.py`, `services/pic_serial_daq.py` (not imported
  anywhere), and `core/timing.py` (timing math moved into the PIC driver).
- `ITimingController.get_timing_limits()` implemented by both drivers, never
  called (the UI does not validate timing inputs against the hardware limits).
- `AnalysisService.calculate_acf/calculate_pacf` never called (ACF/PACF not wired
  into the running window; only MA + FFT are).
- `AppController._on_stop_measurement` (`app.py:92`) is never connected.
- `MeasurementController.calculate_results` contains an `if False:` block
  (`controllers/measurement_controller.py:674`) that disables the auto-update of
  analysis results inside a saved ZIP (`DataExporter.update_analysis_in_zip`).
- Duplicated `setCheckable(True)` calls in `views/mode_window.py:37-38,55-56`.
- **Partially fixed (2026-09-09):** `ITimingController.get_timing_limits()` (never
  called) is removed — drivers expose hardcoded limits via `HARDWARE_LIMITS` /
  the registry, and the UI uses the declared envelope from the config. The
  superseded drivers in `services/`, the unused ACF/PACF and the `if False:`
  block remain to be cleaned up.
