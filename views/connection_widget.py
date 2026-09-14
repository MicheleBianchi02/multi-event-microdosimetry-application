"""
Wizard step 1 — hardware connection card.

Shows the per-device connection status of the loaded hardware configuration
with three actions: Auto Connect (open every configured port — the udev
persistent names on Linux), Manual Connect… (per-device port mapping dialog,
the fallback when the udev rules are missing or on Windows) and Simulate
(synthetic data, no hardware).

The card replaces the old always-visible port table (SettingWindow): the
table only appears inside the manual-connect dialog.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

COLOR_OK = QColor("#155724")
COLOR_FAIL = QColor("#721c24")
COLOR_IDLE = QColor("#666666")


class ConnectionWidget(QWidget):
    """Connection status card: device table + Auto/Manual/Simulate actions."""

    connection_successfull = pyqtSignal()  # emitted when the hardware is fully connected (wizard gate)
    auto_connect_requested = pyqtSignal()
    manual_connect_requested = pyqtSignal()
    simulate_requested = pyqtSignal()

    def __init__(self, session_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        self._simulated = False
        self.init_ui()
        self.update_devices()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def init_ui(self):
        master = QVBoxLayout(self)
        master.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Device", "Roles", "Driver", "Port", "Status"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(
            "QTableWidget { font-size: 11pt; border: 1px solid #ccc; }"
            "QTableWidget::item { padding: 6px; }"
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        master.addWidget(self.table, 1)

        self.auto_btn = QPushButton("Auto Connect")
        self.auto_btn.setToolTip(
            "Connect to every device's configured port.\n"
            "On Linux these are the persistent names created by the udev rules\n"
            "(setup_udev_rules.sh); without them use Manual Connect."
        )
        self.auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_btn.setStyleSheet(
            "QPushButton { background-color: #28a745; color: white; font-weight: bold;"
            " padding: 8px 16px; font-size: 12pt; border-radius: 5px; }"
            "QPushButton:hover { background-color: #218838; }"
            "QPushButton:disabled { background-color: #cccccc; color: #666; }"
        )
        self.auto_btn.clicked.connect(self.auto_connect_requested.emit)

        self.manual_btn = QPushButton("Manual Connect…")
        self.manual_btn.setToolTip("Map each configured device to an available port, one row per device.")
        self.manual_btn.setStyleSheet(
            "QPushButton { padding: 8px 16px; font-size: 12pt; }"
        )
        self.manual_btn.clicked.connect(self.manual_connect_requested.emit)

        self.simulate_btn = QPushButton("Simulate")
        self.simulate_btn.setToolTip("Replace the hardware with synthetic data.")
        self.simulate_btn.setStyleSheet(
            "QPushButton { background-color: #007bff; color: white; font-weight: bold;"
            " padding: 8px 16px; font-size: 12pt; border-radius: 4px; }"
            "QPushButton:hover { background-color: #0069d9; }"
        )
        self.simulate_btn.clicked.connect(self.simulate_requested.emit)

        buttons = QHBoxLayout()
        buttons.addWidget(self.auto_btn)
        buttons.addWidget(self.manual_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.simulate_btn)
        master.addLayout(buttons)

        self.summary = QLabel("No hardware configuration loaded.")
        self.summary.setStyleSheet("font-size: 11pt; color: #666; padding: 2px;")
        self.summary.setWordWrap(True)
        master.addWidget(self.summary)

    # ------------------------------------------------------------------
    # State (called by the controller / app)
    # ------------------------------------------------------------------

    def update_devices(self):
        """Rebuild the device rows (e.g. after a config load/unload)."""
        self.table.setRowCount(0)
        manager = self.hardware_manager
        if not manager or not manager.is_loaded:
            self._set_summary("No hardware configuration loaded — load a workspace first.", COLOR_IDLE)
            return
        self._refresh_rows()

    def on_connect_result(self, success: bool, message: str = ""):
        """Called by the controller after an auto/manual connect attempt."""
        self._refresh_rows()
        if success:
            self._set_summary(message or "All devices connected.", COLOR_OK)
            self.connection_successfull.emit()
        elif message:
            self._set_summary(message, COLOR_FAIL)
        # without a message the k/N summary computed by _refresh_rows stays

    def set_simulated(self, simulated: bool = True):
        """Simulation active: hardware bypassed, auto/manual disabled."""
        self._simulated = simulated
        self.auto_btn.setEnabled(not simulated)
        self.manual_btn.setEnabled(not simulated)
        if simulated:
            self._set_summary("Simulation active — hardware bypassed.", COLOR_OK)
            self.connection_successfull.emit()
        else:
            self._refresh_rows()

    def restart_event(self):
        """Wizard restarted from the running window: reset the card."""
        self._simulated = False
        self.auto_btn.setEnabled(True)
        self.manual_btn.setEnabled(True)
        self.update_devices()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_rows(self):
        manager = self.hardware_manager
        status = manager.connection_status() if (manager and manager.is_loaded) else {}
        self.table.setRowCount(len(status))
        connected_count = 0
        for row, (dev_id, info) in enumerate(status.items()):
            roles = ", ".join(manager.device_roles(dev_id)) if manager else ""
            connected = info["connected"]
            connected_count += connected
            port = info["port"] or ("—" if info["transport"] == "serial" else "virtual")

            self.table.setItem(row, 0, QTableWidgetItem(f"{info['name']} ({dev_id})"))
            self.table.setItem(row, 1, QTableWidgetItem(roles or "—"))
            self.table.setItem(row, 2, QTableWidgetItem(info["driver"]))
            self.table.setItem(row, 3, QTableWidgetItem(str(port)))

            status_item = QTableWidgetItem("Connected" if connected else "Not connected")
            status_item.setForeground(COLOR_OK if connected else COLOR_IDLE)
            self.table.setItem(row, 4, status_item)

        if not status:
            return
        if connected_count == len(status):
            self._set_summary(f"All {len(status)} devices connected.", COLOR_OK)
        else:
            self._set_summary(f"{connected_count} of {len(status)} devices connected.", COLOR_FAIL)

    def _set_summary(self, text: str, color: QColor):
        bold = color in (COLOR_OK, COLOR_FAIL)
        self.summary.setText(text)
        self.summary.setStyleSheet(
            f"font-size: 11pt; padding: 2px; color: {color.name()};"
            f"{' font-weight: bold;' if bold else ''}"
        )
