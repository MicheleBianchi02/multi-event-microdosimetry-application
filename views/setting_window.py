from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SettingWindow(QWidget):
    connection_successfull = pyqtSignal()
    port_changed = pyqtSignal()
    ports_refresh_requested = pyqtSignal()
    simulate_requested = pyqtSignal()

    connect_requested = pyqtSignal(str, str)

    def __init__(self, session_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        self.selected_port = None
        self.selected_device = None
        self.init_ui()

    def init_ui(self):
        # --- Icons ---
        self.icon_refresh = self.style().standardIcon(
            QStyle.StandardPixmap.SP_BrowserReload
        )
        self.icon_error = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxCritical
        )
        self.icon_success = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogApplyButton
        )

        # --- Table Setup ---
        self.port_table = QTableWidget()
        self.port_table.setColumnCount(3)
        self.port_table.setHorizontalHeaderLabels(
            ["Port Name", "Description", "Manufacturer"]
        )

        self.port_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.port_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.port_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.port_table.setAlternatingRowColors(True)
        self.port_table.setShowGrid(False)
        self.port_table.verticalHeader().setVisible(False)

        # Table Header Styling
        header = self.port_table.horizontalHeader()
        # header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setStyleSheet(
            "QHeaderView::section { padding: 5px; font-weight: bold; font-size: 11pt; }"
        )

        # Table Font & Style
        self.port_table.setStyleSheet("""
            QTableWidget {
                font-size: 12pt;
                border: 1px solid #ccc;
            }
        
            QTableWidget::item {
                padding: 10px;
            }
        """)

        self.port_table.itemSelectionChanged.connect(self.on_selection_changed)

        # --- Buttons ---
        self.simulate_btn = QPushButton(" Simulate")
        self.simulate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.simulate_btn.clicked.connect(self.simulate_requested.emit)
        self.simulate_btn.setStyleSheet(
            "padding: 6px 10px; font-size: 11pt; background-color: #007bff; color: white; border-radius: 4px; font-weight: bold;"
        )

        self.refresh_btn = QPushButton(" Refresh List")
        self.refresh_btn.setIcon(self.icon_refresh)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.on_update_press)
        self.refresh_btn.setStyleSheet("padding: 6px 10px; font-size: 11pt;")

        self.verify_btn = QPushButton("Connect")
        self.verify_btn.setEnabled(False)  # Disabled until selection
        self.verify_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.verify_btn.clicked.connect(self.on_verify_press)
        self.verify_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745; 
                color: white; 
                font-weight: bold; 
                padding: 10px 20px; 
                font-size: 14pt;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #cccccc; color: #666; }
        """)

        # --- Status Display ---
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet("""
            QFrame {
                background-color: #f8f9fa;
                border: 1px solid #ddd;
                border-radius: 5px;
                padding: 10px;
            }
        """)
        self.status_frame.setVisible(False)

        self.status_icon = QLabel()
        self.status_icon.setStyleSheet("border: none;")
        self.status_msg = QLabel("Ready")
        self.status_msg.setStyleSheet(
            "font-size: 12pt; font-weight: bold; border: none;"
        )

        status_layout = QHBoxLayout()
        status_layout.addWidget(self.status_icon)
        status_layout.addWidget(self.status_msg, 1)
        self.status_frame.setLayout(status_layout)

        self.baud_label = QLabel(f"Baud Rate: <b>{self.session.baud_rate}</b> bps")
        self.baud_label.setStyleSheet("font-size: 11pt; color: #555; border: none;")

        self.setup_layout()

    def setup_layout(self):
        master = QVBoxLayout()
        self.setLayout(master)

        # Group Box for Port Selection
        group = QGroupBox("Select Serial Port")
        group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 14pt;
                margin-top: 10px;
                border: 1px solid #ddd;
                border-radius: 6px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        """)

        group_layout = QVBoxLayout()

        # Toolbar (Refresh button and Simulate button)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.simulate_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_btn)

        group_layout.addLayout(toolbar)
        group_layout.addWidget(self.port_table)

        group.setLayout(group_layout)

        # Bottom Action Area
        action_layout = QHBoxLayout()
        action_layout.addWidget(self.baud_label)
        action_layout.addStretch(1)
        
        self.dev_id_combo = QComboBox()
        self.dev_id_combo.setStyleSheet("padding: 5px; font-size: 12pt;")
        if self.hardware_manager:
            for dev_id in self.hardware_manager.devices.keys():
                self.dev_id_combo.addItem(dev_id)
                
        action_layout.addWidget(QLabel("Map to Device:"))
        action_layout.addWidget(self.dev_id_combo)
        action_layout.addWidget(self.verify_btn)

        master.addWidget(group, 1)  # Stretch factor 1 to take space
        master.addLayout(action_layout)
        master.addWidget(self.status_frame)
        master.addStretch(0)

        # The controller provides the initial port list after signals are bound.

    def fill_port_table(self, port_list: list[dict[str, str]]):
        self.port_table.setRowCount(0)  # Clear existing

        port_list = list(port_list)
        if self.simulate:
            port_list.insert(
                0,
                {
                    "name": "Simulated Port",
                    "description": "Virtual device for testing",
                    "manufacturer": "Software Emulation",
                    "device": "SIMULATOR",
                },
            )

        # Populate Table
        self.port_table.setRowCount(len(port_list))

        for row_idx, port in enumerate(port_list):
            # Safely get strings, defaulting to "-" if None
            p_name = str(port.get("name", "Unknown"))
            p_desc = str(port.get("description", "-"))
            p_mfg = str(port.get("manufacturer", "-"))

            # Clean up "n/a" or None values common in pyserial
            if p_desc.lower() == "n/a" or p_desc == "None":
                p_desc = "-"
            if p_mfg.lower() == "n/a" or p_mfg == "None":
                p_mfg = "-"

            item_name = QTableWidgetItem(p_name)
            item_name.setData(Qt.ItemDataRole.UserRole, port.get("device"))
            item_desc = QTableWidgetItem(p_desc)
            item_mfg = QTableWidgetItem(p_mfg)

            self.port_table.setItem(row_idx, 0, item_name)
            self.port_table.setItem(row_idx, 1, item_desc)
            self.port_table.setItem(row_idx, 2, item_mfg)

        self.port_table.resizeColumnsToContents()

        # Calculate and set fixed width
        width = self.port_table.verticalHeader().width()
        for i in range(self.port_table.columnCount()):
            width += self.port_table.columnWidth(i)
        self.port_table.setFixedWidth(width + 20)

    def on_selection_changed(self):
        selected_items = self.port_table.selectedItems()
        if selected_items:
            # Column 0 is the Name
            row = self.port_table.currentRow()
            item = self.port_table.item(row, 0)
            self.selected_port = item.text()
            self.selected_device = item.data(Qt.ItemDataRole.UserRole)

            self.verify_btn.setEnabled(True)
            self.verify_btn.setText(f"Connect to {self.selected_port}")

            # Hide status on new selection to avoid confusion
            self.status_frame.setVisible(False)

        else:
            self.selected_port = None
            self.selected_device = None
            self.verify_btn.setEnabled(False)
            self.verify_btn.setText("Connect")

        self.port_changed.emit()

    def on_update_press(self):
        self.port_table.blockSignals(True)
        self.port_table.clearSelection()
        self.port_table.setRowCount(0)
        self.port_table.blockSignals(False)
        self.selected_port = None
        self.selected_device = None
        self.verify_btn.setEnabled(False)
        self.verify_btn.setText("Connect")
        self.ports_refresh_requested.emit()

    def on_verify_press(self):
        """Verify the connection."""
        if not self.selected_port:
            return

        # Show pending status
        self.status_frame.setVisible(True)
        self.status_frame.setStyleSheet(
            "background-color: #fff3cd; border: 1px solid #ffeeba; border-radius: 5px;"
        )
        self.status_icon.setPixmap(
            self.icon_refresh.pixmap(24, 24)
        )  # Or a spinning wheel
        self.status_msg.setText(f"Attempting to connect to {self.selected_port}...")
        self.status_msg.setStyleSheet("color: #856404; font-weight: bold; border: none;")

        # Disable buttons while checking
        self.verify_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)

        dev_id = self.dev_id_combo.currentText()
        self.connect_requested.emit(dev_id, self.selected_port)

    def on_connection_result(self, success: bool, message: str = ""):
        self.status_frame.setVisible(True)
        if success:
            self.status_frame.setStyleSheet(
                "background-color: #d4edda; border: 1px solid #c3e6cb; border-radius: 5px;"
            )
            self.status_msg.setText(f"Success: {message or 'Connection established.'}")
            self.status_msg.setStyleSheet(
                "color: #155724; font-weight: bold; font-size: 12pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_success.pixmap(24, 24))
            self.verify_btn.setText("Connected")
            self.verify_btn.setEnabled(False)
            self.connection_successfull.emit()
        else:
            self.status_frame.setStyleSheet(
                "background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 5px;"
            )
            self.status_msg.setText(f"Failed: {message or 'Connection error.'}")
            self.status_msg.setStyleSheet(
                "color: #721c24; font-weight: bold; font-size: 12pt; border: none;"
            )
            self.status_icon.setPixmap(self.icon_error.pixmap(24, 24))

            self.verify_btn.setEnabled(True)
            self.verify_btn.setText("Retry Connection")

    def restart_event(self):
        self.status_frame.setVisible(False)
        self.port_table.clearSelection()
        self.selected_port = None
        self.selected_device = None
        self.verify_btn.setText("Connect")
        self.verify_btn.setEnabled(False)
        self.ports_refresh_requested.emit()
