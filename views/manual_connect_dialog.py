"""
Manual hardware connection dialog.

One row per configured device: the user maps each serial device to an
available port (physical devices — the udev persistent names belong to the
auto-connect path), connects them individually or all at once, and sees the
per-device result. Non-serial devices (the virtual acquisition) need no
port. The mapping is session-only: nothing is written back to the hardware
configuration.

Opened from the wizard's connection card (Manual Connect… button) or
automatically when Auto Connect fails (missing udev rules, Windows, device
not plugged).
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from services.serial_device import get_port

COLOR_OK = QColor("#155724")
COLOR_FAIL = QColor("#721c24")
COLOR_IDLE = QColor("#666666")

COL_DEVICE, COL_ROLES, COL_PORT, COL_STATUS, COL_BUTTON = range(5)


class ManualConnectDialog(QDialog):
    """Map each configured device to an available connection endpoint."""

    def __init__(self, hardware_manager, parent=None, initial_errors: dict = None):
        super().__init__(parent)
        self.hardware_manager = hardware_manager
        self._ports: list[dict] = []
        self._combos: dict[str, QComboBox] = {}
        self._status_items: dict[str, QTableWidgetItem] = {}
        self._rows: list[str] = []
        self._initial_errors = dict(initial_errors) if initial_errors else {}
        self.init_ui()
        self._populate_rows()
        self._refresh_ports()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def init_ui(self):
        self.setWindowTitle("Manual Connection")
        self.resize(950, 420)

        layout = QVBoxLayout(self)

        help_label = QLabel(
            "Map each configured device to an available serial port "
            "(ports already used by another device are hidden).\n"
            "Virtual devices need no port. The mapping is not saved."
        )
        help_label.setStyleSheet("font-size: 11pt; color: #666;")
        layout.addWidget(help_label)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Device", "Roles", "Connect To", "Status", ""])
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
        header.setSectionResizeMode(COL_DEVICE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ROLES, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_PORT, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_BUTTON, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-enumerate the available ports.")
        self.refresh_btn.clicked.connect(self._refresh_ports)

        self.connect_all_btn = QPushButton("Connect All")
        self.connect_all_btn.setStyleSheet(
            "QPushButton { background-color: #28a745; color: white; font-weight: bold;"
            " padding: 8px 16px; font-size: 12pt; border-radius: 5px; }"
            "QPushButton:hover { background-color: #218838; }"
            "QPushButton:disabled { background-color: #cccccc; color: #666; }"
        )
        self.connect_all_btn.clicked.connect(self._connect_all)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        self.done_btn = QPushButton("Done")
        self.done_btn.setEnabled(False)
        self.done_btn.setToolTip("Enabled when every role-assigned device is connected.")
        self.done_btn.setStyleSheet(
            "QPushButton { padding: 8px 16px; font-size: 12pt; }"
        )
        self.done_btn.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self.refresh_btn)
        buttons.addWidget(self.connect_all_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.done_btn)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate_rows(self):
        status = self.hardware_manager.connection_status()
        self._rows = list(status.keys())
        self.table.setRowCount(len(self._rows))
        for row, (dev_id, info) in enumerate(status.items()):
            roles = ", ".join(self.hardware_manager.device_roles(dev_id)) or "—"
            self.table.setItem(row, COL_DEVICE, QTableWidgetItem(f"{info['name']} ({dev_id})"))
            self.table.setItem(row, COL_ROLES, QTableWidgetItem(roles))

            if info["transport"] == "serial":
                combo = QComboBox()
                combo.currentIndexChanged.connect(lambda _idx, d=dev_id: self._on_combo_changed(d))
                self._combos[dev_id] = combo
                self.table.setCellWidget(row, COL_PORT, combo)
            else:
                self.table.setCellWidget(row, COL_PORT, QLabel("virtual — no port needed"))
                self.table.cellWidget(row, COL_PORT).setStyleSheet("color: #666; padding-left: 6px;")

            status_item = QTableWidgetItem()
            self._status_items[dev_id] = status_item
            self.table.setItem(row, COL_STATUS, status_item)
            if info["transport"] != "serial":
                self._set_status(dev_id, "virtual — no port needed" if not info["connected"] else "Connected",
                                 COLOR_IDLE if not info["connected"] else COLOR_OK)
            elif info["connected"]:
                self._set_status(dev_id, "Connected", COLOR_OK)
            elif dev_id in self._initial_errors:
                self._set_status(dev_id, f"Failed: {self._initial_errors[dev_id]}", COLOR_FAIL)
            else:
                self._set_status(dev_id, "Not connected", COLOR_IDLE)

            connect_btn = QPushButton("Connect")
            connect_btn.clicked.connect(lambda _checked, d=dev_id: self._connect_row(d))
            self.table.setCellWidget(row, COL_BUTTON, connect_btn)

        self._update_done_enabled()

    def _refresh_ports(self):
        self._ports = get_port()
        self._rebuild_combos()

    def _rebuild_combos(self):
        """Rebuild every combo, excluding the ports chosen in the other rows."""
        chosen = {
            dev_id: combo.currentData()
            for dev_id, combo in self._combos.items()
            if combo.currentData()
        }
        configured = {
            dev_id: info["port"]
            for dev_id, info in self.hardware_manager.connection_status().items()
        }
        taken_by_others = lambda dev_id: {p for d, p in chosen.items() if d != dev_id}

        for dev_id, combo in self._combos.items():
            combo.blockSignals(True)
            combo.clear()
            selected = chosen.get(dev_id)
            if selected is None:
                # preselect the configured port when it is currently present
                cfg = configured.get(dev_id)
                if cfg and cfg not in taken_by_others(dev_id) and any(p["device"] == cfg for p in self._ports):
                    selected = cfg
            if selected is None:
                combo.addItem("(select port…)", None)
            for port in self._ports:
                if port["device"] not in taken_by_others(dev_id):
                    name = port.get("name") or port["device"]
                    desc = port.get("description") or "-"
                    combo.addItem(f"{name} — {desc}", port["device"])
            if selected is not None:
                combo.setCurrentIndex(max(combo.findData(selected), 0))
            combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_combo_changed(self, dev_id: str):
        self._rebuild_combos()

    def _connect_row(self, dev_id: str):
        self._connect_dev_ids([dev_id])

    def _connect_all(self):
        self._connect_dev_ids(list(self._rows))

    def _connect_dev_ids(self, dev_ids: list[str]):
        to_connect = []
        for dev_id in dev_ids:
            combo = self._combos.get(dev_id)
            if combo is not None and not combo.currentData():
                self._set_status(dev_id, "select a port first", COLOR_FAIL)
                continue
            if combo is not None:
                self.hardware_manager.set_device_port(dev_id, combo.currentData())
            to_connect.append(dev_id)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for dev_id in to_connect:
                ok, error = self.hardware_manager.connect_device(dev_id)
                if ok:
                    self._set_status(dev_id, "Connected", COLOR_OK)
                else:
                    self._set_status(dev_id, f"Failed: {error}", COLOR_FAIL)
        finally:
            QApplication.restoreOverrideCursor()
        self._update_done_enabled()

    def _set_status(self, dev_id: str, text: str, color: QColor):
        item = self._status_items.get(dev_id)
        if item is not None:
            item.setText(text)
            item.setForeground(color)

    def _update_done_enabled(self):
        ok = self.hardware_manager.all_role_devices_connected()
        self.done_btn.setEnabled(ok)
