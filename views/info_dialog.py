import json

from PyQt6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class InfoDialog(QDialog):
    def __init__(self, session_model, parent=None):
        super().__init__(parent)
        self.session = session_model
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
        self.fw_version_input = QLineEdit()
        self.hardware_id_ch1_input = QLineEdit()
        self.hardware_id_ch2_input = QLineEdit()
        self.facility_input = QLineEdit()
        self.beam_type_input = QLineEdit()

        self.adc_bits_input = QSpinBox()
        self.adc_bits_input.setRange(8, 32)

        self.adc_vref_input = QDoubleSpinBox()
        self.adc_vref_input.setRange(1.0, 10.0)
        self.adc_vref_input.setDecimals(2)
        self.adc_vref_input.setSingleStep(0.1)

        self.ch1_caps_input = QLineEdit()
        self.ch2_caps_input = QLineEdit()

        self.shift_note_input = QTextEdit()
        self.shift_note_input.setMaximumHeight(60)

        # Connect signals
        self.app_version_input.textEdited.connect(self.on_ws_edit)
        self.fw_version_input.textEdited.connect(self.on_ws_edit)
        self.hardware_id_ch1_input.textEdited.connect(self.on_ws_edit)
        self.hardware_id_ch2_input.textEdited.connect(self.on_ws_edit)
        self.facility_input.textEdited.connect(self.on_ws_edit)
        self.beam_type_input.textEdited.connect(self.on_ws_edit)
        self.adc_bits_input.valueChanged.connect(self.on_ws_edit)
        self.adc_vref_input.valueChanged.connect(self.on_ws_edit)
        self.ch1_caps_input.textEdited.connect(self.on_ws_edit)
        self.ch2_caps_input.textEdited.connect(self.on_ws_edit)
        self.shift_note_input.textChanged.connect(self.on_ws_edit)

        ws_form.addRow("App Ver:", self.app_version_input)
        ws_form.addRow("Firmware Ver:", self.fw_version_input)
        ws_form.addRow("HW ID CH1:", self.hardware_id_ch1_input)
        ws_form.addRow("HW ID CH2:", self.hardware_id_ch2_input)
        ws_form.addRow("Facility:", self.facility_input)
        ws_form.addRow("Beam Type:", self.beam_type_input)
        ws_form.addRow("ADC Num Bits:", self.adc_bits_input)
        ws_form.addRow("ADC VRef (V):", self.adc_vref_input)
        ws_form.addRow("CH1 Caps (pF):", self.ch1_caps_input)
        ws_form.addRow("CH2 Caps (pF):", self.ch2_caps_input)
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

        from PyQt6.QtCore import QRegularExpression
        from PyQt6.QtGui import QRegularExpressionValidator

        binary_validator = QRegularExpressionValidator(QRegularExpression("[01]*"))
        self.ch1_cap_code_input = QLineEdit()
        self.ch1_cap_code_input.setValidator(binary_validator)
        self.ch2_cap_code_input = QLineEdit()
        self.ch2_cap_code_input.setValidator(binary_validator)

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
            self.ch1_cap_code_input,
            self.ch2_cap_code_input,
        ]:
            w.textEdited.connect(self.on_meta_edit)
        self.meas_note_input.textChanged.connect(self.on_meta_edit)

        meta_form.addRow("Beam Energy (MeV):", self.beam_energy_input)
        meta_form.addRow("Beam Current (nA):", self.beam_current_input)
        meta_form.addRow("Detector Voltage (V):", self.det_voltage_input)
        meta_form.addRow("Depth (cm):", self.depth_input)
        meta_form.addRow("CH1 Cap Code (binary):", self.ch1_cap_code_input)
        meta_form.addRow("CH2 Cap Code (binary):", self.ch2_cap_code_input)
        meta_form.addRow("Problematic Measurement:", self.is_problematic_input)
        meta_form.addRow("Measurement Note:", self.meas_note_input)

        layout.addWidget(meta_group)
        
        # -----------------------------
        # 3) Read-Only Information
        # -----------------------------
        info_group = QGroupBox("Read-Only Information")
        info_form = QFormLayout(info_group)
        
        from PyQt6.QtWidgets import QLabel
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

    def populate_fields(self):
        self.blockSignals(True)
        # Workspace
        self.app_version_input.setText(self.session.app_version)
        self.fw_version_input.setText(self.session.firmware_version)
        self.hardware_id_ch1_input.setText(self.session.hardware_id_ch1)
        self.hardware_id_ch2_input.setText(self.session.hardware_id_ch2)
        self.facility_input.setText(self.session.facility)
        self.beam_type_input.setText(self.session.beam_type)

        self.adc_bits_input.blockSignals(True)
        self.adc_bits_input.setValue(self.session.adc_num_bits)
        self.adc_bits_input.blockSignals(False)

        self.adc_vref_input.blockSignals(True)
        self.adc_vref_input.setValue(self.session.adc_vref_volts)
        self.adc_vref_input.blockSignals(False)

        self.ch1_caps_input.setText(json.dumps(self.session.capacitor_base_values_ch1))
        self.ch2_caps_input.setText(json.dumps(self.session.capacitor_base_values_ch2))
        self.shift_note_input.setPlainText(self.session.shift_note)
        self.blockSignals(False)

        # Measurement
        self.beam_energy_input.setText(str(self.session.beam_energy_mev))
        self.beam_current_input.setText(str(self.session.beam_current_na))
        self.det_voltage_input.setText(str(self.session.detector_voltage_v))
        self.depth_input.setText(str(self.session.depth_cm))
        self.ch1_cap_code_input.setText(self.session.capacitor_code_ch1)
        self.ch2_cap_code_input.setText(self.session.capacitor_code_ch2)

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
        self.session.app_version = self.app_version_input.text()
        self.session.firmware_version = self.fw_version_input.text()
        self.session.hardware_id_ch1 = self.hardware_id_ch1_input.text()
        self.session.hardware_id_ch2 = self.hardware_id_ch2_input.text()
        self.session.facility = self.facility_input.text()
        self.session.beam_type = self.beam_type_input.text()
        self.session.adc_num_bits = self.adc_bits_input.value()
        self.session.adc_vref_volts = self.adc_vref_input.value()
        self.session.shift_note = self.shift_note_input.toPlainText()

        try:
            ch1_vals = json.loads(self.ch1_caps_input.text())
            if isinstance(ch1_vals, list):
                self.session.capacitor_base_values_ch1 = ch1_vals
        except json.JSONDecodeError:
            pass

        try:
            ch2_vals = json.loads(self.ch2_caps_input.text())
            if isinstance(ch2_vals, list):
                self.session.capacitor_base_values_ch2 = ch2_vals
        except json.JSONDecodeError:
            pass

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

        self.session.capacitor_code_ch1 = self.ch1_cap_code_input.text()
        self.session.capacitor_code_ch2 = self.ch2_cap_code_input.text()
        self.session.is_problematic = self.is_problematic_input.isChecked()
        self.session.measurement_note = self.meas_note_input.toPlainText()
