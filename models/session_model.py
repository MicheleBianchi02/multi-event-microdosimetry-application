from PyQt6.QtCore import QObject, pyqtSignal

from core.constants import (
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
