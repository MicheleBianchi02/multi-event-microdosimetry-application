from PyQt6.QtCore import QObject, pyqtSignal

from core.constants import (
    DEFAULT_ADC_BITS,
    TX_DELAY_DEBUG_DOUBLE,
    TX_DELAY_DEBUG_SINGLE,
    TX_DELAY_PRO_DOUBLE,
    TX_DELAY_PRO_SINGLE,
)


class SessionModel(QObject):
    """
    Holds the application-wide configuration state.
    Emits signals when important configuration changes, allowing controllers/views to react.
    """

    # Signals for state changes
    mode_changed = pyqtSignal(bool)  # emits (debug_mode)
    channel_mode_changed = pyqtSignal(bool)  # emits (multiple_channel)
    file_path_changed = pyqtSignal(str)
    workspace_changed = pyqtSignal(str)  # emits (active_workspace_dir)
    adc_bits_changed = pyqtSignal(int)  # emits (adc_bits) — histogram geometry changes
    adc_input_range_changed = pyqtSignal(float, float)  # emits (min, max) V — volt/charge scaling changes

    def __init__(self):
        super().__init__()
        self._int_time = 1
        self._reset_time = 1
        self._period_s = 0.0
        self._sample_freq = 1
        self._file_path = None
        self._tx_delay = TX_DELAY_PRO_DOUBLE
        self._multiple_channel = True
        # The app always acquires in debug mode (start/stop stored); it is
        # only set to False while an old operational-mode archive is loaded.
        self._debug_mode = True
        # What the timing controller generates during a measurement:
        # 'internal' (classic no-ramp on the device's own ADC), 'ramp'
        # (ramp-sampling triggers), or 'markers' (integration-window markers
        # for an external digitizer = degenerate ramp with dt = int).
        self.timing_mode = "internal"
        # The APPLIED (quantized) ramp-sampling interval in seconds when
        # timing_mode is 'ramp'; 0.0 otherwise.  Set from the timing
        # driver's exact mirror (ActualTiming.ramp_dt_eff_us) — never from
        # the requested dt, so every consumer (the NI reader's expected
        # rate, the AQR1 header) sees what the generator actually emits.
        self.ramp_dt_s = 0.0
        # Samples (CONV_START edges) per integration window the generator
        # will emit, from the driver's exact mirror
        # (ActualTiming.samples_per_window); 0 when not in ramp/markers
        # mode.  The ramp sink chunks on exactly this number — deriving it
        # downstream from int/dt is lossy and misaligns the window grid.
        self.samples_per_window = 0

        self.baud_rate = 115200
        self.port = None

        self.rois = None
        self.time_rois = []
        self.calibration_slope = 1.0
        self.calibration_intercept = 0.0

        self._single_event = False
        self._increased_gain = False
        self.capacitance_pf_ch1 = 100.0
        self.capacitance_pf_ch2 = 100.0

        # ADC resolution of the active context (B6): set from the loaded
        # hardware configuration at Apply Configuration and from the archive
        # settings at load time; DEFAULT_ADC_BITS for simulation/legacy.
        # Drives histogram bins and the overflow sentinel app-wide.
        self._adc_bits = DEFAULT_ADC_BITS
        # The ADC input range APPLIED on the acquisition device (B23):
        # (min_volts, max_volts) from HardwareManager.get_adc_settings() at
        # Apply Configuration and from hardware_settings.adc.input_range_volts
        # when an archive is loaded; None = unknown (simulation / legacy
        # archives) — the running window then keeps its editable default.
        # Drives the counts→volt/charge scaling.
        self._adc_input_range = None

        # --- Workspace & Shift Setup Properties (Optional) ---
        from core.constants import APP_VERSION
        self._active_workspace_dir = None
        self.setup_file_name = "setup.json"  # may carry any name; set on load/save
        self.facility = "TIFPA"
        self.beam_type = "Biological"
        self.particle = "Proton"
        self.app_version = APP_VERSION
        self.shift_note = ""
        # ADC settings and capacitor values are no longer session defaults:
        # they come from the loaded hardware configuration (B6/B12), and the
        # applied values are recorded per measurement (gpio_mask).

        # --- Measurement Specific Properties (Optional) ---
        self.beam_energy_mev = 0.0
        self.beam_current_na = 0.0
        self.detector_voltage_v = 0.0
        self.depth_cm = 0.0
        self.measurement_note = ""
        self.is_problematic = False
        self.gpio_mask_ch1 = None  # applied per-channel pin states (B4)
        self.gpio_mask_ch2 = None
        self.timestamp_start = None
        self.timestamp_end = None

    @property
    def active_workspace_dir(self):
        return self._active_workspace_dir

    @active_workspace_dir.setter
    def active_workspace_dir(self, value):
        if self._active_workspace_dir != value:
            self._active_workspace_dir = value
            self.workspace_changed.emit(value if value else "")

    @property
    def single_event(self):
        return self._single_event

    @single_event.setter
    def single_event(self, value):
        self._single_event = value

    @property
    def adc_bits(self) -> int:
        """ADC resolution of the active context (hardware config or loaded
        archive); see DataModel.configure_adc for the downstream effects."""
        return self._adc_bits

    @adc_bits.setter
    def adc_bits(self, value: int):
        value = int(value)
        if value != self._adc_bits:
            self._adc_bits = value
            self.adc_bits_changed.emit(value)

    @property
    def adc_input_range(self):
        """(min_volts, max_volts) of the applied ADC input range, or None
        when unknown (see the initializer note) — the running window's
        volt/charge scaling follows it (B23)."""
        return self._adc_input_range

    @adc_input_range.setter
    def adc_input_range(self, value):
        if value is not None:
            value = (float(value[0]), float(value[1]))
        if value != self._adc_input_range:
            self._adc_input_range = value
            if value is not None:
                self.adc_input_range_changed.emit(*value)

    @property
    def increased_gain(self):
        return self._increased_gain

    @increased_gain.setter
    def increased_gain(self, value):
        self._increased_gain = value

    @property
    def int_time(self):
        return self._int_time

    @int_time.setter
    def int_time(self, value):
        self._int_time = value

    @property
    def reset_time(self):
        return self._reset_time

    @reset_time.setter
    def reset_time(self, value):
        self._reset_time = value

    @property
    def sample_freq(self):
        return self._sample_freq

    @sample_freq.setter
    def sample_freq(self, value):
        self._sample_freq = value

    @property
    def period_s(self):
        """The actual (quantized) measurement period in seconds, as reported
        by the timing driver (0.0 = unknown — derive from int + reset)."""
        return self._period_s

    @period_s.setter
    def period_s(self, value):
        self._period_s = value

    @property
    def sample_period_s(self) -> float:
        """Seconds between consecutive measurement samples as the hardware
        actually ran: 1 / the applied sample frequency — the timing
        driver's period, which includes every dead time the driver knows
        (settle, generator overhead) and is the same basis as the archived
        data.csv timestamps — falling back to the applied period, then to
        int + reset for legacy sessions carrying neither.

        Never re-derive it as int + reset: outside internal timing the real
        period is reset + settle + N·dt (in markers mode up to ~4x the sum,
        PIO generator settle included), and the time chart's axis would
        drift against the archived timestamps."""
        if self._sample_freq and self._sample_freq > 0.0:
            return 1.0 / self._sample_freq
        if self._period_s and self._period_s > 0.0:
            return self._period_s
        return self._int_time + self._reset_time

    @property
    def file_path(self):
        return self._file_path

    @file_path.setter
    def file_path(self, value):
        if self._file_path != value:
            self._file_path = value
            self.file_path_changed.emit(value)

    @property
    def multiple_channel(self):
        return self._multiple_channel

    @multiple_channel.setter
    def multiple_channel(self, value):
        if value:
            if self._debug_mode:
                self._tx_delay = TX_DELAY_DEBUG_DOUBLE
            else:
                self._tx_delay = TX_DELAY_PRO_DOUBLE
        else:
            if self._debug_mode:
                self._tx_delay = TX_DELAY_DEBUG_SINGLE
            else:
                self._tx_delay = TX_DELAY_PRO_SINGLE

        if self._multiple_channel != value:
            self._multiple_channel = value
            self.channel_mode_changed.emit(value)

    @property
    def debug_mode(self):
        return self._debug_mode

    @debug_mode.setter
    def debug_mode(self, value):
        if value:
            if self._multiple_channel:
                self._tx_delay = TX_DELAY_DEBUG_DOUBLE
            else:
                self._tx_delay = TX_DELAY_DEBUG_SINGLE
        else:
            if self._multiple_channel:
                self._tx_delay = TX_DELAY_PRO_DOUBLE
            else:
                self._tx_delay = TX_DELAY_PRO_SINGLE

        if self._debug_mode != value:
            self._debug_mode = value
            self.mode_changed.emit(value)

    @property
    def tx_delay(self):
        return self._tx_delay
