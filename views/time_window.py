import logging

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
    QGroupBox,
    QTextEdit,
)

from views.analysis_widget import AnalysisWidget

logger = logging.getLogger(__name__)

# Set global PyQtGraph theme to white background, black foreground
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


class TimeWindow(QWidget):
    sent_successfull = pyqtSignal()
    editing_time = pyqtSignal()
    apply_configuration_requested = pyqtSignal(dict)
    timing_calculation_requested = pyqtSignal(float, float)

    def __init__(self, session_model, simulate: bool, hardware_manager=None) -> None:
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        self.already_sent = False

        # Core layout uses a stacked widget now
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
            QComboBox {
                background-color: palette(Base);
                color: palette(WindowText);
            }
            /* Abstract control the popup window */
            QComboBox QAbstractItemView {
                background-color: palette(Base);
                color: palette(WindowText);
                border: 1px solid palette(Mid);
                selection-background-color: palette(Highlight);
                selection-color: palette(HighlightedText);
                outline: 0;
            }
        """)
        self.icon_error = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxCritical
        )
        self.icon_success = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogApplyButton
        )

        self.summary_frame = QFrame()
        self.summary_frame.setStyleSheet("""
            QFrame {
                background-color: #dfe3e8;   /* soft dark gray surface */
                border-radius: 8px;
            }
        """)
        self.final_int_time_label = QLabel("-")
        self.final_rst_time_label = QLabel("-")
        self.final_freq_label = QLabel("-")

        for lbl in [
            self.final_int_time_label,
            self.final_rst_time_label,
            self.final_freq_label,
        ]:
            lbl.setStyleSheet("font-size: 16pt; font-weight: bold; color: #2f3b45;")

        # --- manual inputs ---
        self.int_time = QLineEdit("1")
        self.int_time.setMaximumWidth(300)
        self.rst_time = QLineEdit("1")
        self.rst_time.setMaximumWidth(300)
        self.magnitude_int = QComboBox()
        self.magnitude_int.setFixedWidth(60)
        self.magnitude_rst = QComboBox()
        self.magnitude_rst.setFixedWidth(60)
        self.mode_menu = QComboBox()
        self.mode_menu.setMaximumWidth(150)

        for widget in [
            self.int_time,
            self.rst_time,
            self.magnitude_int,
            self.magnitude_rst,
            self.mode_menu,
        ]:
            widget.setStyleSheet("padding: 5px; font-size: 12pt;")

        self.magnitude_int.addItems(["s", "ms", "μs"])
        self.magnitude_int.setCurrentIndex(1)  # ms
        self.magnitude_rst.addItems(["s", "ms", "μs"])
        self.magnitude_rst.setCurrentIndex(2)  # us

        self.mode_menu.addItems(["Debug", "Operational"])
        self.mode_menu.setCurrentIndex(1)

        # Signals
        self.magnitude_int.currentIndexChanged.connect(self.on_manual_edit)
        self.int_time.textEdited.connect(self.on_manual_edit)
        self.magnitude_rst.currentIndexChanged.connect(self.on_manual_edit)
        self.rst_time.textEdited.connect(self.on_manual_edit)
        self.mode_menu.currentIndexChanged.connect(self.on_mode_change)

        # Measurement Parameters Inputs
        self.capacitance_combo_ch1 = QComboBox()
        self.capacitance_combo_ch2 = QComboBox()
        self.capacitance_combo_ch1.currentIndexChanged.connect(self.on_cap_change)
        self.capacitance_combo_ch2.currentIndexChanged.connect(self.on_cap_change)
        self._adjusting_linked_gain = False

        self.beam_energy_input = QLineEdit(str(self.session.beam_energy_mev))
        self.beam_current_input = QLineEdit(str(self.session.beam_current_na))
        self.det_voltage_input = QLineEdit(str(self.session.detector_voltage_v))
        self.depth_input = QLineEdit(str(self.session.depth_cm))
        self.meas_note_input = QTextEdit(self.session.measurement_note)
        self.meas_note_input.setMaximumHeight(60)

        for w in [self.beam_energy_input, self.beam_current_input, self.det_voltage_input, self.depth_input]:
            w.setStyleSheet("padding: 5px; font-size: 12pt;")
            w.textEdited.connect(self.on_meta_edit)
        self.meas_note_input.textChanged.connect(self.on_meta_edit)

        # --- action buttons ---
        self.send_bnt = QPushButton(" Apply Configuration")
        self.send_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_bnt.setStyleSheet("""
            QPushButton {
                background-color: #28a745; color: white; font-weight: bold; 
                padding: 12px 20px; font-size: 14pt; border-radius: 5px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #ccc; }
        """)
        self.send_bnt.clicked.connect(self.on_send_press)

        self.edit_bnt = QPushButton("Edit")
        self.edit_bnt.setVisible(False)
        self.edit_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_bnt.setStyleSheet("""
            QPushButton {
                background-color: #ffc107; color: #333; font-weight: bold; 
                padding: 12px 20px; font-size: 14pt; border-radius: 5px;
            }
            QPushButton:hover { background-color: #e0a800; }
        """)
        self.edit_bnt.clicked.connect(self.on_edit_press)

        # --- status bar ---
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
        # Apply this stylesheet to your QApplication or individual QCheckBox widgets
        CHECKBOX_STYLE = """
            QCheckBox {
                spacing: 8px;
                outline: none;
                font-size: 14pt;
                font-weight: bold;
                font-family: "Segoe UI";
            }

            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid palette(dark);
                background-color: palette(base);
            }

            QCheckBox::indicator:hover {
                border: 1px solid palette(highlight);
                background-color: palette(alternate-base);
            }

            QCheckBox::indicator:checked {
                background-color: palette(highlight);
                border: 1px solid palette(highlight);
            }

            QCheckBox::indicator:disabled {
                background-color: palette(window);
                border: 1px solid palette(midlight);
            }

            QCheckBox::indicator:checked:disabled {
                background-color: palette(highlight);
                border: 1px solid palette(midlight);
            }

            QCheckBox::indicator:indeterminate {
                background-color: palette(light);
                border: 1px solid palette(dark);
            }
        """

        outer_master = QHBoxLayout(self.manual_config_widget)
        outer_master.addStretch(1)
        master_container = QWidget()
        master_container.setFixedWidth(700)
        master = QVBoxLayout(master_container)
        outer_master.addWidget(master_container)
        outer_master.addStretch(1)

        self.btn_back = QPushButton("<- Back to Analysis")
        self.btn_back.setStyleSheet(
            "background-color: #6c757d; color: white; font-weight: bold; padding: 8px 15px; font-size: 12pt; border-radius: 5px;"
        )
        self.btn_back.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        master.addWidget(self.btn_back, alignment=Qt.AlignmentFlag.AlignLeft)

        self.num_channel_box = QCheckBox("Single channel mode")
        self.num_channel_box.setStyleSheet(CHECKBOX_STYLE)
        self.num_channel_box.stateChanged.connect(self.on_num_ch_checked)
        master.addWidget(self.num_channel_box)
        master.addSpacing(5)

        self.sample_ramp_cb = QCheckBox("Sample Ramp")
        self.sample_ramp_cb.setStyleSheet(CHECKBOX_STYLE)
        
        # Ramp dt layout
        self.ramp_dt_widget = QWidget()
        ramp_dt_layout = QHBoxLayout(self.ramp_dt_widget)
        ramp_dt_layout.setContentsMargins(0, 0, 0, 0)
        ramp_dt_layout.addWidget(QLabel("Ramp dt (μs):"))
        self.ramp_dt_input = QLineEdit("10")
        self.ramp_dt_input.setFixedWidth(80)
        ramp_dt_layout.addWidget(self.ramp_dt_input)
        ramp_dt_layout.addStretch(1)
        
        self.sample_ramp_cb.stateChanged.connect(self.on_manual_edit)
        self.ramp_dt_input.textEdited.connect(self.on_manual_edit)

        # Visibility depends on the loaded hardware config (reload_hardware)
        master.addWidget(self.sample_ramp_cb)
        master.addWidget(self.ramp_dt_widget)
            
        master.addSpacing(15)

        # Summary Dashboard
        sum_layout = QHBoxLayout(self.summary_frame)

        def create_stat(title, widget):
            v = QVBoxLayout()
            t = QLabel(title)
            t.setStyleSheet(
                "color: #7f8c8d; font-size: 10pt; text-transform: uppercase; letter-spacing: 1px;"
            )
            v.addWidget(t)
            v.addWidget(widget)
            return v

        sum_layout.addLayout(create_stat("Integration", self.final_int_time_label))
        sum_layout.addLayout(create_stat("Reset Time", self.final_rst_time_label))
        sum_layout.addLayout(create_stat("Sample Frequency", self.final_freq_label))

        master.addWidget(self.summary_frame)
        master.addSpacing(15)

        # Main Content (Scrollable if screen is small)
        scroll = QScrollArea()
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # Disable horizontal scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)

        # Measurement Parameters
        meta_group = QGroupBox("Measurement Parameters")
        meta_group.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12pt; border: 1px solid #ccc; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }")
        meta_form = QFormLayout(meta_group)
        meta_form.addRow("Beam Energy (MeV):", self.beam_energy_input)
        meta_form.addRow("Beam Current (nA):", self.beam_current_input)
        meta_form.addRow("Detector Voltage (V):", self.det_voltage_input)
        meta_form.addRow("Depth (cm):", self.depth_input)
        meta_form.addRow("Measurement Note:", self.meas_note_input)
        content_layout.addWidget(meta_group)
        
        content_layout.addSpacing(15)

        # -- Configuration --
        time_group = QGroupBox("Timing Configuration")
        time_group.setStyleSheet("QGroupBox { font-weight: bold; font-size: 12pt; border: 1px solid #ccc; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }")
        form_layout = QFormLayout(time_group)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        # Int Row
        r1 = QHBoxLayout()
        r1.addWidget(self.int_time)
        r1.addWidget(self.magnitude_int)
        form_layout.addRow("Integration Time:", r1)

        # Reset Row
        r2 = QHBoxLayout()
        r2.addWidget(self.rst_time)
        r2.addWidget(self.magnitude_rst)
        form_layout.addRow("Reset Time:", r2)

        # Mode Row
        form_layout.addRow("Operational Mode:", self.mode_menu)
        
        # Gain Rows
        form_layout.addRow("CH 1 Gain:", self.capacitance_combo_ch1)
        form_layout.addRow("CH 2 Gain:", self.capacitance_combo_ch2)

        content_layout.addWidget(time_group)

        content_layout.addStretch(1)
        scroll.setWidget(content_widget)
        master.addWidget(scroll)

        # Footer (Action Buttons)
        footer = QHBoxLayout()
        footer.addWidget(self.status_frame, 1)
        footer.addWidget(self.edit_bnt)
        footer.addWidget(self.send_bnt)
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

    def on_manual_edit(self):
        # This handles input from Text boxes
        self.already_sent = False
        self.editing_time.emit()
        self.send_bnt.setEnabled(False)

        try:
            # integration
            t_int = ""
            for c in self.int_time.text():
                if c.isdecimal() or (c == "." and "." not in t_int):
                    t_int += c

            self.int_time.setText(t_int)
            t_int = float(t_int)
            mag_i = self.magnitude_int.currentText()
            mult_i = 1 if mag_i == "s" else (1e-3 if mag_i == "ms" else 1e-6)
            final_int = t_int * mult_i

            # RESET ----------------

            t_rst = ""
            for c in self.rst_time.text():
                if c.isdecimal() or (c == "." and "." not in t_rst):
                    t_rst += c

            self.rst_time.setText(t_rst)
            t_rst = float(t_rst)
            mag_r = self.magnitude_rst.currentText()
            mult_r = 1 if mag_r == "s" else (1e-3 if mag_r == "ms" else 1e-6)
            final_rst = t_rst * mult_r

            self.timing_calculation_requested.emit(final_int, final_rst)
            self.send_bnt.setEnabled(True)
        except ValueError:
            pass  # Handle empty strings or parse errors gracefully

    def on_mode_change(self):
        self.session.debug_mode = self.mode_menu.currentText() == "Debug"
        self.on_manual_edit()  # Re-trigger

    def on_num_ch_checked(self):
        if not self.num_channel_box.isChecked():
            self.session.multiple_channel = True
        else:
            self.session.multiple_channel = False

        self.on_manual_edit()



    def on_cap_change(self):
        self.already_sent = False
        self.send_bnt.setEnabled(True)
        # With ganged pins the channels' gains are physically coupled: keep
        # the ch2 selection compatible with the ch1 one.
        manager = self.hardware_manager
        if (
            not self._adjusting_linked_gain
            and manager is not None
            and manager.is_loaded
            and manager.channels_share_gain
        ):
            current = self.capacitance_combo_ch2.currentText()
            if manager.gains_conflict(
                self.capacitance_combo_ch1.currentText(), current
            ):
                self._adjusting_linked_gain = True
                for i in range(self.capacitance_combo_ch2.count()):
                    label = self.capacitance_combo_ch2.itemText(i)
                    if not manager.gains_conflict(
                        self.capacitance_combo_ch1.currentText(), label
                    ):
                        self.capacitance_combo_ch2.setCurrentIndex(i)
                        break
                self._adjusting_linked_gain = False

    def reload_hardware(self):
        """Rebuild the hardware-dependent parts after a config load/unload."""
        manager = self.hardware_manager
        ch2_active = manager is not None and manager.is_loaded and "ch2" in manager.active_channels
        dual_possible = manager is not None and manager.max_channels >= 2

        self.capacitance_combo_ch1.blockSignals(True)
        self.capacitance_combo_ch1.clear()
        if manager is not None and manager.is_loaded:
            self.capacitance_combo_ch1.addItems(manager.get_gain_labels(1))
        self.capacitance_combo_ch1.blockSignals(False)

        self.capacitance_combo_ch2.blockSignals(True)
        self.capacitance_combo_ch2.clear()
        if ch2_active:
            self.capacitance_combo_ch2.addItems(manager.get_gain_labels(2))
        self.capacitance_combo_ch2.blockSignals(False)
        self.capacitance_combo_ch2.setVisible(ch2_active)

        shared = manager is not None and manager.is_loaded and manager.channels_share_gain
        self.capacitance_combo_ch2.setEnabled(ch2_active and not shared)
        if shared:
            self.capacitance_combo_ch2.setToolTip(
                "Gain pins are shared with CH1: the selection follows CH1."
            )
        else:
            self.capacitance_combo_ch2.setToolTip("")

        supports_ramp = manager is not None and manager.supports_ramp_sampling
        self.sample_ramp_cb.setVisible(supports_ramp)
        self.ramp_dt_widget.setVisible(supports_ramp)

        # Channel gating (B8): the acquisition system bounds the choice
        if not dual_possible:
            self.num_channel_box.blockSignals(True)
            self.num_channel_box.setChecked(True)
            self.num_channel_box.setEnabled(False)
            self.num_channel_box.blockSignals(False)
            self.session.multiple_channel = False
        else:
            self.num_channel_box.setEnabled(True)

        self.on_manual_edit()

    def update_label(self):
        i_ms = self.session.int_time * 1000
        r_ms = self.session.reset_time * 1000
        self.final_int_time_label.setText(f"{i_ms:.4f} ms")
        self.final_rst_time_label.setText(f"{r_ms:.4f} ms")
        self.final_freq_label.setText(f"{self.session.sample_freq:.4f} Hz")

    def on_send_press(self):
        self.status_frame.setVisible(True)
        ramp_dt = 10.0
        try:
            ramp_dt = float(self.ramp_dt_input.text())
        except ValueError:
            pass
            
        config = {
            "int_time_us": self.session.int_time * 1e6,
            "reset_time_us": self.session.reset_time * 1e6,
            "debug_mode": self.session.debug_mode,
            "multiple_channel": self.session.multiple_channel,
            "sample_ramp": self.sample_ramp_cb.isChecked(),
            "ramp_dt_us": ramp_dt,
            "gain_ch1_label": self.capacitance_combo_ch1.currentText(),
            "gain_ch2_label": self.capacitance_combo_ch2.currentText()
        }
        self.apply_configuration_requested.emit(config)

    def on_configuration_result(self, success: bool):
        if success:
            self.status_frame.setStyleSheet(
                "background-color: #d4edda; border: 1px solid #c3e6cb; border-radius: 5px;"
            )
            self.status_label.setText("Configuration Applied Successfully")
            self.status_label.setStyleSheet(
                "color: #155724; font-weight: bold; font-size: 12pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_success.pixmap(24, 24))
            self.already_sent = True
            self.sent_successfull.emit()

            self.toggle_ui_lock(locked=True)

            logger.debug("Parameters sent to the DAQ device.")
        else:
            self.status_frame.setStyleSheet(
                "background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 5px;"
            )
            self.status_label.setText("Error: Failed to send data. Check connection.")
            self.status_label.setStyleSheet(
                "color: #721c24; font-weight: bold; font-size: 12pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_error.pixmap(24, 24))
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
        self.int_time.setEnabled(not locked)
        self.rst_time.setEnabled(not locked)
        self.magnitude_int.setEnabled(not locked)
        self.magnitude_rst.setEnabled(not locked)
        self.mode_menu.setEnabled(not locked)
        self.num_channel_box.setEnabled(not locked)
        self.sample_ramp_cb.setEnabled(not locked)
        self.ramp_dt_input.setEnabled(not locked)
        self.capacitance_combo_ch1.setEnabled(not locked)
        self.capacitance_combo_ch2.setEnabled(not locked)

    def on_edit_press(self):
        self.already_sent = False
        self.toggle_ui_lock(locked=False)
        self.status_frame.setVisible(False)
        self.editing_time.emit()

    def restart_event(self):
        self.already_sent = False
        self.toggle_ui_lock(locked=False)
        self.status_frame.setVisible(False)
