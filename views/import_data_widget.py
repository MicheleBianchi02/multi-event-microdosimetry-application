import json
import os
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)


class ImportDataWidget(QWidget):
    load_signal = pyqtSignal(str)
    multi_analysis_requested = pyqtSignal(str)

    def __init__(self, session_model):
        super().__init__()
        self.session = session_model

        self.workspace_dir = None
        self.measurements = []

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(20, 30, 20, 30)

        # --- Header ---
        title = QLabel("Import Offline Data")
        title.setStyleSheet(
            "font-size: 24pt; font-weight: bold; color: #333; margin-bottom: 5px;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            "Select a workspace setup.json to view all its measurements, or load a single file."
        )
        subtitle.setStyleSheet("font-size: 14pt; color: #666; margin-bottom: 20px;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)
        btn_layout.addStretch()

        self.btn_workspace = QPushButton(" Open Workspace")
        icon_folder = self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        self.btn_workspace.setIcon(icon_folder)
        self.btn_workspace.setStyleSheet("""
            QPushButton {
                background-color: #0078d7; color: white; 
                font-size: 14pt; font-weight: bold; padding: 12px 25px; border-radius: 8px;
            }
            QPushButton:hover { background-color: #005a9e; }
        """)
        self.btn_workspace.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_workspace.clicked.connect(self.on_open_workspace)
        btn_layout.addWidget(self.btn_workspace)

        self.btn_single = QPushButton(" Load Single File")
        icon_file = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.btn_single.setIcon(icon_file)
        self.btn_single.setStyleSheet("""
            QPushButton {
                background-color: #6c757d; color: white; 
                font-size: 14pt; font-weight: bold; padding: 12px 25px; border-radius: 8px;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        self.btn_single.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_single.clicked.connect(self.on_load_single)
        btn_layout.addWidget(self.btn_single)

        self.btn_multi = QPushButton(" Multi-Measurement Analysis")
        icon_chart = self.style().standardIcon(
            QStyle.StandardPixmap.SP_FileDialogDetailedView
        )
        self.btn_multi.setIcon(icon_chart)
        self.btn_multi.setStyleSheet("""
            QPushButton {
                background-color: #28a745; color: white;
                font-size: 14pt; font-weight: bold; padding: 12px 25px; border-radius: 8px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #a8c9b0; }
        """)
        self.btn_multi.setToolTip(
            "Plot any result quantity of every measurement in the workspace "
            "against any other, with custom formulas on both axes"
        )
        self.btn_multi.setEnabled(False)
        self.btn_multi.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_multi.clicked.connect(self.on_multi_analysis)
        btn_layout.addWidget(self.btn_multi)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addSpacing(30)

        # --- Workspace Info ---
        self.workspace_info_group = QGroupBox("Workspace Information")
        self.workspace_info_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold; font-size: 14pt; border: 1px solid #ddd;
                border-radius: 8px; margin-top: 15px; background-color: #f8f9fa;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        """)
        self.workspace_info_layout = QGridLayout(self.workspace_info_group)
        self.workspace_info_layout.setColumnStretch(1, 1)
        self.workspace_info_group.setVisible(False)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )

        # Container for everything inside the scroll area
        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll_layout.addWidget(self.workspace_info_group)

        # --- Filters ---
        self.filter_group = QGroupBox("Filters")
        self.filter_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold; font-size: 12pt; border: 1px solid #ddd;
                border-radius: 8px; margin-top: 10px; background-color: #f8f9fa;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QComboBox { padding: 5px; border-radius: 4px; border: 1px solid #ccc; font-size: 11pt; }
        """)
        self.filter_layout = QHBoxLayout(self.filter_group)
        self.filter_group.setVisible(False)

        self.filter_combos = {}
        self.filter_keys = [
            ("Energy", "beam_energy_MeV"),
            ("Current", "beam_current_nA"),
            ("Det Volt", "detector_voltage_V"),
            ("Depth", "depth_cm"),
        ]

        for display_name, dict_key in self.filter_keys:
            v_layout = QVBoxLayout()
            lbl = QLabel(display_name)
            lbl.setStyleSheet("font-size: 10pt; color: #555;")
            combo = QComboBox()
            combo.currentIndexChanged.connect(self.apply_filters)
            v_layout.addWidget(lbl)
            v_layout.addWidget(combo)
            self.filter_layout.addLayout(v_layout)
            self.filter_combos[dict_key] = combo

        reset_btn = QPushButton("Reset Filters")
        reset_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d; color: white; padding: 6px 15px;
                border-radius: 4px; border: 1px solid #6c757d;
                font-weight: bold; font-size: 11pt;
            }
            QPushButton:hover { background-color: #5a6268; border: 1px solid #5a6268; }
        """)
        reset_btn.clicked.connect(self.reset_filters)
        
        btn_layout = QVBoxLayout()
        dummy_lbl = QLabel("")
        dummy_lbl.setStyleSheet("font-size: 10pt;")
        btn_layout.addWidget(dummy_lbl)
        btn_layout.addWidget(reset_btn)
        self.filter_layout.addLayout(btn_layout)

        self.scroll_layout.addWidget(self.filter_group)
        self.scroll_layout.addSpacing(10)

        self.cards_widget = QWidget()
        self.cards_widget.setStyleSheet("background: transparent;")
        self.cards_layout = QGridLayout(self.cards_widget)
        self.cards_layout.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        self.cards_layout.setSpacing(20)

        self.scroll_layout.addWidget(self.cards_widget)

        self.scroll_area.setWidget(self.scroll_content)
        layout.addWidget(self.scroll_area, 1)

    def on_open_workspace(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Workspace Setup File", "", "JSON Files (*.json)"
        )
        if not file_path:
            return

        try:
            from services.shift_service import ShiftService
            ShiftService.load_setup(self.session, file_path)
            
            with open(file_path, "r") as f:
                setup_data = json.load(f)
            self.display_workspace_info(setup_data)
        except Exception as e:
            print(f"Failed to load setup.json: {e}")
            self.workspace_info_group.setVisible(False)

        self.workspace_dir = os.path.dirname(file_path)
        self.scan_workspace()

    def on_multi_analysis(self):
        if self.workspace_dir:
            self.multi_analysis_requested.emit(self.workspace_dir)

    def display_workspace_info(self, setup_data):
        # Clear existing layout
        for i in reversed(range(self.workspace_info_layout.count())):
            widget = self.workspace_info_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        exp = setup_data.get("experimental_setup", {})
        hw = setup_data.get("hardware_settings", {})
        ver = setup_data.get("versions", {})
        hardware_link = setup_data.get("hardware", {})

        adc = hw.get("adc", {})
        ch1 = hw.get("channels", {}).get("ch1", {})
        ch2 = hw.get("channels", {}).get("ch2", {})

        info = [
            ("App Version:", ver.get("app_version", "N/A")),
            ("Facility:", exp.get("facility", "N/A")),
            ("Particle:", exp.get("particle", "N/A")),
            ("Beam Type:", exp.get("beam_type", "N/A")),
            ("ADC Bits:", adc.get("bits", hw.get("adc_num_bits", "N/A"))),
            (
                "ADC Range:",
                adc.get("input_range_volts") and (
                    f"{adc['input_range_volts'].get('min_volts', 'N/A')} … "
                    f"{adc['input_range_volts'].get('max_volts', 'N/A')} V"
                ),
            ),
            (
                "CH1 Gain:",
                ch1.get("capacitance_pF") and f"{ch1['capacitance_pF']:g} pF"
                or hw.get("channels", {}).get("ch1", {}).get("capacitor_base_values_pF", "N/A"),
            ),
            (
                "CH2 Gain:",
                ch2.get("capacitance_pF") and f"{ch2['capacitance_pF']:g} pF"
                or hw.get("channels", {}).get("ch2", {}).get("capacitor_base_values_pF", "N/A"),
            ),
            (
                "Hardware Config:",
                hardware_link.get("config_file", "N/A")
                + (f" (sha {hardware_link['config_sha256'][:8]}…)" if hardware_link.get("config_sha256") else ""),
            ),
            ("Shift Note:", exp.get("shift_note", "None") or "None"),
        ]

        row = 0
        for label, value in info:
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #666; font-size: 12pt; font-weight: bold;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
            val = QLabel(str(value))
            val.setStyleSheet("color: #222; font-size: 12pt;")
            val.setWordWrap(True)
            val.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

            self.workspace_info_layout.addWidget(lbl, row, 0)
            self.workspace_info_layout.addWidget(val, row, 1)
            row += 1

        self.workspace_info_group.setVisible(True)

    def on_load_single(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Measurement File", "", "Data Files (*.zip *.csv)"
        )
        if file_path:
            self.load_signal.emit(file_path)

    def scan_workspace(self):
        # Clear existing cards
        for i in reversed(range(self.cards_layout.count())):
            widget = self.cards_layout.itemAt(i).widget()
            if widget is not None:
                widget.setParent(None)

        if not self.workspace_dir or not os.path.isdir(self.workspace_dir):
            return

        # Shared scanner (also feeds the multi-measurement analysis);
        # records come oldest-first, the cards show newest-first
        from services.measurement_catalog import scan_workspace as scan_dir

        measurements = scan_dir(self.workspace_dir)
        measurements.reverse()
        self.measurements = measurements
        self.btn_multi.setEnabled(True)

        if not measurements:
            no_data = QLabel("No .zip measurements found in this workspace.")
            no_data.setStyleSheet("font-size: 14pt; color: #888; font-style: italic;")
            self.cards_layout.addWidget(no_data, 0, 0)
            self.filter_group.setVisible(False)
            return

        # Populate filter comboboxes
        self.populate_filters(measurements)
        self.filter_group.setVisible(True)

        # Build cards
        self.card_widgets = []
        col_count = 2
        row = 0
        col = 0
        for m in measurements:
            card = self.build_card(m)
            card.m_data = m  # Attach metadata for filtering
            self.card_widgets.append(card)
            self.cards_layout.addWidget(card, row, col)
            col += 1
            if col >= col_count:
                col = 0
                row += 1

        self.apply_filters()

    def populate_filters(self, measurements):
        for display_name, dict_key in self.filter_keys:
            combo = self.filter_combos[dict_key]
            combo.blockSignals(True)
            combo.clear()

            # Extract unique values
            unique_values = set()
            for m in measurements:
                exp = m.settings.get("experimental_setup", {})
                val = exp.get(dict_key, None)
                if val is not None:
                    unique_values.add(str(val))

            combo.addItem("All")
            for val in sorted(unique_values):
                combo.addItem(val)

            combo.blockSignals(False)

    def reset_filters(self):
        for combo in self.filter_combos.values():
            combo.setCurrentIndex(0)

    def apply_filters(self):
        # Gather active filters
        active_filters = {}
        for dict_key, combo in self.filter_combos.items():
            current_text = combo.currentText()
            if current_text != "All":
                active_filters[dict_key] = current_text

        for card in self.card_widgets:
            exp = card.m_data.settings.get("experimental_setup", {})
            matches = True
            for dict_key, filter_val in active_filters.items():
                card_val = str(exp.get(dict_key, ""))
                if card_val != filter_val:
                    matches = False
                    break
            card.setVisible(matches)

    def build_card(self, m_data):
        card = QFrame()
        card.setMinimumSize(280, 200)
        card.setMaximumWidth(350)
        card.setStyleSheet("""
            QFrame {
                background-color: white;
                border: 1px solid #ddd;
                border-radius: 10px;
            }
            QFrame:hover {
                border: 2px solid #0078d7;
            }
        """)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 15, 15, 15)

        settings = m_data.settings
        info = settings.get("measurement_info", {})
        exp = settings.get("experimental_setup", {})
        timing = settings.get("timing_settings", {})

        # Format Timestamp
        ts_raw = info.get("timestamp_start", "")
        try:
            dt = datetime.fromisoformat(ts_raw)
            ts_str = dt.strftime("%b %d, %Y - %H:%M:%S")
        except ValueError:
            ts_str = ts_raw or m_data.filename
            
        if info.get("is_problematic", False):
            ts_str += " <span style='color: #e74c3c; font-size: 16pt;'>⚠</span>"

        header = QLabel(ts_str)
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setStyleSheet(
            "font-weight: bold; font-size: 14pt; color: #333; border: none;"
        )
        layout.addWidget(header)

        # Info Grid
        info_layout = QGridLayout()
        info_layout.setContentsMargins(0, 10, 0, 10)
        info_layout.setVerticalSpacing(5)

        def add_row(r, label, value):
            lbl = QLabel(label)
            lbl.setStyleSheet(
                "color: #666; font-size: 10pt; font-weight: bold; border: none;"
            )
            val = QLabel(str(value))
            val.setStyleSheet("color: #333; font-size: 10pt; border: none;")
            info_layout.addWidget(lbl, r, 0)
            info_layout.addWidget(val, r, 1)

        int_time_s = timing.get("integration_time_s", 0.0)
        int_time_ms = float(int_time_s) * 1000

        add_row(0, "Energy:", f"{exp.get('beam_energy_MeV', 0)} MeV")
        add_row(1, "Current:", f"{exp.get('beam_current_nA', 0)} nA")
        add_row(2, "Det Volt:", f"{exp.get('detector_voltage_V', 0)} V")
        add_row(3, "Depth:", f"{exp.get('depth_cm', 0)} cm")
        add_row(4, "Int Time:", f"{int_time_ms:.3f} ms")

        layout.addLayout(info_layout)

        note = exp.get("measurement_note", "")
        note_lbl = QLabel(note if note else "No note provided.")
        note_lbl.setStyleSheet(
            "font-style: italic; color: #888; font-size: 9pt; border: none;"
        )
        note_lbl.setWordWrap(True)
        layout.addWidget(note_lbl, 1)

        # Load button overlay
        load_btn = QPushButton("Load Measurement")
        load_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        load_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745; color: white; font-weight: bold;
                border-radius: 5px; padding: 6px;
            }
            QPushButton:hover { background-color: #218838; }
        """)
        load_btn.clicked.connect(lambda _, p=m_data.path: self.load_signal.emit(p))
        layout.addWidget(load_btn)

        return card
