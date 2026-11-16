import logging

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from views.analysis_widget import AnalysisWidget
from hardware.pico.pico_daq import (
    TRANSIENT_TEST_INT_MAX_US,
    TRANSIENT_TEST_SILENT_MAX_US,
)

logger = logging.getLogger(__name__)

# Set global PyQtGraph theme to white background, black foreground
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


class TimeWindow(QWidget):
    sent_successfull = pyqtSignal()
    live_ramp_requested = pyqtSignal()
    editing_time = pyqtSignal()
    apply_configuration_requested = pyqtSignal(dict)
    timing_calculation_requested = pyqtSignal(float, float, object, object, bool, bool)

    def __init__(self, session_model, simulate: bool, hardware_manager=None) -> None:
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        self.already_sent = False
        self._adjusting_linked_gain = False
        self._ramp_allowed = False
        self._timing_limits = None   # declared envelope of the timing device
        self._inputs_valid = False

        # Core layout uses a stacked widget
        self.main_layout = QVBoxLayout(self)
        self.stacked_widget = QStackedWidget()
        self.main_layout.addWidget(self.stacked_widget)

        self.analysis_widget = AnalysisWidget()
        self.analysis_widget.finished.connect(self.go_to_manual_config)
        self.stacked_widget.addWidget(self.analysis_widget)

        self.manual_config_widget = QWidget()
        self.stacked_widget.addWidget(self.manual_config_widget)

        self.init_ui()
        self.reload_hardware()
        if self.hardware_manager is not None:
            self.hardware_manager.add_config_listener(
                lambda _manager: self.reload_hardware()
            )
        self.stacked_widget.setCurrentIndex(0)  # Start with analysis

    def go_to_manual_config(self):
        self.stacked_widget.setCurrentIndex(1)

    def init_ui(self):
        self.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 11pt;
                border: 1px solid #ced4da;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 12px;
                background-color: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
                color: #2b3035;
            }
            QLineEdit, QComboBox {
                padding: 5px 8px;
                font-size: 11pt;
                border: 1px solid #ced4da;
                border-radius: 4px;
                background-color: #ffffff;
                color: #212529;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #007bff;
            }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                color: #212529;
                border: 1px solid #ced4da;
                selection-background-color: #007bff;
                selection-color: #ffffff;
            }
        """)
        self.icon_error = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxCritical
        )
        self.icon_success = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogApplyButton
        )

        # --- Live Hardware Timing Summary Frame ---
        self.summary_frame = QFrame()
        self.summary_frame.setStyleSheet("""
            QFrame {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        self.final_int_time_label = QLabel("-")
        self.final_rst_time_label = QLabel("-")
        self.final_period_label = QLabel("-")
        self.final_dt_label = QLabel("-")
        self.final_freq_label = QLabel("-")

        for lbl in [
            self.final_int_time_label,
            self.final_rst_time_label,
            self.final_period_label,
            self.final_dt_label,
            self.final_freq_label,
        ]:
            lbl.setStyleSheet("font-size: 14pt; font-weight: bold; color: #1a252f;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # --- Timing Inputs ---
        self.int_time = QLineEdit("1")
        self.rst_time = QLineEdit("30")
        self.period_time = QLineEdit("")

        self.magnitude_int = QComboBox()
        self.magnitude_int.setFixedWidth(65)
        self.magnitude_int.addItems(["s", "ms", "μs"])
        self.magnitude_int.setCurrentIndex(1)  # ms

        self.magnitude_rst = QComboBox()
        self.magnitude_rst.setFixedWidth(65)
        self.magnitude_rst.addItems(["s", "ms", "μs"])
        self.magnitude_rst.setCurrentIndex(2)  # μs

        self.magnitude_period = QComboBox()
        self.magnitude_period.setFixedWidth(65)
        self.magnitude_period.addItems(["s", "ms", "μs"])
        self.magnitude_period.setCurrentIndex(1)  # ms

        self.period_widget = QWidget()
        period_row = QHBoxLayout(self.period_widget)
        period_row.setContentsMargins(0, 0, 0, 0)
        period_row.addWidget(self.period_time, 1)
        period_row.addWidget(self.magnitude_period)

        # Signals for timing inputs
        for w in [self.int_time, self.rst_time, self.period_time]:
            w.textEdited.connect(self.on_manual_edit)
        for c in [self.magnitude_int, self.magnitude_rst, self.magnitude_period]:
            c.currentIndexChanged.connect(self.on_manual_edit)

        # --- Gain Inputs ---
        self.capacitance_combo_ch1 = QComboBox()
        self.capacitance_combo_ch2 = QComboBox()
        self.capacitance_combo_ch1.currentIndexChanged.connect(self.on_cap_change)
        self.capacitance_combo_ch2.currentIndexChanged.connect(self.on_cap_change)

        self.ch2_gain_container = QWidget()
        ch2_layout = QVBoxLayout(self.ch2_gain_container)
        ch2_layout.setContentsMargins(0, 0, 0, 0)
        ch2_layout.setSpacing(2)
        ch2_layout.addWidget(self.capacitance_combo_ch2)
        self.ch2_hint_label = QLabel("")
        self.ch2_hint_label.setStyleSheet("color: #6c757d; font-size: 9pt; font-style: italic;")
        ch2_layout.addWidget(self.ch2_hint_label)

        # --- Ramp Sampling ---
        self.sample_ramp_cb = QCheckBox("Enable Ramp Sampling")
        self.sample_ramp_cb.setStyleSheet("QCheckBox { font-size: 11pt; font-weight: bold; }")
        self.sample_ramp_cb.stateChanged.connect(self.on_manual_edit)

        self.ramp_dt_widget = QWidget()
        ramp_dt_layout = QHBoxLayout(self.ramp_dt_widget)
        ramp_dt_layout.setContentsMargins(0, 0, 0, 0)
        ramp_dt_layout.addWidget(QLabel("Ramp Δt (μs):"))
        self.ramp_dt_input = QLineEdit("10")
        self.ramp_dt_input.setFixedWidth(90)
        self.ramp_dt_input.textEdited.connect(self.on_manual_edit)
        ramp_dt_layout.addWidget(self.ramp_dt_input)
        ramp_dt_layout.addStretch(1)

        # --- Transient Test (study mode; Pico 2.3+ timing only) ---
        # dt pinned by an integer PIO divider (16 at 150 MHz -> 1.0667 us =
        # 937.5 kS/s; exactly 1.000 us would run the acquisition card at
        # its absolute 1 MS/s spec with zero margin), reset sweepable down
        # to 1 us (below the validated measurement minimum), a one-tick
        # (~107 ns) settle before the first conversion edge: samples the
        # ramp right at the reset release.  The TRUE RAMP Δt tile shows
        # the applied (quantized) dt.
        self.transient_test_cb = QCheckBox("Transient Test (Δt ≈ 1.07 μs, reset from 1 μs)")
        self.transient_test_cb.setStyleSheet("QCheckBox { font-size: 10pt; }")
        self.transient_test_cb.setToolTip(
            "Study mode: the timing device pins dt to 1.0667 us (nominal "
            "1 us — exactly 1.000 us would clock the acquisition card at "
            "its absolute 1 MS/s limit with zero margin) and generates "
            "the first CONV_START edge one ~107 ns tick after the reset "
            "release, so the front-end transient is visible in the ramp "
            "data. The reset time becomes settable from 1 us (below the "
            "validated measurement minimum — never use this mode for "
            "measurements). The TRUE RAMP Δt tile shows the applied value."
        )
        self.transient_test_cb.stateChanged.connect(self._on_transient_toggled)

        # --- Transient Test, sampling DURING the reset (study mode) ---
        # Same generator family, pulses while RESET is LOW.  The two
        # time fields are SWAPPED in this mode: Integration Time drives
        # the sampled (pulsed) reset phase, Reset Time the silent
        # integration.
        self.transient_reset_cb = QCheckBox("Sample During Reset (times swapped)")
        self.transient_reset_cb.setStyleSheet("QCheckBox { font-size: 10pt; }")
        self.transient_reset_cb.setToolTip(
            "Study mode: CONV_START pulses are generated DURING the reset "
            "phase (the first edge coincides with the reset assertion) and "
            "the integration phase is silent. The field meanings are "
            "swapped: Integration Time = the sampled reset phase (from "
            "1 us, one sample per dt ≈ 1.07 us), Reset Time = the silent "
            "integration."
        )
        self.transient_reset_cb.stateChanged.connect(self._on_transient_reset_toggled)

        # --- Measurement & Beam Parameters Inputs ---
        self.beam_energy_input = QLineEdit(str(self.session.beam_energy_mev))
        self.beam_current_input = QLineEdit(str(self.session.beam_current_na))
        self.det_voltage_input = QLineEdit(str(self.session.detector_voltage_v))
        self.depth_input = QLineEdit(str(self.session.depth_cm))

        self.adc_range_combo = QComboBox()
        self.adc_range_label = QLabel("0.0 V to 5.0 V (Fixed)")
        self.adc_range_label.setStyleSheet("color: #495057; font-size: 11pt; padding: 5px 0;")

        self.meas_note_input = QTextEdit(self.session.measurement_note)
        self.meas_note_input.setMaximumHeight(70)

        for w in [self.beam_energy_input, self.beam_current_input, self.det_voltage_input, self.depth_input]:
            w.textEdited.connect(self.on_meta_edit)
        self.meas_note_input.textChanged.connect(self.on_meta_edit)

        # --- Action Buttons & Status ---
        self.send_bnt = QPushButton(" Apply Configuration")
        self.send_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_bnt.setStyleSheet("""
            QPushButton {
                background-color: #28a745; color: white; font-weight: bold; 
                padding: 10px 22px; font-size: 12pt; border-radius: 5px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #6c757d; }
        """)
        self.send_bnt.clicked.connect(self.on_send_press)

        self.edit_bnt = QPushButton("Edit")
        self.edit_bnt.setVisible(False)
        self.edit_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_bnt.setStyleSheet("""
            QPushButton {
                background-color: #ffc107; color: #333; font-weight: bold; 
                padding: 10px 22px; font-size: 12pt; border-radius: 5px;
            }
            QPushButton:hover { background-color: #e0a800; }
        """)
        self.edit_bnt.clicked.connect(self.on_edit_press)

        # Live ramp analysis: stream ramps through memory and keep only the
        # results (averaged spectrum + CDS series) — the campaign mode for
        # long ramps where recording every sample would fill the disk.
        # Enabled only with the timing applied in ramp mode (markers mode
        # holds 2 samples/window: no waveform to analyze).
        self.live_ramp_bnt = QPushButton("Live Ramp Analysis…")
        self.live_ramp_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.live_ramp_bnt.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8; color: white; font-weight: bold;
                padding: 10px 18px; font-size: 12pt; border-radius: 5px;
            }
            QPushButton:hover { background-color: #138496; }
            QPushButton:disabled { background-color: #6c757d; }
        """)
        self.live_ramp_bnt.setEnabled(False)
        self.live_ramp_bnt.setToolTip(
            "Stream ramps through memory and keep only the results "
            "(averaged FFT + correlated double sampling).\n\n"
            "Requires the timing configuration applied with ramp "
            "sampling enabled."
        )
        self.live_ramp_bnt.clicked.connect(self.live_ramp_requested.emit)

        self.status_icon = QLabel()
        self.status_icon.setStyleSheet("border: none;")
        self.status_label = QLabel("Ready to configure")
        self.status_label.setStyleSheet(
            "color: #666; font-size: 11pt; font-style: italic; border: none;"
        )
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet(
            "background-color: #f8f9fa; border-radius: 4px; padding: 5px;"
        )
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.addWidget(self.status_icon)
        status_layout.addWidget(self.status_label, 1)
        self.status_frame.setVisible(False)

        self.setup_layout()

        # Trigger initial calculation
        self.on_manual_edit()

    def setup_layout(self):
        outer_master = QHBoxLayout(self.manual_config_widget)
        outer_master.addStretch(1)
        master_container = QWidget()
        master_container.setMinimumWidth(860)
        master_container.setMaximumWidth(1060)
        master = QVBoxLayout(master_container)
        master.setSpacing(12)
        outer_master.addWidget(master_container)
        outer_master.addStretch(1)

        self.btn_back = QPushButton("← Back to Analysis")
        self.btn_back.setStyleSheet(
            "background-color: #6c757d; color: white; font-weight: bold; padding: 6px 14px; font-size: 11pt; border-radius: 4px;"
        )
        self.btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        master.addWidget(self.btn_back, alignment=Qt.AlignmentFlag.AlignLeft)

        # Scrollable content area for cards
        scroll = QScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(12)

        # --- Side-by-Side Cards ---
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(14)

        # Card 1: Timing & Gain Configuration (Left)
        time_group = QGroupBox("Timing & Gain Configuration")
        t_form = QFormLayout(time_group)
        t_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        t_form.setSpacing(10)

        # Integration Time Row
        r_int = QHBoxLayout()
        r_int.addWidget(self.int_time, 1)
        r_int.addWidget(self.magnitude_int)
        t_form.addRow("Integration Time:", r_int)

        # Reset Time Row
        r_rst = QHBoxLayout()
        r_rst.addWidget(self.rst_time, 1)
        r_rst.addWidget(self.magnitude_rst)
        t_form.addRow("Reset Time:", r_rst)

        # Period Row (visible only when configurable)
        self.period_label = QLabel("Period:")
        t_form.addRow(self.period_label, self.period_widget)

        # CH1 Gain Row
        t_form.addRow("CH 1 Gain:", self.capacitance_combo_ch1)

        # CH2 Gain Row (visible only when CH2 active)
        self.ch2_label = QLabel("CH 2 Gain:")
        t_form.addRow(self.ch2_label, self.ch2_gain_container)

        # Ramp Sampling Row
        t_form.addRow("", self.sample_ramp_cb)
        t_form.addRow("", self.ramp_dt_widget)
        t_form.addRow("", self.transient_test_cb)
        t_form.addRow("", self.transient_reset_cb)

        cards_layout.addWidget(time_group, 1)

        # Card 2: Measurement & Beam Parameters (Right)
        meta_group = QGroupBox("Measurement Parameters (settings.json)")
        m_form = QFormLayout(meta_group)
        m_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        m_form.setSpacing(10)

        m_form.addRow("Beam Energy (MeV):", self.beam_energy_input)
        m_form.addRow("Beam Current (nA):", self.beam_current_input)
        m_form.addRow("Detector Bias (V):", self.det_voltage_input)
        m_form.addRow("Depth in Phantom (cm):", self.depth_input)

        # ADC Range Row
        adc_range_container = QWidget()
        adc_layout = QVBoxLayout(adc_range_container)
        adc_layout.setContentsMargins(0, 0, 0, 0)
        adc_layout.addWidget(self.adc_range_combo)
        adc_layout.addWidget(self.adc_range_label)
        m_form.addRow("ADC Input Range:", adc_range_container)

        m_form.addRow("Measurement Note:", self.meas_note_input)

        cards_layout.addWidget(meta_group, 1)

        content_layout.addLayout(cards_layout)

        # --- Live Hardware Timing Summary Dashboard ---
        sum_layout = QHBoxLayout(self.summary_frame)
        sum_layout.setContentsMargins(10, 8, 10, 8)

        def create_stat_tile(title, widget):
            v = QVBoxLayout()
            t = QLabel(title)
            t.setStyleSheet(
                "color: #6c757d; font-size: 9pt; font-weight: bold; letter-spacing: 0.5px;"
            )
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(t)
            v.addWidget(widget)
            return v

        sum_layout.addLayout(create_stat_tile("TRUE INTEGRATION", self.final_int_time_label))
        sum_layout.addLayout(create_stat_tile("TRUE RESET TIME", self.final_rst_time_label))
        sum_layout.addLayout(create_stat_tile("TRUE PERIOD", self.final_period_label))
        sum_layout.addLayout(create_stat_tile("TRUE RAMP Δt", self.final_dt_label))
        sum_layout.addLayout(create_stat_tile("SAMPLE FREQUENCY", self.final_freq_label))

        content_layout.addWidget(self.summary_frame)

        scroll.setWidget(content_widget)
        master.addWidget(scroll, 1)

        # Footer (Status + Action Buttons)
        footer = QHBoxLayout()
        footer.addWidget(self.status_frame, 1)
        footer.addWidget(self.edit_bnt)
        footer.addWidget(self.send_bnt)
        footer.addWidget(self.live_ramp_bnt)
        master.addLayout(footer)

    def on_meta_edit(self):
        try:
            self.session.beam_energy_mev = float(self.beam_energy_input.text() or 0.0)
        except ValueError:
            pass

        try:
            self.session.beam_current_na = float(self.beam_current_input.text() or 0.0)
        except ValueError:
            pass

        try:
            self.session.detector_voltage_v = float(self.det_voltage_input.text() or 0.0)
        except ValueError:
            pass

        try:
            self.session.depth_cm = float(self.depth_input.text() or 0.0)
        except ValueError:
            pass

        self.session.measurement_note = self.meas_note_input.toPlainText()

    def _transient_mode(self):
        """Which transient study mode is active: 'integration' (pulses
        during the ramp), 'reset' (pulses during the reset LOW, the two
        time fields swapped), or None (normal timing)."""
        for mode, cb in (("integration", self.transient_test_cb),
                         ("reset", self.transient_reset_cb)):
            if cb.isVisible() and cb.isEnabled() and cb.isChecked():
                return mode
        return None

    def _transient_active(self) -> bool:
        """Backward-compatible boolean view of _transient_mode()."""
        return self._transient_mode() is not None

    def _on_transient_toggled(self, _state: int):
        """Checking a study mode forces the ramp path with dt pinned by
        the firmware's integer divider (nominal 1 us, applied 1.0667 us —
        shown in the TRUE RAMP Δt tile); the two modes are mutually
        exclusive; unchecking restores normal editing."""
        if self.transient_test_cb.isChecked():
            self.transient_reset_cb.blockSignals(True)
            self.transient_reset_cb.setChecked(False)
            self.transient_reset_cb.blockSignals(False)
            self.sample_ramp_cb.setChecked(True)
            self.ramp_dt_input.setText("1")
        self.ramp_dt_input.setEnabled(self._transient_mode() is None)
        self.on_manual_edit()

    def _on_transient_reset_toggled(self, _state: int):
        """Same as _on_transient_toggled for the reset-sampling mode."""
        if self.transient_reset_cb.isChecked():
            self.transient_test_cb.blockSignals(True)
            self.transient_test_cb.setChecked(False)
            self.transient_test_cb.blockSignals(False)
            self.sample_ramp_cb.setChecked(True)
            self.ramp_dt_input.setText("1")
        self.ramp_dt_input.setEnabled(self._transient_mode() is None)
        self.on_manual_edit()

    def _collect_timing_values(self):
        """Parse the timing fields exactly as typed (seconds for the times,
        µs for the ramp dt).  Single source for the preview AND for Apply:
        both must feed the driver's quantization mirror identical inputs —
        re-sending the preview's already-quantized session values would
        quantize a second time (one integration interval can be lost on
        the PIO generator).  Raises ValueError on unparsable input."""
        # Integration
        t_int = self._sanitize_number(self.int_time.text())
        self.int_time.setText(t_int)
        val_int = float(t_int)
        mag_i = self.magnitude_int.currentText()
        mult_i = 1.0 if mag_i == "s" else (1e-3 if mag_i == "ms" else 1e-6)
        final_int = val_int * mult_i

        # Reset
        t_rst = self._sanitize_number(self.rst_time.text())
        self.rst_time.setText(t_rst)
        val_rst = float(t_rst)
        mag_r = self.magnitude_rst.currentText()
        mult_r = 1.0 if mag_r == "s" else (1e-3 if mag_r == "ms" else 1e-6)
        final_rst = val_rst * mult_r

        # Period (if configurable)
        final_period = None
        if self.period_widget.isVisible() and self.period_time.text().strip():
            t_prd = self._sanitize_number(self.period_time.text())
            self.period_time.setText(t_prd)
            if t_prd:
                val_prd = float(t_prd)
                mag_p = self.magnitude_period.currentText()
                mult_p = 1.0 if mag_p == "s" else (1e-3 if mag_p == "ms" else 1e-6)
                final_period = val_prd * mult_p

        # Ramp dt travels with the request: the true-values preview is
        # ramp-aware (generator selection and quantization depend on it).
        # An unparsable dt yields None here (the preview falls back to the
        # no-ramp numbers) — _validate_timing_inputs flags the field red
        # and blocks Apply separately.
        final_dt = None
        if self.sample_ramp_cb.isVisible() and self.sample_ramp_cb.isChecked():
            if self._transient_active():
                final_dt = 1.0     # pinned by the transient-test firmware
            else:
                try:
                    final_dt = float(self.ramp_dt_input.text())
                except ValueError:
                    final_dt = None

        return final_int, final_rst, final_period, final_dt

    def on_manual_edit(self):
        self.already_sent = False
        self.editing_time.emit()
        self.send_bnt.setEnabled(False)

        try:
            final_int, final_rst, final_period, final_dt = self._collect_timing_values()
            # Validate against the declared envelope of the loaded hardware
            # config; out-of-bounds inputs are highlighted and block Apply
            # (the drivers would reject them at configure time).
            self._inputs_valid = self._validate_timing_inputs(final_int, final_rst, final_period)
            mode = self._transient_mode()
            self.timing_calculation_requested.emit(
                final_int, final_rst, final_period, final_dt,
                mode == "integration", mode == "reset",
            )
        except ValueError:
            self._inputs_valid = False
        self._update_apply_enabled()

    @staticmethod
    def _sanitize_number(text: str) -> str:
        cleaned = "".join(c for c in text if c.isdigit() or c == ".")
        if cleaned.count(".") > 1:
            first = cleaned.find(".")
            cleaned = cleaned[: first + 1] + cleaned[first + 1 :].replace(".", "")
        return cleaned

    # ------------------------------------------------------------------
    # Declared-envelope helpers
    # ------------------------------------------------------------------

    def _timing_param(self, name: str) -> dict:
        """The declared shape of one timing parameter: {min,max,step},
        {value}, or {} when not declared."""
        if name == "ramp_dt_us":
            ramp = (self._timing_limits or {}).get("ramp_sampling") or {}
            return ramp.get("dt_us") or {}
        return (self._timing_limits or {}).get(name) or {}

    def _field_within_envelope(self, line_edit: QLineEdit, value_us: float, name: str) -> bool:
        """Mark the field red when the value violates the declared bounds.
        Fixed-value parameters are always valid (the field is read-only)."""
        bounds = self._timing_param(name)
        valid = True
        if "min" in bounds:
            valid = bounds["min"] <= value_us <= bounds["max"]
        line_edit.setStyleSheet("" if valid else "border: 1px solid #dc3545;")
        return valid

    def _validate_timing_inputs(self, final_int_s: float, final_rst_s: float, final_period_s: float) -> bool:
        valid = True
        mode = self._transient_mode()
        if mode == "integration":
            # Test mode: the reset may go below the declared envelope floor
            # (the firmware sweep starts at 1 us); the INTEGRATION may go
            # far above the 10 s measurement envelope (tens of seconds and
            # more — the generators' 32-bit pulse counts allow ~70 min).
            bounds = self._timing_param("reset_time_us")
            rst_ok = final_rst_s * 1e6 >= 1.0
            if "max" in bounds:
                rst_ok &= final_rst_s * 1e6 <= bounds["max"]
            self.rst_time.setStyleSheet("" if rst_ok else "border: 1px solid #dc3545;")
            valid &= rst_ok
            int_ok = 5.0 <= final_int_s * 1e6 <= TRANSIENT_TEST_INT_MAX_US
            self.int_time.setStyleSheet("" if int_ok else "border: 1px solid #dc3545;")
            valid &= int_ok
        elif mode == "reset":
            # Swapped: the INTEGRATION field drives the sampled pulsed
            # reset phase (floor 1 us); the RESET field drives the silent
            # integration (normal declared bounds).
            # Swapped: the INTEGRATION field drives the sampled pulsed
            # reset phase (1 us .. 1 h of pulsed train); the RESET field
            # the silent integration (1 us .. 5 min in ~107 ns ticks).
            int_ok = 1.0 <= final_int_s * 1e6 <= TRANSIENT_TEST_INT_MAX_US
            self.int_time.setStyleSheet("" if int_ok else "border: 1px solid #dc3545;")
            valid &= int_ok
            rst_ok = 1.0 <= final_rst_s * 1e6 <= TRANSIENT_TEST_SILENT_MAX_US
            self.rst_time.setStyleSheet("" if rst_ok else "border: 1px solid #dc3545;")
            valid &= rst_ok
        else:
            valid &= self._field_within_envelope(self.int_time, final_int_s * 1e6, "integration_time_us")
            valid &= self._field_within_envelope(self.rst_time, final_rst_s * 1e6, "reset_time_us")
        if final_period_s is not None:
            valid &= self._field_within_envelope(self.period_time, final_period_s * 1e6, "period_us")

        if self.sample_ramp_cb.isVisible() and self.sample_ramp_cb.isChecked():
            dt_valid = True
            try:
                dt_us = float(self.ramp_dt_input.text())
                dt_bounds = self._timing_param("ramp_dt_us")
                if "min" in dt_bounds:
                    dt_valid = dt_bounds["min"] <= dt_us <= dt_bounds["max"]
            except ValueError:
                dt_valid = False
            self.ramp_dt_input.setStyleSheet("" if dt_valid else "border: 1px solid #dc3545;")
            valid &= dt_valid
        else:
            self.ramp_dt_input.setStyleSheet("")
        return bool(valid)

    def _ch2_active(self) -> bool:
        manager = self.hardware_manager
        return bool(manager and manager.is_loaded and "ch2" in manager.active_channels)

    def _selections_complete(self) -> bool:
        """All active channels must have a gain selected before Apply.
        Without a loaded config (simulation) there is nothing to enforce."""
        manager = self.hardware_manager
        if not (manager and manager.is_loaded):
            return True
        if not self.capacitance_combo_ch1.currentText():
            return False
        if self._ch2_active() and not self.capacitance_combo_ch2.currentText():
            return False
        return True

    def _update_apply_enabled(self):
        ok = self._inputs_valid and self._selections_complete()
        self.send_bnt.setEnabled(ok)
        self.send_bnt.setToolTip(
            ""
            if ok
            else "Fix the highlighted inputs and select a gain for every active channel first."
        )

    def on_cap_change(self):
        self.already_sent = False
        if not self._adjusting_linked_gain:
            self._adjusting_linked_gain = True
            self._update_ch2_gain_options()
            self._adjusting_linked_gain = False
        self._update_apply_enabled()

    def _update_ch2_gain_options(self):
        if not self._ch2_active():
            return

        ch1_label = self.capacitance_combo_ch1.currentText()
        compatible_labels = self.hardware_manager.get_compatible_gain_labels(2, ch1_label)

        prev_selected = self.capacitance_combo_ch2.currentText()
        self.capacitance_combo_ch2.blockSignals(True)
        self.capacitance_combo_ch2.clear()
        self.capacitance_combo_ch2.addItems(compatible_labels)

        if prev_selected in compatible_labels:
            self.capacitance_combo_ch2.setCurrentText(prev_selected)
        elif compatible_labels:
            self.capacitance_combo_ch2.setCurrentIndex(0)
        self.capacitance_combo_ch2.blockSignals(False)

        # Tooltip / badge hints
        shared = self.hardware_manager.channels_share_gain
        if not compatible_labels:
            self.capacitance_combo_ch2.setEnabled(False)
            self.ch2_hint_label.setText(
                "No CH2 gain is compatible with the CH1 selection — pick another CH1 gain."
            )
        elif shared and len(compatible_labels) <= 1:
            self.capacitance_combo_ch2.setEnabled(False)
            self.ch2_hint_label.setText("Ganged pins: CH2 follows CH1")
        elif shared:
            self.capacitance_combo_ch2.setEnabled(True)
            self.ch2_hint_label.setText("Shared pins: showing options compatible with CH1")
        else:
            self.capacitance_combo_ch2.setEnabled(True)
            self.ch2_hint_label.setText("")

    def reload_hardware(self):
        """Rebuild the hardware-dependent parts after a config load/unload."""
        manager = self.hardware_manager
        is_loaded = manager is not None and manager.is_loaded

        ch2_active = is_loaded and "ch2" in manager.active_channels

        # Gain CH1
        self.capacitance_combo_ch1.blockSignals(True)
        self.capacitance_combo_ch1.clear()
        if is_loaded:
            self.capacitance_combo_ch1.addItems(manager.get_gain_labels(1))
        self.capacitance_combo_ch1.blockSignals(False)

        # Gain CH2 visibility
        self.ch2_label.setVisible(ch2_active)
        self.ch2_gain_container.setVisible(ch2_active)
        self._update_ch2_gain_options()

        # Period input visibility: bounds -> editable, fixed -> read-only
        # display, not declared (derived by the firmware) -> hidden.
        self._timing_limits = manager.declared_timing_limits() if is_loaded else None
        period_cfg = self._timing_param("period_us")
        period_declared = bool(period_cfg)
        self.period_label.setVisible(period_declared)
        self.period_widget.setVisible(period_declared)
        if period_declared:
            if "value" in period_cfg:
                self._show_fixed_value(self.period_time, self.magnitude_period, period_cfg["value"])
            elif "min" in period_cfg:
                self.period_time.setToolTip(
                    f"Configurable bounds: {period_cfg['min']:g} to {period_cfg['max']:g} μs"
                )

        # Fixed-by-firmware timing parameters: displayed, not settable.
        for name, line_edit, combo in (
            ("integration_time_us", self.int_time, self.magnitude_int),
            ("reset_time_us", self.rst_time, self.magnitude_rst),
        ):
            fixed = self._timing_param(name)
            if "value" in fixed:
                self._show_fixed_value(line_edit, combo, fixed["value"])
            else:
                line_edit.setReadOnly(False)

        # Ramp sampling controls
        supports_ramp = is_loaded and manager.supports_ramp_sampling
        self._ramp_allowed = supports_ramp and manager.can_ramp_while_acquiring
        self.sample_ramp_cb.setVisible(supports_ramp)
        self.ramp_dt_widget.setVisible(supports_ramp)
        self.sample_ramp_cb.setEnabled(self._ramp_allowed)
        if supports_ramp and not self._ramp_allowed:
            self.sample_ramp_cb.setChecked(False)
            self.sample_ramp_cb.setToolTip(
                "This device cannot generate ramp triggers while its ADC is "
                "acquiring: use a separate timing controller for ramp sampling."
            )
        else:
            self.sample_ramp_cb.setToolTip("")

        # Transient test: only on timing devices that implement it, and
        # only when ramp generation is possible at all.
        transient_supported = self._ramp_allowed and manager.transient_test_supported
        self.transient_test_cb.setVisible(supports_ramp)
        self.transient_test_cb.setEnabled(transient_supported)
        if not transient_supported:
            self.transient_test_cb.setChecked(False)
        self.transient_reset_cb.setVisible(supports_ramp)
        self.transient_reset_cb.setEnabled(transient_supported)
        if not transient_supported:
            self.transient_reset_cb.setChecked(False)

        # ADC Input Range
        adc_settings = manager.get_adc_settings() if is_loaded else {}
        ranges = adc_settings.get("input_ranges_volts", [])
        self.adc_range_combo.blockSignals(True)
        self.adc_range_combo.clear()
        if len(ranges) > 1:
            self.adc_range_combo.setVisible(True)
            self.adc_range_label.setVisible(False)
            for r in ranges:
                self.adc_range_combo.addItem(f"{r['min_volts']:g} V to {r['max_volts']:g} V", r)
            eff = adc_settings.get("input_range_volts")
            if eff:
                target_str = f"{eff['min_volts']:g} V to {eff['max_volts']:g} V"
                idx = self.adc_range_combo.findText(target_str)
                if idx >= 0:
                    self.adc_range_combo.setCurrentIndex(idx)
        elif len(ranges) == 1:
            self.adc_range_combo.setVisible(False)
            self.adc_range_label.setVisible(True)
            r = ranges[0]
            self.adc_range_label.setText(f"{r['min_volts']:g} V to {r['max_volts']:g} V (Fixed)")
        else:
            self.adc_range_combo.setVisible(False)
            self.adc_range_label.setVisible(True)
            self.adc_range_label.setText("Default")
        self.adc_range_combo.blockSignals(False)

        self.on_manual_edit()

    def _show_fixed_value(self, line_edit: QLineEdit, combo: QComboBox, value_us: float):
        """Display a firmware-fixed timing parameter (μs), read-only."""
        combo.blockSignals(True)
        combo.setCurrentIndex(2)  # μs, the unit of the declared value
        combo.blockSignals(False)
        line_edit.setText(f"{value_us:g}")
        line_edit.setReadOnly(True)
        line_edit.setStyleSheet("")

    def _format_time_us(self, val_us: float) -> str:
        if val_us >= 1_000_000:
            return f"{val_us / 1e6:.4f} s"
        elif val_us >= 1000:
            return f"{val_us / 1e3:.4f} ms"
        else:
            return f"{val_us:.2f} μs"

    def update_actual_timing(self, actual):
        """Update the Live Hardware Timing Summary card with quantized actual values."""
        self.final_int_time_label.setText(self._format_time_us(actual.int_time_us))
        self.final_rst_time_label.setText(self._format_time_us(actual.reset_time_us))
        self.final_period_label.setText(self._format_time_us(actual.period_us))
        # The APPLIED ramp dt (the timing driver's quantized value — e.g.
        # the transient test mode's pinned 1.0667 us for the nominal "1 us"),
        # so the nominal-vs-real difference is always visible.
        dt_us = getattr(actual, "ramp_dt_eff_us", None)
        # 6 significant digits: the applied dt differs from the request in
        # the 4th digit upward (divider round-up: 1 -> 1.00026 us) and the
        # tile must show it, not round it back to the nominal value.
        self.final_dt_label.setText(f"{dt_us:.6g} μs" if dt_us else "-")
        freq = actual.sample_frequency_hz
        self.final_freq_label.setText(f"{freq:.2f} Hz" if freq > 0 else "-")

    def update_label(self):
        """Legacy updater reading from session."""
        int_us = self.session.int_time * 1e6
        rst_us = self.session.reset_time * 1e6
        prd_s = self.session.period_s
        if prd_s <= 0:
            prd_s = 1.0 / self.session.sample_freq if self.session.sample_freq > 0 else (
                self.session.int_time + self.session.reset_time
            )
        prd_us = prd_s * 1e6
        self.final_int_time_label.setText(self._format_time_us(int_us))
        self.final_rst_time_label.setText(self._format_time_us(rst_us))
        self.final_period_label.setText(self._format_time_us(prd_us))
        dt_us = self.session.ramp_dt_s * 1e6 if getattr(self.session, "ramp_dt_s", 0) else None
        # 6 significant digits: the applied dt differs from the request in
        # the 4th digit upward (divider round-up: 1 -> 1.00026 us) and the
        # tile must show it, not round it back to the nominal value.
        self.final_dt_label.setText(f"{dt_us:.6g} μs" if dt_us else "-")
        self.final_freq_label.setText(
            f"{self.session.sample_freq:.2f} Hz" if self.session.sample_freq > 0 else "-"
        )

    def on_send_press(self):
        if not self._selections_complete():
            return  # backstop: Apply requires a gain for every active channel
        try:
            final_int, final_rst, final_period, final_dt = self._collect_timing_values()
        except ValueError:
            return  # unparsable time field: the highlighted inputs block Apply
        if self.sample_ramp_cb.isChecked() and self._ramp_allowed and final_dt is None:
            return  # ramp requested but the dt field is unparsable
        self.status_frame.setVisible(True)

        period_us = final_period * 1e6 if final_period is not None else None

        # The TYPED values, not the session's preview-quantized ones: the
        # preview and Apply must feed the driver's exact mirror identical
        # inputs — quantizing already-quantized values loses up to one
        # integration interval on the PIO generator, and the hardware
        # would then run a different timing than the wizard reported.
        config = {
            "int_time_us": final_int * 1e6,
            "reset_time_us": final_rst * 1e6,
            "period_us": period_us,
            "sample_ramp": self.sample_ramp_cb.isChecked() and self._ramp_allowed,
            "ramp_dt_us": final_dt if final_dt is not None else 10.0,
            "transient_test": self._transient_mode() == "integration",
            "transient_reset": self._transient_mode() == "reset",
            "gain_ch1_label": self.capacitance_combo_ch1.currentText(),
            "gain_ch2_label": self.capacitance_combo_ch2.currentText() if self.capacitance_combo_ch2.isVisible() else None,
        }

        if self.adc_range_combo.isVisible():
            range_data = self.adc_range_combo.currentData()
            if range_data:
                config["input_range_min"] = range_data["min_volts"]
                config["input_range_max"] = range_data["max_volts"]

        self.apply_configuration_requested.emit(config)

    def on_configuration_result(self, success: bool, message: str = None):
        if success:
            self.status_frame.setStyleSheet(
                "background-color: #d4edda; border: 1px solid #c3e6cb; border-radius: 5px;"
            )
            self.status_label.setText("Configuration Applied Successfully")
            self.status_label.setStyleSheet(
                "color: #155724; font-weight: bold; font-size: 11pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_success.pixmap(22, 22))
            self.already_sent = True
            self.sent_successfull.emit()
            self.toggle_ui_lock(locked=True)
            logger.debug("Parameters sent to the DAQ device.")
        else:
            # visible immediately: the timing preview reports rejections
            # here while the user is still editing (the frame used to stay
            # hidden until the first Apply press, hiding the reason)
            self.status_frame.setVisible(True)
            self.status_frame.setStyleSheet(
                "background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 5px;"
            )
            manager = self.hardware_manager
            if message:
                # the specific reason (e.g. the timing preview's rejection:
                # markers mode with T_int beyond the firmware's dt cap)
                self.status_label.setText(f"Error: {message}")
            elif manager is not None and manager.gain_state_unknown:
                self.status_label.setText(
                    "Error: gain apply failed — capacitor state unknown. "
                    "Fix the connection and re-apply the configuration."
                )
            else:
                self.status_label.setText("Error: Failed to apply configuration. Check connection/gain conflict.")
            self.status_label.setStyleSheet(
                "color: #721c24; font-weight: bold; font-size: 11pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_error.pixmap(22, 22))
            self.already_sent = False

    def populate_meta_fields(self):
        """Populate the UI fields from the session model."""
        self.beam_energy_input.setText(str(self.session.beam_energy_mev))
        self.beam_current_input.setText(str(self.session.beam_current_na))
        self.det_voltage_input.setText(str(self.session.detector_voltage_v))
        self.depth_input.setText(str(self.session.depth_cm))
        self.meas_note_input.setPlainText(self.session.measurement_note)

    def toggle_ui_lock(self, locked: bool):
        self.send_bnt.setVisible(not locked)
        self.edit_bnt.setVisible(locked)
        # Live ramp analysis only makes sense with the applied timing in
        # ramp mode (the full window waveform); the session's timing_mode is
        # refreshed from the driver mirror before the apply result arrives.
        self.live_ramp_bnt.setEnabled(
            locked and self.session.timing_mode == "ramp"
        )
        self.int_time.setEnabled(not locked)
        self.rst_time.setEnabled(not locked)
        self.period_time.setEnabled(not locked)
        self.magnitude_int.setEnabled(not locked)
        self.magnitude_rst.setEnabled(not locked)
        self.magnitude_period.setEnabled(not locked)
        self.sample_ramp_cb.setEnabled(not locked and self._ramp_allowed)
        self.ramp_dt_input.setEnabled(
            not locked and self._transient_mode() is None
        )
        transient_ok = (
            not locked and self._ramp_allowed
            and bool(self.hardware_manager and self.hardware_manager.transient_test_supported)
        )
        self.transient_test_cb.setEnabled(transient_ok)
        self.transient_reset_cb.setEnabled(transient_ok)
        self.capacitance_combo_ch1.setEnabled(not locked)
        # CH2 enablement on unlock comes from the wiring-aware option filter,
        # not from "any pin is shared": mixed topologies keep CH2 selectable.
        if locked:
            self.capacitance_combo_ch2.setEnabled(False)
        else:
            self._adjusting_linked_gain = True
            self._update_ch2_gain_options()
            self._adjusting_linked_gain = False
            self._update_apply_enabled()
        self.adc_range_combo.setEnabled(not locked)
        self.beam_energy_input.setEnabled(not locked)
        self.beam_current_input.setEnabled(not locked)
        self.det_voltage_input.setEnabled(not locked)
        self.depth_input.setEnabled(not locked)
        self.meas_note_input.setEnabled(not locked)

    def on_edit_press(self):
        self.already_sent = False
        self.toggle_ui_lock(locked=False)
        self.status_frame.setVisible(False)
        self.editing_time.emit()

    def restart_event(self):
        self.already_sent = False
        self.toggle_ui_lock(locked=False)
        self.status_frame.setVisible(False)
