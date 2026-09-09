from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, 
    QLineEdit, QTextEdit, QPushButton, QFileDialog, QMessageBox, QLabel, QComboBox, QGroupBox, QStackedWidget
)
from PyQt6.QtCore import Qt
import os
from services.shift_service import ShiftService
from core.constants import APP_VERSION, FACILITIES, BEAM_TYPES

class ShiftConfigWidget(QWidget):
    def __init__(self, session_model, parent=None):
        super().__init__(parent)
        self.session = session_model
        self.init_ui()
        self.populate_fields()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Active Workspace Label
        self.workspace_lbl = QLabel()
        self.workspace_lbl.setStyleSheet("color: #0078d7; font-weight: bold; font-size: 11pt;")
        layout.addWidget(self.workspace_lbl)

        # Main Group Box for elegance
        group_box = QGroupBox("Workspace Configuration")
        group_box.setStyleSheet("""
            QGroupBox {
                font-weight: bold; font-size: 13pt; border: 1px solid #ddd;
                border-radius: 8px; margin-top: 15px; background-color: #f8f9fa;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QLineEdit, QTextEdit, QComboBox {
                padding: 5px; border-radius: 4px; border: 1px solid #ccc; font-size: 11pt; background-color: white;
            }
            QLabel { font-size: 11pt; }
        """)
        
        form = QFormLayout(group_box)
        form.setContentsMargins(20, 25, 20, 20)
        form.setSpacing(10)
        
        # Facility Setup
        facility_layout = QHBoxLayout()
        self.facility_combo = QComboBox()
        self.facility_combo.addItems(FACILITIES)
        self.facility_combo.currentTextChanged.connect(self.on_facility_changed)
        
        self.facility_custom_input = QLineEdit()
        self.facility_custom_input.setPlaceholderText("Enter custom facility...")
        self.facility_custom_input.setVisible(False)
        
        facility_layout.addWidget(self.facility_combo)
        facility_layout.addWidget(self.facility_custom_input)
        
        # Beam Type Setup
        beam_type_layout = QHBoxLayout()
        self.beam_type_combo = QComboBox()
        self.beam_type_combo.currentTextChanged.connect(self.on_beam_type_changed)
        self.beam_type_custom_input = QLineEdit()
        self.beam_type_custom_input.setPlaceholderText("Enter custom beam type...")
        self.beam_type_custom_input.setVisible(False)
        
        beam_type_layout.addWidget(self.beam_type_combo)
        beam_type_layout.addWidget(self.beam_type_custom_input)
        
        # Other inputs
        self.app_version_input = QLineEdit()
        self.app_version_input.setPlaceholderText(f"Defaults to {APP_VERSION}")

        # Hardware configuration: picked here, copied into the workspace and
        # linked from setup.json (its content is validated by the app on load)
        self.hardware_config_input = QLineEdit()
        self.hardware_config_input.setPlaceholderText(
            "hardware_config.json to copy into this workspace (optional)"
        )
        self.hardware_config_browse = QPushButton("Browse…")
        self.hardware_config_browse.clicked.connect(self.on_browse_hardware_config)
        hw_cfg_layout = QHBoxLayout()
        hw_cfg_layout.addWidget(self.hardware_config_input, 1)
        hw_cfg_layout.addWidget(self.hardware_config_browse)

        self.shift_note_input = QTextEdit()
        self.shift_note_input.setMaximumHeight(80)

        form.addRow("Facility:", facility_layout)
        form.addRow("Beam Type:", beam_type_layout)
        form.addRow("App Version:", self.app_version_input)
        form.addRow("Hardware Config:", hw_cfg_layout)
        form.addRow("Shift Note:", self.shift_note_input)

        layout.addWidget(group_box)

        # Buttons
        btn_layout = QHBoxLayout()
        
        self.load_btn = QPushButton(" Load Existing Workspace...")
        self.load_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d; color: white; padding: 10px;
                border-radius: 5px; font-weight: bold; font-size: 11pt;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        self.load_btn.clicked.connect(self.on_load_clicked)
        
        self.save_btn = QPushButton(" Create/Save Workspace...")
        self.save_btn.clicked.connect(self.on_save_clicked)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #0078d7; color: white; padding: 10px;
                border-radius: 5px; font-weight: bold; font-size: 11pt;
            }
            QPushButton:hover { background-color: #005a9e; }
        """)

        btn_layout.addWidget(self.load_btn)
        btn_layout.addWidget(self.save_btn)
        
        layout.addSpacing(10)
        layout.addLayout(btn_layout)
        
        self.on_facility_changed(self.facility_combo.currentText())

    def on_facility_changed(self, text):
        if text == "Custom":
            self.facility_custom_input.setVisible(True)
            self.beam_type_combo.setVisible(False)
            self.beam_type_custom_input.setVisible(True)
        else:
            self.facility_custom_input.setVisible(False)
            self.beam_type_combo.setVisible(True)
            self.beam_type_custom_input.setVisible(False)
            
            self.beam_type_combo.blockSignals(True)
            self.beam_type_combo.clear()
            options = BEAM_TYPES.get(text, []).copy()
            options.append("Custom")
            self.beam_type_combo.addItems(options)
            self.beam_type_combo.blockSignals(False)
            
            # Trigger logic to show/hide custom input
            self.on_beam_type_changed(self.beam_type_combo.currentText())
            
    def on_beam_type_changed(self, text):
        if text == "Custom" or self.facility_combo.currentText() == "Custom":
            self.beam_type_custom_input.setVisible(True)
        else:
            self.beam_type_custom_input.setVisible(False)
            
    def set_facility_and_beam(self, facility, beam):
        if facility in FACILITIES and facility != "Custom":
            self.facility_combo.setCurrentText(facility)
            
            options = BEAM_TYPES.get(facility, [])
            if beam in options:
                self.beam_type_combo.setCurrentText(beam)
            else:
                self.beam_type_combo.setCurrentText("Custom")
                self.beam_type_custom_input.setText(beam)
        else:
            self.facility_combo.setCurrentText("Custom")
            self.facility_custom_input.setText(facility)
            self.beam_type_custom_input.setText(beam)

    def populate_fields(self):
        if getattr(self.session, 'active_workspace_dir', None):
            self.workspace_lbl.setText(f"Active Workspace: {self.session.active_workspace_dir}")
        else:
            self.workspace_lbl.setText("Active Workspace: None")

        self.set_facility_and_beam(self.session.facility, self.session.beam_type)

        self.app_version_input.setText(self.session.app_version)

        # show the config already linked to the active workspace, if any
        import json
        config_name = ""
        workspace = getattr(self.session, "active_workspace_dir", None)
        if workspace:
            setup_path = os.path.join(workspace, "setup.json")
            try:
                with open(setup_path, "r", encoding="utf-8") as f:
                    config_name = os.path.join(
                        workspace, json.load(f).get("hardware", {}).get("config_file", "")
                    )
            except Exception:
                config_name = ""
        self.hardware_config_input.setText(config_name if os.path.exists(config_name) else "")

        self.shift_note_input.setPlainText(self.session.shift_note)

    def update_session_from_fields(self):
        if self.facility_combo.currentText() == "Custom":
            self.session.facility = self.facility_custom_input.text()
        else:
            self.session.facility = self.facility_combo.currentText()
            
        if self.beam_type_combo.currentText() == "Custom" or self.facility_combo.currentText() == "Custom":
            self.session.beam_type = self.beam_type_custom_input.text()
        else:
            self.session.beam_type = self.beam_type_combo.currentText()
            
        self.session.app_version = self.app_version_input.text() or APP_VERSION
        self.session.shift_note = self.shift_note_input.toPlainText()

    def on_browse_hardware_config(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Choose Hardware Config", "", "JSON Files (*.json)"
        )
        if file_path:
            self.hardware_config_input.setText(file_path)

    def on_load_clicked(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load Workspace Setup", "", "JSON Files (*.json)"
        )
        if file_path:
            if ShiftService.load_setup(self.session, file_path):
                self.populate_fields()
                self._check_for_recovery()
                QMessageBox.information(self, "Success", "Workspace setup loaded.")
            else:
                QMessageBox.critical(self, "Error", "Failed to load setup file.")

    def on_save_clicked(self):
        self.update_session_from_fields()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Create/Save Workspace Setup", "", "JSON Files (*.json)"
        )
        if file_path:
            if not file_path.endswith(".json"):
                file_path += ".json"
            config_source = self.hardware_config_input.text().strip() or None
            if ShiftService.save_setup(self.session, file_path, config_source):
                self.populate_fields()
                folder_path = os.path.dirname(file_path)
                QMessageBox.information(self, "Success", f"Workspace created at:\n{folder_path}\nFile: {os.path.basename(file_path)}")
                self._check_for_recovery()
            else:
                QMessageBox.critical(self, "Error", "Failed to save setup file.")

    def _check_for_recovery(self):
        if not getattr(self.session, 'active_workspace_dir', None):
            return
        if ShiftService.check_for_recovery(self.session.active_workspace_dir):
            reply = QMessageBox.question(
                self, 
                "Recover Unsaved Data", 
                "An unsaved measurement was found in this workspace (likely from a crash or unsaved exit).\n\nWould you like to recover and save it to a zip file now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                ShiftService.recover_data(self.session.active_workspace_dir, self)
