"""
Hardware config editor dialog.

Edits the config at the dict level (via hardware.config.editor_model) and
validates every change with the production loader, so the dialog cannot
produce a file the app would reject.  Values determined by the interface
(driver code, timing envelope, gain pins, ADC properties) are read-only;
bench facts (names, notes, serial port, capacitor values, wiring, role and
channel assignment) are editable.
"""
import copy
import json
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hardware.config import editor_model as em
from hardware.registry import registry_descriptors

logger = logging.getLogger(__name__)


def _format_parameter(param: dict) -> str:
    if param is None:
        return "derived by firmware"
    if "value" in param:
        return f"fixed: {param['value']:g} \u00b5s"
    return f"{param['min']:g} \u2013 {param['max']:g} \u00b5s (step {param.get('step', 1):g})"


class HardwareConfigEditorDialog(QDialog):
    """Edit a hardware config dict. `get_config()` returns the result on accept."""

    def __init__(self, config: dict, library_dirs: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hardware Configuration Editor")
        self.resize(860, 700)
        self._cfg = em.clone_config(config)
        self._library_dirs = library_dirs
        self._updating = False

        self._fragments, self._fragment_problems = em.list_fragments(library_dirs)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_devices_tab(), "Devices")
        self.tabs.addTab(self._build_circuits_tab(), "Circuits")
        self.tabs.addTab(self._build_channels_tab(), "Channels && Roles")
        self.tabs.addTab(self._build_connections_tab(), "Connections")

        self.errors_pane = QPlainTextEdit()
        self.errors_pane.setReadOnly(True)
        self.errors_pane.setMaximumHeight(110)
        self.errors_pane.setPlaceholderText("Validation results appear here.")

        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel("Validation (the file can only be saved when it has no errors):"))
        layout.addWidget(self.errors_pane)
        layout.addLayout(buttons)

        self._refresh()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        return self._cfg

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_devices_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left = QVBoxLayout()
        self.device_list = QListWidget()
        self.device_list.currentRowChanged.connect(self._on_device_selected)
        left.addWidget(self.device_list)

        btns = QVBoxLayout()
        self.add_device_btn = QPushButton("Add from Library…")
        self.add_device_btn.clicked.connect(self._show_device_library_menu)
        add_file_btn = QPushButton("Add from File…")
        add_file_btn.clicked.connect(self._add_device_from_file)
        self.remove_device_btn = QPushButton("Remove")
        self.remove_device_btn.clicked.connect(self._remove_device)
        save_comp_btn = QPushButton("Save as Component…")
        save_comp_btn.clicked.connect(lambda: self._save_component("device"))
        for b in (self.add_device_btn, add_file_btn, self.remove_device_btn, save_comp_btn):
            btns.addWidget(b)
        left.addLayout(btns)
        layout.addLayout(left, 1)

        form_group = QGroupBox("Device")
        self.device_form = QFormLayout(form_group)
        self.device_form.setContentsMargins(15, 20, 15, 15)
        layout.addWidget(form_group, 2)
        return widget

    def _build_circuits_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left = QVBoxLayout()
        self.circuit_list = QListWidget()
        self.circuit_list.currentRowChanged.connect(self._on_circuit_selected)
        left.addWidget(self.circuit_list)
        btns = QVBoxLayout()
        add_btn = QPushButton("Add Circuit")
        add_btn.clicked.connect(self._add_empty_circuit)
        self.remove_circuit_btn = QPushButton("Remove")
        self.remove_circuit_btn.clicked.connect(self._remove_circuit)
        save_comp_btn = QPushButton("Save as Component…")
        save_comp_btn.clicked.connect(lambda: self._save_component("circuit"))
        btns.addWidget(add_btn)
        btns.addWidget(self.remove_circuit_btn)
        btns.addWidget(save_comp_btn)
        left.addLayout(btns)
        layout.addLayout(left, 1)

        form_group = QGroupBox("Circuit")
        form = QFormLayout(form_group)
        form.setContentsMargins(15, 20, 15, 15)

        self.circuit_name_input = QLineEdit()
        self.circuit_name_input.textEdited.connect(self._on_circuit_field_changed)
        self.circuit_model_input = QLineEdit()
        self.circuit_model_input.textEdited.connect(self._on_circuit_field_changed)
        self.circuit_note_input = QLineEdit()
        self.circuit_note_input.textEdited.connect(self._on_circuit_field_changed)
        self.fixed_cap_input = QDoubleSpinBox()
        self.fixed_cap_input.setSuffix(" pF")
        self.fixed_cap_input.setRange(0.0, 1e6)
        self.fixed_cap_input.setDecimals(3)
        self.fixed_cap_input.valueChanged.connect(self._on_circuit_field_changed)

        form.addRow("Name:", self.circuit_name_input)
        form.addRow("Model:", self.circuit_model_input)
        form.addRow("Note:", self.circuit_note_input)
        form.addRow("Fixed capacitance:", self.fixed_cap_input)

        self.caps_table = QTableWidget(0, 3)
        self.caps_table.setHorizontalHeaderLabels(["Capacitor id", "Value (pF)", "Note"])
        self.caps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.caps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.caps_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.caps_table.itemChanged.connect(self._on_cap_table_changed)
        form.addRow("Switchable capacitors:", self.caps_table)

        cap_btns = QHBoxLayout()
        add_cap = QPushButton("Add capacitor")
        add_cap.clicked.connect(self._add_capacitor)
        remove_cap = QPushButton("Remove selected")
        remove_cap.clicked.connect(self._remove_capacitor)
        cap_btns.addWidget(add_cap)
        cap_btns.addWidget(remove_cap)
        form.addRow(cap_btns)

        layout.addWidget(form_group, 2)
        return widget

    def _build_channels_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self.role_timing_combo = QComboBox()
        self.role_acquisition_combo = QComboBox()
        self.role_gain_ch1_combo = QComboBox()
        self.role_gain_ch2_combo = QComboBox()
        form.addRow("Timing role:", self.role_timing_combo)
        form.addRow("Acquisition role:", self.role_acquisition_combo)
        form.addRow("CH1 gain controller:", self.role_gain_ch1_combo)
        form.addRow("CH2 gain controller:", self.role_gain_ch2_combo)
        layout.addLayout(form)

        channels_group = QGroupBox("Channels (a channel without circuit is inactive)")
        ch_form = QFormLayout(channels_group)
        self.ch1_circuit_combo = QComboBox()
        self.ch1_adc_input = QLineEdit()
        self.ch2_enable = QComboBox()
        self.ch2_enable.addItems(["single detector", "second detector (ch2)"])
        self.ch2_circuit_combo = QComboBox()
        self.ch2_adc_input = QLineEdit()
        ch_form.addRow("CH1 circuit:", self.ch1_circuit_combo)
        ch_form.addRow("CH1 ADC input (optional, e.g. ai0):", self.ch1_adc_input)
        ch_form.addRow("Detector count:", self.ch2_enable)
        ch_form.addRow("CH2 circuit:", self.ch2_circuit_combo)
        ch_form.addRow("CH2 ADC input (optional):", self.ch2_adc_input)
        layout.addWidget(channels_group)
        layout.addStretch(1)

        for combo in (
            self.role_timing_combo, self.role_acquisition_combo,
            self.role_gain_ch1_combo, self.role_gain_ch2_combo,
            self.ch1_circuit_combo, self.ch2_circuit_combo, self.ch2_enable,
        ):
            combo.currentIndexChanged.connect(self._on_assignment_changed)
        for line in (self.ch1_adc_input, self.ch2_adc_input):
            line.editingFinished.connect(self._on_assignment_changed)
        return widget

    def _build_connections_tab(self) -> QWidget:
        widget = QWidget()
        self.connections_layout = QVBoxLayout(widget)
        self.connections_layout.addWidget(
            QLabel(
                "Wire each capacitor of the channel's circuit to a GPIO pin of "
                "its gain controller (per-channel capacitor \u2192 pin routing)."
            )
        )
        self.connections_forms: dict[str, QFormLayout] = {}
        for channel in ("ch1", "ch2"):
            group = QGroupBox(f"Channel {channel}")
            form = QFormLayout(group)
            self.connections_forms[channel] = form
            self.connections_layout.addWidget(group)
        self.connections_layout.addStretch(1)
        return widget

    # ------------------------------------------------------------------
    # Refresh / validation
    # ------------------------------------------------------------------

    def _refresh(self):
        self._updating = True
        try:
            self._refresh_device_list()
            self._refresh_circuit_list()
            self._refresh_assignments()
            self._refresh_connections()
        finally:
            self._updating = False
        self._run_validation()

    def _capabilities_of(self, device_id: str) -> dict:
        return self._cfg["devices"].get(device_id, {}).get("capabilities", {})

    def _devices_with_capability(self, capability: str) -> list[str]:
        return [
            dev_id
            for dev_id, dev in self._cfg["devices"].items()
            if capability in dev.get("capabilities", {})
        ]

    def _refresh_device_list(self):
        self.device_list.blockSignals(True)
        self.device_list.clear()
        for dev_id in self._cfg["devices"]:
            self.device_list.addItem(QListWidgetItem(dev_id))
        self.device_list.blockSignals(False)
        self._refresh_device_form()

    def _refresh_device_form(self):
        form = self.device_form
        while form.rowCount():
            form.removeRow(0)

        dev_id = self._current_device_id()
        if dev_id is None:
            form.addRow(QLabel("No device selected."))
            return
        device = self._cfg["devices"][dev_id]
        caps = device.get("capabilities", {})

        id_label = QLabel(dev_id)
        id_label.setStyleSheet("font-weight: bold;")
        form.addRow("Id (fixed):", id_label)

        name_input = QLineEdit(str(device.get("name", "")))
        name_input.textEdited.connect(lambda text: self._on_device_field_changed("name", text))
        form.addRow("Name:", name_input)
        note_input = QLineEdit(str(device.get("note") or ""))
        note_input.textEdited.connect(lambda text: self._on_device_field_changed("note", text))
        form.addRow("Note:", note_input)

        readonly = QLabel(f"{device.get('driver', '?')}  (fixed by the interface)")
        readonly.setStyleSheet("color: #666;")
        form.addRow("Driver:", readonly)

        connection = device.get("connection", {})
        conn_type = connection.get("type", "?")
        form.addRow("Transport:", QLabel(conn_type))
        if conn_type == "serial":
            port_input = QLineEdit(str(connection.get("port") or ""))
            port_input.editingFinished.connect(
                lambda: self._on_connection_changed("port", port_input.text().strip())
            )
            form.addRow("Serial port:", port_input)
            baud = QSpinBox()
            baud.setRange(0, 10_000_000)
            baud.setValue(int(connection.get("baud_rate", 115200)))
            baud.valueChanged.connect(lambda v: self._on_connection_changed("baud_rate", v))
            form.addRow("Baud rate:", baud)
        elif conn_type == "dummy":
            form.addRow("", QLabel("Virtual device: no transport, no data."))

        def hint_suffix() -> str:
            return "   [fixed by the driver \u2014 edit the JSON file to change]"

        if "timing" in caps:
            timing = caps["timing"]
            rows = [
                ("Integration time", _format_parameter(timing.get("integration_time_us"))),
                ("Reset time", _format_parameter(timing.get("reset_time_us"))),
                ("Period", _format_parameter(timing.get("period_us"))),
            ]
            ramp = timing.get("ramp_sampling", {})
            ramp_text = "supported" if ramp.get("supported") else "not supported"
            if ramp.get("supported") and ramp.get("dt_us"):
                ramp_text += f"; dt {_format_parameter(ramp['dt_us'])}"
            rows.append(("Ramp sampling", ramp_text))
            for label, text in rows:
                value = QLabel(text + hint_suffix())
                value.setStyleSheet("color: #666;")
                form.addRow(f"{label}:", value)

        if "gain" in caps:
            value = QLabel(f"{caps['gain'].get('pins', [])}" + hint_suffix())
            value.setStyleSheet("color: #666;")
            form.addRow("GPIO pins:", value)

        if "acquisition" in caps:
            adc = caps["acquisition"].get("adc", {})
            ranges = adc.get("input_ranges_volts", [])
            ranges_text = ", ".join(
                f"[{r['min_volts']:g}, {r['max_volts']:g}] V" for r in ranges
            )
            text = (
                f"{adc.get('bits', '?')} bit; ranges: {ranges_text}; "
                f"channels: {caps['acquisition'].get('num_channels', '?')}"
            )
            value = QLabel(text + hint_suffix())
            value.setStyleSheet("color: #666;")
            value.setWordWrap(True)
            form.addRow("ADC:", value)

    def _refresh_circuit_list(self):
        self.circuit_list.blockSignals(True)
        self.circuit_list.clear()
        for circuit_id in self._cfg["circuits"]:
            self.circuit_list.addItem(QListWidgetItem(circuit_id))
        self.circuit_list.blockSignals(False)
        self._refresh_circuit_form()

    def _refresh_circuit_form(self):
        self.caps_table.blockSignals(True)
        circuit_id = self._current_circuit_id()
        if circuit_id is None:
            self.caps_table.setRowCount(0)
            for w in (
                self.circuit_name_input, self.circuit_model_input,
                self.circuit_note_input, self.fixed_cap_input,
            ):
                w.blockSignals(True)
                self.circuit_name_input.clear()
                self.circuit_model_input.clear()
                self.circuit_note_input.clear()
                self.fixed_cap_input.setValue(0)
                w.blockSignals(False)
            self.caps_table.blockSignals(False)
            return

        circuit = self._cfg["circuits"][circuit_id]
        for w, key in (
            (self.circuit_name_input, "name"),
            (self.circuit_model_input, "model"),
            (self.circuit_note_input, "note"),
        ):
            w.blockSignals(True)
            w.setText(str(circuit.get(key) or ""))
            w.blockSignals(False)
        self.fixed_cap_input.blockSignals(True)
        self.fixed_cap_input.setValue(float(circuit.get("fixed_capacitance_pF", 0.0)))
        self.fixed_cap_input.blockSignals(False)

        caps = circuit.get("switchable_capacitors", [])
        self.caps_table.setRowCount(len(caps))
        for row, capacitor in enumerate(caps):
            for col, value in enumerate((capacitor.get("id"), capacitor.get("value_pF"), capacitor.get("note") or "")):
                item = QTableWidgetItem(str(value))
                self.caps_table.setItem(row, col, item)
        self.caps_table.blockSignals(False)

    def _refresh_assignments(self):
        for combo, capability, role in (
            (self.role_timing_combo, "timing", "timing"),
            (self.role_acquisition_combo, "acquisition", "acquisition"),
            (self.role_gain_ch1_combo, "gain", "gain_ch1"),
            (self.role_gain_ch2_combo, "gain", "gain_ch2"),
        ):
            combo.blockSignals(True)
            current = self._cfg.get("roles", {}).get(role)
            combo.clear()
            combo.addItem("\u2014 none \u2014", None)
            for dev_id in self._devices_with_capability(capability):
                combo.addItem(dev_id, dev_id)
            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)

        circuits = list(self._cfg["circuits"])
        for combo, channel in ((self.ch1_circuit_combo, "ch1"), (self.ch2_circuit_combo, "ch2")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("\u2014 inactive \u2014", None)
            for circuit_id in circuits:
                combo.addItem(circuit_id, circuit_id)
            current = self._cfg.get("channels", {}).get(channel, {}).get("circuit")
            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.blockSignals(False)

        self.ch2_enable.blockSignals(True)
        self.ch2_enable.setCurrentIndex(1 if "ch2" in self._cfg.get("channels", {}) else 0)
        self.ch2_enable.blockSignals(False)
        ch2_active = "ch2" in self._cfg.get("channels", {})
        self.ch2_circuit_combo.setEnabled(ch2_active)
        self.ch2_adc_input.setEnabled(ch2_active)
        self.role_gain_ch2_combo.setEnabled(ch2_active)

        for line, channel in ((self.ch1_adc_input, "ch1"), (self.ch2_adc_input, "ch2")):
            line.blockSignals(True)
            line.setText(str(self._cfg.get("channels", {}).get(channel, {}).get("adc_input") or ""))
            line.blockSignals(False)

    def _refresh_connections(self):
        for channel, form in self.connections_forms.items():
            while form.rowCount():
                form.removeRow(0)
            group = form.parentWidget()
            if group is not None:
                group.setVisible(False)

        for channel in ("ch1", "ch2"):
            channel_cfg = self._cfg.get("channels", {}).get(channel)
            if not channel_cfg:
                continue
            circuit_id = channel_cfg.get("circuit")
            circuit = self._cfg["circuits"].get(circuit_id)
            gain_device = self._cfg.get("roles", {}).get(
                "gain_ch1" if channel == "ch1" else "gain_ch2"
            )
            if not circuit or not gain_device:
                label = QLabel(
                    f"Assign a circuit and a gain controller to channel {channel} first."
                )
                label.setStyleSheet("color: #a33;")
                self.connections_forms[channel].addRow(label)
                group = self.connections_forms[channel].parentWidget()
                if group is not None:
                    group.setVisible(True)
                continue

            pins = self._cfg["devices"][gain_device].get("capabilities", {}).get("gain", {}).get("pins", [])
            group = self.connections_forms[channel].parentWidget()
            if group is not None:
                group.setTitle(f"Channel {channel} \u2014 circuit {circuit_id}, gain device {gain_device}")
                group.setVisible(True)
            for capacitor in circuit.get("switchable_capacitors", []):
                combo = QComboBox()
                combo.addItem("(not wired)", None)
                for pin in pins:
                    combo.addItem(f"pin {pin}", pin)
                current = em.connection_of(self._cfg, channel, capacitor["id"])
                if current is not None:
                    index = combo.findData(current)
                    if index >= 0:
                        combo.setCurrentIndex(index)
                combo.currentIndexChanged.connect(
                    lambda _i, ch=channel, cap_id=capacitor["id"], cb=combo: self._on_connection_combo(ch, cap_id, cb)
                )
                self.connections_forms[channel].addRow(f"{capacitor['id']} ({capacitor['value_pF']:g} pF):", combo)

    def _run_validation(self):
        errors, warnings = em.validate(self._cfg, registry_descriptors())
        lines = [f"\u2716 {e}" for e in errors] + [f"\u26a0 {w}" for w in warnings]
        if not lines:
            if any(self._cfg.get(section) for section in ("devices", "circuits")):
                lines = ["\u2714 Configuration valid."]
            else:
                lines = ["Empty configuration \u2014 add devices and circuits."]
        self.errors_pane.setPlainText("\n".join(lines))
        self.save_btn.setEnabled(not errors)

    # ------------------------------------------------------------------
    # Slots: devices
    # ------------------------------------------------------------------

    def _current_device_id(self):
        item = self.device_list.currentItem()
        return item.text() if item else None

    def _on_device_selected(self, _row):
        if not self._updating:
            self._refresh_device_form()

    def _on_device_field_changed(self, key, value):
        dev_id = self._current_device_id()
        if dev_id:
            self._cfg["devices"][dev_id][key] = value

    def _on_connection_changed(self, key, value):
        dev_id = self._current_device_id()
        if dev_id:
            self._cfg["devices"][dev_id].setdefault("connection", {})[key] = value

    def _show_device_library_menu(self):
        menu = QMenu(self)
        by_kind = [f for f in self._fragments if f["kind"] == "device"]
        if not by_kind:
            menu.addAction("(no device fragments found \u2014 check hardware_library/)").setEnabled(False)
        for fragment in by_kind:
            action = menu.addAction(f"{fragment['label']}  [{fragment['suggested_id']}]")
            action.setData(fragment)
            action.triggered.connect(lambda _=False, f=fragment: self._add_fragment(f))
        menu.exec(self.add_device_btn.mapToGlobal(self.add_device_btn.rect().bottomLeft()))

    def _add_device_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Add Device Fragment", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            fragment = em.load_fragment(path)
            if fragment["kind"] != "device":
                raise ValueError("not a device fragment")
        except (ValueError, OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Fragment", f"Cannot load fragment:\n{e}")
            return
        self._add_fragment({"suggested_id": fragment.get("suggested_id", "device"), "data": fragment["data"]})

    def _add_fragment(self, fragment: dict):
        if fragment["kind"] == "device":
            new_id = em.add_device(self._cfg, fragment["data"], fragment.get("suggested_id", "device"))
            self._refresh()
            row = list(self._cfg["devices"]).index(new_id)
            self.device_list.setCurrentRow(row)
        else:
            new_id = em.add_circuit(self._cfg, fragment["data"], fragment.get("suggested_id", "circuit"))
            self._refresh()
            row = list(self._cfg["circuits"]).index(new_id)
            self.circuit_list.setCurrentRow(row)

    def _remove_device(self):
        dev_id = self._current_device_id()
        if dev_id is None:
            return
        notes = em.remove_device(self._cfg, dev_id)
        if notes:
            QMessageBox.information(self, "Device removed", "\n".join(notes))
        self._refresh()

    def _save_component(self, kind: str):
        if kind == "device":
            item_id = self._current_device_id()
            data = copy.deepcopy(self._cfg["devices"].get(item_id, {}))
        else:
            item_id = self._current_circuit_id()
            data = copy.deepcopy(self._cfg["circuits"].get(item_id, {}))
        if not data:
            QMessageBox.information(self, "Save as Component", "Select an entry first.")
            return
        if kind == "device":
            data.pop("connection", None)  # transport is machine-specific

        suggested = item_id or kind
        name, ok = QInputDialog.getText(self, "Save as Component", "Component id:", text=suggested)
        if not ok or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Component", f"{name.strip()}.json", "JSON Files (*.json)"
        )
        if not path:
            return
        fragment = em.fragment_to_json(data, kind, name.strip())
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(fragment, f, indent=4)
        except OSError as e:
            QMessageBox.warning(self, "Save as Component", f"Cannot write file:\n{e}")
            return
        self._fragments, self._fragment_problems = em.list_fragments(self._library_dirs)
        QMessageBox.information(self, "Save as Component", f"Component saved to {path}")

    # ------------------------------------------------------------------
    # Slots: circuits
    # ------------------------------------------------------------------

    def _current_circuit_id(self):
        item = self.circuit_list.currentItem()
        return item.text() if item else None

    def _on_circuit_selected(self, _row):
        if not self._updating:
            self._refresh_circuit_form()

    def _on_circuit_field_changed(self, *_):
        circuit_id = self._current_circuit_id()
        if not circuit_id:
            return
        circuit = self._cfg["circuits"][circuit_id]
        circuit["name"] = self.circuit_name_input.text()
        if self.circuit_model_input.text():
            circuit["model"] = self.circuit_model_input.text()
        else:
            circuit.pop("model", None)
        if self.circuit_note_input.text():
            circuit["note"] = self.circuit_note_input.text()
        else:
            circuit.pop("note", None)
        circuit["fixed_capacitance_pF"] = round(self.fixed_cap_input.value(), 6)
        self._run_validation()

    def _add_empty_circuit(self):
        new_id = em.add_circuit(
            self._cfg,
            {"name": "New circuit", "fixed_capacitance_pF": 0.0, "switchable_capacitors": []},
            "circuit",
        )
        self._refresh()
        self.circuit_list.setCurrentRow(list(self._cfg["circuits"]).index(new_id))

    def _remove_circuit(self):
        circuit_id = self._current_circuit_id()
        if circuit_id is None:
            return
        notes = em.remove_circuit(self._cfg, circuit_id)
        if notes:
            QMessageBox.information(self, "Circuit removed", "\n".join(notes))
        self._refresh()

    def _on_cap_table_changed(self, item):
        if self._updating:
            return
        circuit_id = self._current_circuit_id()
        if not circuit_id:
            return
        caps = self._cfg["circuits"][circuit_id].get("switchable_capacitors", [])
        row, col = item.row(), item.column()
        if row >= len(caps):
            return
        text = item.text().strip()
        try:
            if col == 0:  # id: update references
                old_id = caps[row].get("id")
                if text and text != old_id:
                    em.rename_capacitor(self._cfg, circuit_id, old_id, text)
            elif col == 1:
                caps[row]["value_pF"] = float(text)
            else:
                if text:
                    caps[row]["note"] = text
                else:
                    caps[row].pop("note", None)
        except (ValueError, KeyError):
            pass
        self._updating = True
        try:
            self._refresh_connections()
        finally:
            self._updating = False
        self._run_validation()

    def _add_capacitor(self):
        circuit_id = self._current_circuit_id()
        if not circuit_id:
            return
        caps = self._cfg["circuits"][circuit_id].setdefault("switchable_capacitors", [])
        base, n = "c100", 100
        existing = {c.get("id") for c in caps}
        while base in existing:
            n += 100
            base = f"c{n}"
        caps.append({"id": base, "value_pF": 100.0})
        self._refresh_circuit_form()
        self._refresh_connections()
        self._run_validation()

    def _remove_capacitor(self):
        circuit_id = self._current_circuit_id()
        row = self.caps_table.currentRow()
        if not circuit_id or row < 0:
            return
        caps = self._cfg["circuits"][circuit_id].get("switchable_capacitors", [])
        if row >= len(caps):
            return
        em.remove_capacitor(self._cfg, circuit_id, caps[row]["id"])
        self._refresh_circuit_form()
        self._refresh_connections()
        self._run_validation()

    # ------------------------------------------------------------------
    # Slots: assignments & connections
    # ------------------------------------------------------------------

    def _on_assignment_changed(self, *_):
        if self._updating:
            return
        self._apply_assignments()
        self._refresh_connections()
        self._run_validation()

    def _apply_assignments(self):
        for combo, role in (
            (self.role_timing_combo, "timing"),
            (self.role_acquisition_combo, "acquisition"),
            (self.role_gain_ch1_combo, "gain_ch1"),
            (self.role_gain_ch2_combo, "gain_ch2"),
        ):
            em.set_role(self._cfg, role, combo.currentData())
        em.set_channel(self._cfg, "ch1", self.ch1_circuit_combo.currentData(), self.ch1_adc_input.text().strip() or None)
        if self.ch2_enable.currentIndex() == 1:
            em.set_channel(self._cfg, "ch2", self.ch2_circuit_combo.currentData(), self.ch2_adc_input.text().strip() or None)
        else:
            em.set_channel(self._cfg, "ch2", None)
        self._refresh_assignments()

    def _on_connection_combo(self, channel, capacitor_id, combo):
        if self._updating:
            return
        try:
            em.set_connection(self._cfg, channel, capacitor_id, combo.currentData())
        except ValueError as e:
            QMessageBox.warning(self, "Connections", str(e))
        self._run_validation()
