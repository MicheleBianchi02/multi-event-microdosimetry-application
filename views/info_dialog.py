import json

from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class InfoDialog(QDialog):
    def __init__(self, session_model, hardware_manager=None, parent=None):
        super().__init__(parent)
        self.session = session_model
        self.hardware_manager = hardware_manager
        self.setWindowTitle("Measurement Info")
        self.resize(600, 800)
        self.init_ui()
        self.populate_fields()

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)

        # -----------------------------
        # 1) Workspace Settings
        # -----------------------------
        ws_group = QGroupBox("Workspace Settings")
        ws_form = QFormLayout(ws_group)

        self.app_version_input = QLineEdit()
        self.app_version_input.setReadOnly(True)
        self.facility_input = QLineEdit()
        self.particle_input = QLineEdit()
        self.beam_type_input = QLineEdit()

        # Hardware identity is a snapshot of what the loaded hardware config
        # produced this measurement: displayed, not edited here.
        self.hardware_summary_label = QLabel()
        self.hardware_summary_label.setStyleSheet("color: #555;")
        self.hardware_summary_label.setWordWrap(True)

        self.shift_note_input = QTextEdit()
        self.shift_note_input.setMaximumHeight(60)

        # Connect signals
        self.facility_input.textEdited.connect(self.on_ws_edit)
        self.particle_input.textEdited.connect(self.on_ws_edit)
        self.beam_type_input.textEdited.connect(self.on_ws_edit)
        self.shift_note_input.textChanged.connect(self.on_ws_edit)

        ws_form.addRow("App Ver:", self.app_version_input)
        ws_form.addRow("Facility:", self.facility_input)
        ws_form.addRow("Particle:", self.particle_input)
        ws_form.addRow("Beam Type:", self.beam_type_input)
        ws_form.addRow("Hardware:", self.hardware_summary_label)
        ws_form.addRow("Shift Note:", self.shift_note_input)

        layout.addWidget(ws_group)

        # -----------------------------
        # 2) Measurement Parameters
        # -----------------------------
        meta_group = QGroupBox("Measurement Parameters")
        meta_form = QFormLayout(meta_group)

        self.beam_energy_input = QLineEdit()
        self.beam_current_input = QLineEdit()
        self.det_voltage_input = QLineEdit()
        self.depth_input = QLineEdit()

        # Applied gain: read-only snapshot (chosen in the wizard, applied to
        # the hardware before the measurement started)
        self.ch1_gain_label = QLabel()
        self.ch1_gain_label.setStyleSheet("color: #555;")
        self.ch2_gain_label = QLabel()
        self.ch2_gain_label.setStyleSheet("color: #555;")

        self.meas_note_input = QTextEdit()
        self.meas_note_input.setMaximumHeight(60)

        from PyQt6.QtWidgets import QCheckBox

        self.is_problematic_input = QCheckBox()
        self.is_problematic_input.stateChanged.connect(self.on_meta_edit)

        for w in [
            self.beam_energy_input,
            self.beam_current_input,
            self.det_voltage_input,
            self.depth_input,
        ]:
            w.textEdited.connect(self.on_meta_edit)
        self.meas_note_input.textChanged.connect(self.on_meta_edit)

        meta_form.addRow("Beam Energy (MeV):", self.beam_energy_input)
        meta_form.addRow("Beam Current (nA):", self.beam_current_input)
        meta_form.addRow("Detector Voltage (V):", self.det_voltage_input)
        meta_form.addRow("Depth (cm):", self.depth_input)
        meta_form.addRow("CH1 Gain:", self.ch1_gain_label)
        meta_form.addRow("CH2 Gain:", self.ch2_gain_label)
        meta_form.addRow("Problematic Measurement:", self.is_problematic_input)
        meta_form.addRow("Measurement Note:", self.meas_note_input)

        layout.addWidget(meta_group)
        
        # -----------------------------
        # 3) Read-Only Information
        # -----------------------------
        info_group = QGroupBox("Read-Only Information")
        info_form = QFormLayout(info_group)
        
        self.filename_label = QLabel()
        self.filename_label.setStyleSheet("color: #555;")
        self.single_channel_label = QLabel()
        self.single_channel_label.setStyleSheet("color: #555;")
        self.analysis_timestamp_label = QLabel()
        self.analysis_timestamp_label.setStyleSheet("color: #555;")
        self.timestamp_start_label = QLabel()
        self.timestamp_start_label.setStyleSheet("color: #555;")
        self.timestamp_end_label = QLabel()
        self.timestamp_end_label.setStyleSheet("color: #555;")
        
        info_form.addRow("Filename:", self.filename_label)
        info_form.addRow("Single Channel Mode:", self.single_channel_label)
        info_form.addRow("Analysis Timestamp:", self.analysis_timestamp_label)
        info_form.addRow("Timestamp Start:", self.timestamp_start_label)
        info_form.addRow("Timestamp End:", self.timestamp_end_label)
        
        layout.addWidget(info_group)
        layout.addStretch()

        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)

        # Add Close button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        main_layout.addLayout(btn_layout)

    def _gain_text(self, gpio_mask, capacitance):
        if gpio_mask is None:
            return "N/A"
        bits = "".join("1" if b else "0" for b in gpio_mask)
        c = f" ({capacitance:g} pF)" if capacitance else ""
        return f"mask {bits}{c}"

    def populate_fields(self):
        self.blockSignals(True)
        # Workspace
        self.app_version_input.setText(self.session.app_version)
        self.facility_input.setText(self.session.facility)
        self.particle_input.setText(getattr(self.session, "particle", ""))
        self.beam_type_input.setText(self.session.beam_type)

        manager = getattr(self, "hardware_manager", None)
        if manager is not None and manager.is_loaded:
            summary = manager.devices_summary()
            devices = ", ".join(
                f"{dev['name']} [{dev['driver']}]"
                for dev in summary.get("devices", {}).values()
            )
            self.hardware_summary_label.setText(devices or "N/A")
        else:
            self.hardware_summary_label.setText("no hardware config loaded")

        self.shift_note_input.setPlainText(self.session.shift_note)
        self.blockSignals(False)

        # Measurement
        self.beam_energy_input.setText(str(self.session.beam_energy_mev))
        self.beam_current_input.setText(str(self.session.beam_current_na))
        self.det_voltage_input.setText(str(self.session.detector_voltage_v))
        self.depth_input.setText(str(self.session.depth_cm))
        self.ch1_gain_label.setText(
            self._gain_text(self.session.gpio_mask_ch1, getattr(self.session, "capacitance_pf_ch1", None))
        )
        self.ch2_gain_label.setText(
            self._gain_text(self.session.gpio_mask_ch2, getattr(self.session, "capacitance_pf_ch2", None))
        )

        self.is_problematic_input.blockSignals(True)
        self.is_problematic_input.setChecked(
            getattr(self.session, "is_problematic", False)
        )
        self.is_problematic_input.blockSignals(False)

        self.meas_note_input.setPlainText(self.session.measurement_note)
        
        # Read-Only
        file_path = getattr(self.session, "file_path", None)
        if file_path:
            import os
            self.filename_label.setText(os.path.basename(file_path))
        else:
            self.filename_label.setText("N/A")
            
        sc_mode = not getattr(self.session, "multiple_channel", True)
        self.single_channel_label.setText("True" if sc_mode else "False")
        
        ana_ts = getattr(self.session, "analysis_timestamp", "")
        self.analysis_timestamp_label.setText(str(ana_ts) if ana_ts else "N/A")
        
        start_ts = getattr(self.session, "timestamp_start", "")
        self.timestamp_start_label.setText(str(start_ts) if start_ts else "N/A")
        
        end_ts = getattr(self.session, "timestamp_end", "")
        self.timestamp_end_label.setText(str(end_ts) if end_ts else "N/A")

    def on_ws_edit(self):
        self.session.facility = self.facility_input.text()
        self.session.particle = self.particle_input.text()
        self.session.beam_type = self.beam_type_input.text()
        self.session.shift_note = self.shift_note_input.toPlainText()

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
            self.session.detector_voltage_v = float(
                self.det_voltage_input.text() or 0.0
            )
        except ValueError:
            pass

        try:
            self.session.depth_cm = float(self.depth_input.text() or 0.0)
        except ValueError:
            pass

        self.session.is_problematic = self.is_problematic_input.isChecked()
        self.session.measurement_note = self.meas_note_input.toPlainText()
