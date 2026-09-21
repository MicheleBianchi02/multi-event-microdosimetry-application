"""
Multi-measurement analysis dialog (non-modal): plot any result-dict or
metadata quantity of a whole workspace against any other, with custom
formulas on both axes.

Layout: measurement table with filters and per-measurement checkboxes
plus the variable picker on the left, the plot filling the right side,
and a thin calculator strip at the bottom (X expression, one or more Y
expressions — one per line, each drawn in its own colour — grouping,
units, presets, export). The current (in-progress or loaded)
measurement can be included as a distinct "live" point and refreshed on
demand.

The "Units" box chooses between channels (raw ADC counts) and charge
(pC): the variable picker then shows only that variant, and switching
rewrites the formulas accordingly. All data logic lives in
services/measurement_catalog.py, services/variable_catalog.py and
services/multi_analysis_service.py; this class is only presentation.
"""

import csv
import json
import os
import re

import pyqtgraph as pg

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.formula import FormulaError, validate
from services.measurement_catalog import build_live_record, scan_workspace
from services.multi_analysis_service import (
    FILTER_VAR_IDS,
    GROUP_VAR_IDS,
    apply_filters,
    distinct_values,
    format_value,
    refresh_values,
    run_pipeline,
)
from services.variable_catalog import default_catalog

PALETTE = [
    "#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    "#393b79", "#843c39",
]

PRESETS_FILENAME = "analysis_presets.json"

DEFAULT_PRESETS = [
    {"name": "ȳ_D vs depth", "x": "depth_cm", "y": "autocov_yd_ch1", "group": "int_time_ms"},
    {"name": "Mean / current vs current", "x": "beam_current_nA", "y": "autocov_mean_ch1 / beam_current_nA", "group": "int_time_ms"},
    {"name": "ȳ_D vs measurement index", "x": "index", "y": "autocov_yd_ch1", "group": ""},
    {"name": "Mean vs time", "x": "timestamp", "y": "autocov_mean_ch1", "group": ""},
]

UNITS_CHANNELS, UNITS_PC = 0, 1

_GROUPBOX_STYLE = """
    QGroupBox {
        font-weight: bold; font-size: 11pt; border: 1px solid #ddd;
        border-radius: 8px; margin-top: 12px; background-color: #f8f9fa;
    }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
"""


class MultiAnalysisDialog(QDialog):
    """Non-modal workspace-wide analysis window. Opened from the offline
    import page and from the running window; one shared instance lives
    on the main window (View.open_multi_analysis)."""

    def __init__(self, session_model, data_model, hardware_manager=None, parent=None):
        super().__init__(parent)
        self.session = session_model
        self.data_model = data_model
        self.hardware_manager = hardware_manager
        self.catalog = default_catalog()

        self.workspace_dir = None
        self.records = []
        self.live_record = None
        self.user_presets = []
        self._last_expr_field = None
        self._last_result = None

        self.setWindowTitle("Multi-Measurement Analysis")
        self.setModal(False)
        self.resize(1400, 900)
        self._build_ui()

        # Debounced re-evaluation while typing formulas
        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(450)
        self._replot_timer.timeout.connect(self.replot)

    # --- public API ---------------------------------------------------------

    def open_with(self, workspace_dir=None, focus_live=False):
        """Show the dialog, (re)scanning the given workspace folder (falls
        back to the session's active workspace). ``focus_live`` pre-checks
        the live-point inclusion (entry from the running window)."""
        if focus_live:
            self.live_check.setChecked(True)
        target = workspace_dir or getattr(self.session, "active_workspace_dir", None)
        if target and target != self.workspace_dir:
            self.workspace_dir = target
            self._load_user_presets()
        self.rescan(refresh_filters=True)
        self.show()
        self.raise_()
        self.activateWindow()

    # --- data ---------------------------------------------------------------

    def rescan(self, refresh_filters=False):
        self.workspace_lbl.setText(self.workspace_dir or "No workspace loaded")
        self.records = scan_workspace(self.workspace_dir)
        refresh_values(self.records, self.catalog)
        if refresh_filters:
            self._remember_filter_selection()
        self._populate_filters()
        self._refresh_table()
        self.replot()

    def _active_records(self):
        """Filtered + checkbox-selected records (the pipeline input)."""
        active_filters = {
            var_id: combo.currentData()
            for var_id, combo in self.filter_combos.items()
            if combo.currentIndex() > 0
        }
        visible = apply_filters(self.records, active_filters)
        if self.exclude_problematic_check.isChecked():
            visible = [r for r in visible if not r.is_problematic]
        return [r for r in visible if r.selected]

    def _rebuild_live_record(self):
        self.live_record = build_live_record(
            self.session, self.data_model, self.hardware_manager
        )
        refresh_values([self.live_record], self.catalog)

    # --- units (channels / pC) ------------------------------------------------

    def _unit_translation(self):
        """{counts id -> charge id} and its inverse, over the result
        variables that have both variants."""
        to_pc, to_counts = {}, {}
        for var in self.catalog:
            if not var.channel:
                continue
            if var.unit == "pC":
                base = var.var_id.replace("_pc_", "_")
            elif var.unit == "pC²":
                base = var.var_id.replace("_pc2_", "_")
            else:
                continue
            if self.catalog.get(base) is not None:
                to_pc[base] = var.var_id
                to_counts[var.var_id] = base
        return to_pc, to_counts

    @staticmethod
    def _translate_expr(expr, mapping):
        for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            expr = re.sub(rf"\b{re.escape(old)}\b", new, expr)
        return expr

    def on_units_changed(self, *_args):
        pc_mode = self.units_combo.currentIndex() == UNITS_PC
        to_pc, to_counts = self._unit_translation()
        mapping = to_pc if pc_mode else to_counts
        self.x_edit.setText(self._translate_expr(self.x_edit.text(), mapping))
        self.y_edit.setPlainText(
            "\n".join(
                self._translate_expr(line.strip(), mapping)
                for line in self.y_edit.toPlainText().splitlines()
                if line.strip()
            )
        )
        self._apply_variable_filter()
        self.replot()

    # --- UI construction ----------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("Multi-Measurement Analysis")
        title.setStyleSheet("font-size: 16pt; font-weight: bold; color: #333;")
        header.addWidget(title)
        header.addStretch()
        self.workspace_lbl = QLabel("No workspace loaded")
        self.workspace_lbl.setStyleSheet("color: #666;")
        header.addWidget(self.workspace_lbl)
        browse_btn = QPushButton("Choose Workspace…")
        browse_btn.clicked.connect(self.on_choose_workspace)
        header.addWidget(browse_btn)
        self.refresh_btn = QPushButton("Refresh")
        refresh_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        self.refresh_btn.setIcon(refresh_icon)
        self.refresh_btn.setToolTip(
            "Rescan the workspace folder and rebuild the live point"
        )
        self.refresh_btn.clicked.connect(self.on_refresh)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        body = QHBoxLayout()

        # --- left: filters, measurement table, variable picker
        left = QVBoxLayout()
        filters_box = QGroupBox("Filters")
        filters_box.setStyleSheet(_GROUPBOX_STYLE)
        fgrid = QGridLayout(filters_box)
        self.filter_combos = {}
        for col, var_id in enumerate(FILTER_VAR_IDS):
            var = self.catalog.get(var_id)
            fgrid.addWidget(QLabel(var.label if var else var_id), 0, col)
            combo = QComboBox()
            combo.currentIndexChanged.connect(self.on_filters_changed)
            fgrid.addWidget(combo, 1, col)
            self.filter_combos[var_id] = combo
        self.exclude_problematic_check = QCheckBox("Exclude ⚠ problematic")
        self.exclude_problematic_check.setChecked(True)
        self.exclude_problematic_check.stateChanged.connect(self.on_filters_changed)
        fgrid.addWidget(self.exclude_problematic_check, 2, 0, 1, 2)
        sel_all = QPushButton("Select visible")
        sel_all.clicked.connect(lambda: self._select_visible(True))
        sel_none = QPushButton("Deselect visible")
        sel_none.clicked.connect(lambda: self._select_visible(False))
        fgrid.addWidget(sel_all, 2, 2)
        fgrid.addWidget(sel_none, 2, 3)
        fgrid.addWidget(QWidget(), 2, 4)  # keep the checkbox left-aligned
        left.addWidget(filters_box)

        self.table = self._build_table()
        self.table.itemChanged.connect(self.on_table_item_changed)
        left.addWidget(self.table, 1)

        left.addWidget(self._build_variable_picker())
        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMaximumWidth(560)
        left_widget.setMinimumWidth(430)
        body.addWidget(left_widget, 0)

        # --- right: plot (takes all remaining space)
        right = QVBoxLayout()
        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot = self.plot_widget.getPlotItem()
        self.legend = self.plot.addLegend(offset=(10, 10))
        right.addWidget(self.plot_widget, 1)
        right_widget = QWidget()
        right_widget.setLayout(right)
        body.addWidget(right_widget, 1)
        layout.addLayout(body, 1)

        layout.addWidget(self._build_calculator())

        self.warning_lbl = QLabel("")
        self.warning_lbl.setStyleSheet("color: #b3600b;")
        self.warning_lbl.setWordWrap(True)
        layout.addWidget(self.warning_lbl)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #555;")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)

    def _build_table(self):
        columns = ["", "Time", "Depth [cm]", "Int [ms]", "I [nA]", "E [MeV]", ""]
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(2, len(columns)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(columns) - 1, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def _build_variable_picker(self):
        box = QGroupBox("Variables")
        box.setStyleSheet(_GROUPBOX_STYLE)
        picker = QVBoxLayout(box)

        top = QHBoxLayout()
        top.addWidget(QLabel("Units:"))
        self.units_combo = QComboBox()
        self.units_combo.addItem("Channels (counts)", UNITS_CHANNELS)
        self.units_combo.addItem("Charge (pC)", UNITS_PC)
        self.units_combo.currentIndexChanged.connect(self.on_units_changed)
        top.addWidget(self.units_combo)
        self.var_search = QLineEdit()
        self.var_search.setPlaceholderText("Filter variables…")
        self.var_search.textChanged.connect(self.on_variable_search)
        top.addWidget(self.var_search, 1)
        picker.addLayout(top)

        self.variable_tree = QTreeWidget()
        self.variable_tree.setHeaderLabels(["Variable", "Unit"])
        self.variable_tree.setRootIsDecorated(True)
        self.variable_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.variable_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.variable_tree.itemDoubleClicked.connect(self.on_variable_activated)
        for category, variables in self.catalog.categories().items():
            cat_item = QTreeWidgetItem([category, ""])
            for var in variables:
                item = QTreeWidgetItem([var.label, var.unit])
                item.setData(0, Qt.ItemDataRole.UserRole, var.var_id)
                item.setToolTip(0, f"{var.var_id} — {var.description}")
                item.setToolTip(1, item.toolTip(0))
                cat_item.addChild(item)
            self.variable_tree.addTopLevelItem(cat_item)
        self.variable_tree.expandAll()
        self._apply_variable_filter()
        picker.addWidget(self.variable_tree, 1)
        hint = QLabel("Double-click a variable to insert it into the last used expression field.")
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        hint.setWordWrap(True)
        picker.addWidget(hint)
        return box

    def _build_calculator(self):
        box = QGroupBox("Calculator — X and Y expressions (variables, + − × ÷ ** and parentheses; one Y per line = one curve)")
        box.setStyleSheet(_GROUPBOX_STYLE)
        grid = QGridLayout(box)
        grid.setVerticalSpacing(6)

        self.x_edit = QLineEdit()
        self.x_edit.installEventFilter(self)
        self.x_edit.textChanged.connect(self._schedule_validation)
        self.x_status = QLabel("")
        self.y_status = QLabel("")

        self.y_edit = QPlainTextEdit()
        self.y_edit.setPlaceholderText(
            "one expression per line — e.g.\n"
            "autocov_yd_ch1\n"
            "varcov_yd_ch1"
        )
        self.y_edit.setFixedHeight(64)
        self.y_edit.installEventFilter(self)
        self.y_edit.textChanged.connect(self._schedule_validation)

        grid.addWidget(QLabel("X:"), 0, 0)
        grid.addWidget(self.x_edit, 0, 1)
        grid.addWidget(self.x_status, 0, 2)
        grid.addWidget(QLabel("Y:"), 1, 0)
        grid.addWidget(self.y_edit, 1, 1)
        grid.addWidget(self.y_status, 1, 2)

        # --- controls row
        controls = QHBoxLayout()
        for label, token in (
            ("+", " + "), ("−", " - "), ("×", " * "), ("÷", " / "),
            ("(", "("), (")", ")"), ("xʸ", "**"),
        ):
            btn = QPushButton(label)
            btn.setFixedWidth(38)
            btn.clicked.connect(lambda _, t=token: self._insert_token(t))
            controls.addWidget(btn)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Colour by:"))
        self.group_combo = QComboBox()
        self.group_combo.addItem("(none)", "")
        for var_id in GROUP_VAR_IDS:
            var = self.catalog.get(var_id)
            self.group_combo.addItem(
                f"{var.label} [{var.unit}]".replace(" []", ""), var_id
            )
        self.group_combo.currentIndexChanged.connect(self.replot)
        controls.addWidget(self.group_combo)
        controls.addSpacing(12)
        self.live_check = QCheckBox("Include current measurement (○)")
        self.live_check.toggled.connect(self.replot)
        self.err_check = QCheckBox("Error bars")
        self.err_check.setChecked(True)
        self.err_check.toggled.connect(self.replot)
        self.log_x_check = QCheckBox("log X")
        self.log_x_check.toggled.connect(self.on_log_toggled)
        self.log_y_check = QCheckBox("log Y")
        self.log_y_check.toggled.connect(self.on_log_toggled)
        for w in (self.live_check, self.err_check, self.log_x_check, self.log_y_check):
            controls.addWidget(w)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self.on_preset_selected)
        controls.addWidget(self.preset_combo, 1)
        save_preset = QPushButton("Save…")
        save_preset.clicked.connect(self.on_save_preset)
        del_preset = QPushButton("Delete")
        del_preset.clicked.connect(self.on_delete_preset)
        controls.addWidget(save_preset)
        controls.addWidget(del_preset)
        self.export_btn = QPushButton("Export CSV…")
        self.export_btn.clicked.connect(self.on_export_csv)
        controls.addWidget(self.export_btn)

        wrap = QWidget()
        wrap.setLayout(controls)
        grid.addWidget(wrap, 2, 0, 1, 3)
        return box

    # --- variable tree -------------------------------------------------------

    def _apply_variable_filter(self):
        """Show only the selected unit variant of the result variables
        (channels/counts or charge/pC) plus everything without a unit
        counterpart; then apply the search box text."""
        pc_mode = self.units_combo.currentIndex() == UNITS_PC
        to_pc, to_counts = self._unit_translation()
        search = self.var_search.text().strip().lower()

        hidden_by_units = set()
        for var in self.catalog:
            if not var.channel:
                continue
            if pc_mode:
                # counts variables that have a charge sibling are hidden;
                # the charge variants and unit-less quantities stay
                if var.var_id in to_pc:
                    hidden_by_units.add(var.var_id)
            else:
                # channels mode: hide the charge variants
                if var.var_id in to_counts:
                    hidden_by_units.add(var.var_id)

        for i in range(self.variable_tree.topLevelItemCount()):
            cat = self.variable_tree.topLevelItem(i)
            any_visible = False
            for j in range(cat.childCount()):
                child = cat.child(j)
                var_id = child.data(0, Qt.ItemDataRole.UserRole)
                hidden = var_id in hidden_by_units
                if not hidden and search:
                    hidden = not (
                        search in var_id.lower() or search in child.text(0).lower()
                    )
                child.setHidden(hidden)
                any_visible = any_visible or not hidden
            cat.setHidden(not any_visible)
            cat.setExpanded(True)

    def on_variable_search(self, _text):
        self._apply_variable_filter()

    # --- table & filters ------------------------------------------------------

    def _remember_filter_selection(self):
        self._saved_filters = {
            var_id: combo.currentData() for var_id, combo in self.filter_combos.items()
        }

    def _populate_filters(self):
        for var_id, combo in self.filter_combos.items():
            previous = None
            if hasattr(self, "_saved_filters"):
                previous = self._saved_filters.get(var_id)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All", None)
            for value in distinct_values(self.records, var_id):
                if value == "—":
                    combo.addItem("missing", value)
                else:
                    combo.addItem(value, value)
            if previous is not None:
                index = combo.findData(previous)
                combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def on_filters_changed(self, *_args):
        self._refresh_table()
        self.replot()

    def _refresh_table(self):
        self._updating_table = True
        try:
            self._populate_table_rows()
        finally:
            self._updating_table = False

    def _populate_table_rows(self):
        self.table.setRowCount(0)
        active_filters = {
            var_id: combo.currentData()
            for var_id, combo in self.filter_combos.items()
            if combo.currentIndex() > 0
        }
        visible_ids = set()
        for record in apply_filters(self.records, active_filters):
            if self.exclude_problematic_check.isChecked() and record.is_problematic:
                continue
            visible_ids.add(id(record))

        for record in self.records:
            row = self.table.rowCount()
            self.table.insertRow(row)

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(
                Qt.CheckState.Checked if record.selected else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, check)

            time_str = record.timestamp_iso.replace("T", " ")[5:16] if record.timestamp_iso else record.filename[:16]
            values = [time_str]
            for var_id in ("depth_cm", "int_time_ms", "beam_current_nA", "beam_energy_MeV"):
                pair = record.values.get(var_id)
                values.append(format_value(pair[0]) if pair else "—")
            for col, text in enumerate(values, start=1):
                self.table.setItem(row, col, QTableWidgetItem(text))

            flag = QTableWidgetItem("⚠" if record.is_problematic else "")
            flag.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, len(values) + 1, flag)

            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, id(record))
            self.table.setRowHidden(row, id(record) not in visible_ids)

        self.status_lbl.setText(
            f"{len(self.records)} measurements in workspace · {len(visible_ids)} shown"
        )

    def _record_for_row(self, row):
        item = self.table.item(row, 0)
        if item is None:
            return None
        rid = item.data(Qt.ItemDataRole.UserRole)
        return next((r for r in self.records if id(r) == rid), None)

    def on_table_item_changed(self, item):
        if getattr(self, "_updating_table", False) or item.column() != 0:
            return
        record = self._record_for_row(item.row())
        if record is not None:
            record.selected = item.checkState() == Qt.CheckState.Checked
            self.replot()

    def _select_visible(self, selected):
        self._updating_table = True
        try:
            for row in range(self.table.rowCount()):
                if self.table.isRowHidden(row):
                    continue
                record = self._record_for_row(row)
                if record is not None:
                    record.selected = selected
                    self.table.item(row, 0).setCheckState(
                        Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked
                    )
        finally:
            self._updating_table = False
        self.replot()

    # --- expression fields ------------------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn and isinstance(
            obj, (QLineEdit, QPlainTextEdit)
        ):
            if obj in (self.x_edit, self.y_edit):
                self._last_expr_field = obj
        return super().eventFilter(obj, event)

    def _insert_token(self, token):
        field = self._last_expr_field or self.x_edit
        self._insert_into_field(field, token)
        field.setFocus()

    @staticmethod
    def _insert_into_field(field, text):
        if isinstance(field, QPlainTextEdit):
            field.insertPlainText(text)
        else:
            field.insert(text)

    def on_variable_activated(self, item, column):
        var_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not var_id:
            return  # category header
        field = self._last_expr_field or self.x_edit
        self._insert_into_field(field, var_id)
        field.setFocus()

    def _active_y_exprs(self):
        return [
            line.strip()
            for line in self.y_edit.toPlainText().splitlines()
            if line.strip()
        ]

    def _schedule_validation(self, *_args):
        self._validate_fields()
        self._replot_timer.start()

    def _validate_fields(self):
        known = {v.var_id for v in self.catalog}

        if not self.x_edit.text().strip():
            self.x_status.setText("empty")
            self.x_status.setStyleSheet("color: #999;")
        else:
            problems = validate(self.x_edit.text(), known)
            if problems:
                self.x_status.setText("⚠ " + "; ".join(problems))
                self.x_status.setStyleSheet("color: #c0392b;")
            else:
                self.x_status.setText("✓")
                self.x_status.setStyleSheet("color: #2e7d32;")

        problems = []
        for i, expr in enumerate(self._active_y_exprs(), start=1):
            for p in validate(expr, known):
                problems.append(f"line {i}: {p}")
        if problems:
            self.y_status.setText("⚠ " + "; ".join(problems))
            self.y_status.setStyleSheet("color: #c0392b;")
        elif self._active_y_exprs():
            self.y_status.setText("✓")
            self.y_status.setStyleSheet("color: #2e7d32;")
        else:
            self.y_status.setText("empty")
            self.y_status.setStyleSheet("color: #999;")

    # --- evaluation ---------------------------------------------------------------

    def replot(self, *_args):
        self._validate_fields()
        self._rebuild_live_record()
        self._last_result = None

        records = list(self._active_records())
        if self.live_check.isChecked():
            self.live_record.selected = True
            records.append(self.live_record)

        group_var = self.group_combo.currentData() or None
        x_text = self.x_edit.text().strip()
        y_exprs = self._active_y_exprs()
        multi_y = len(y_exprs) > 1
        self.group_combo.setEnabled(not multi_y)
        self.plot.clear()
        self.legend.clear()

        base = f"{len(self.records)} measurements in workspace"
        if not records:
            self.status_lbl.setText(base + " · none selected")
            return
        if not x_text or not y_exprs:
            self.status_lbl.setText(
                base + f" · {len(records)} selected — enter both X and Y expressions"
            )
            return

        try:
            result = run_pipeline(records, x_text, y_exprs, group_var_id=group_var)
        except FormulaError as exc:
            self.status_lbl.setText(f"Invalid formula: {exc}")
            return
        self._last_result = result

        # colours: by group label (single Y) or by Y expression (multi-Y)
        if multi_y:
            keys = sorted({s.y_label for s in result.series})
            colors = {key: PALETTE[i % len(PALETTE)] for i, key in enumerate(keys)}
        else:
            group_labels = sorted({s.label for s in result.series})
            colors = {label: PALETTE[i % len(PALETTE)] for i, label in enumerate(group_labels)}

        # few points deserve bigger markers
        symbol_size = 12 if result.n_plotted <= 5 else 8

        for series in result.series:
            color = colors[series.y_label if multi_y else series.label]
            if series.is_live:
                scatter = pg.ScatterPlotItem(
                    x=series.x, y=series.y,
                    symbol="o", size=symbol_size + 5, brush=None,
                    pen=pg.mkPen(color, width=2),
                )
                self.plot.addItem(scatter)
            else:
                scatter = pg.ScatterPlotItem(
                    x=series.x, y=series.y,
                    symbol="o", size=symbol_size, brush=QBrush(QColor(color)),
                    pen=pg.mkPen(color, width=1),
                )
                self.plot.addItem(scatter)
                legend_label = series.y_label if multi_y else series.label
                self.legend.addItem(scatter, legend_label)

            if self.err_check.isChecked():
                err = pg.ErrorBarItem(
                    x=series.x, y=series.y,
                    top=series.y_unc, bottom=series.y_unc,
                    left=series.x_unc, right=series.x_unc,
                    pen=pg.mkPen(0.6),
                )
                self.plot.addItem(err)

        self.plot.setLabel("bottom", self._axis_title(x_text))
        self.plot.setLabel("left", self._axis_title(y_exprs[0]) if not multi_y else "")
        self.plot.enableAutoRange()

        parts = [
            base,
            f"{result.n_selected} selected",
            f"{result.n_plotted} plotted",
        ]
        if multi_y:
            parts.append(f"{len(y_exprs)} Y expressions")
        if result.n_excluded:
            parts.append(
                f"{result.n_excluded} excluded (missing value: "
                + ", ".join(result.excluded_files[:4])
                + ("…" if len(result.excluded_files) > 4 else "")
                + ")"
            )
        if len(colors) > len(PALETTE):
            parts.append("too many groups — colours repeat")
        self.status_lbl.setText(" · ".join(parts))
        self.status_lbl.setStyleSheet("color: #555;")
        if result.n_selected > 0 and result.n_plotted == 0:
            self.status_lbl.setStyleSheet("color: #c0392b;")
            self.status_lbl.setText(
                "⚠ NOTHING PLOTTED — every selected point has a missing "
                "value (a result not computed in the archive, e.g. N = 0, "
                "or a missing metadata field). " + " · ".join(parts)
            )
        self.warning_lbl.setText("\n".join(result.warnings))

    def _axis_title(self, expr):
        var = self.catalog.get(expr)
        if var is not None:
            return f"{var.label} [{var.unit}]" if var.unit else var.label
        return expr

    # --- presets ------------------------------------------------------------------

    def _populate_presets(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in DEFAULT_PRESETS + self.user_presets:
            self.preset_combo.addItem(preset["name"], preset)
        # nothing applied yet: leaving the combo unselected makes picking
        # the first preset an actual change (so it fills the fields)
        self.preset_combo.setCurrentIndex(-1)
        self.preset_combo.blockSignals(False)

    def on_preset_selected(self, index):
        preset = self.preset_combo.itemData(index)
        if not preset:
            return
        self.x_edit.setText(preset.get("x", ""))
        self.y_edit.setPlainText(preset.get("y", ""))
        group_id = preset.get("group", "")
        pos = self.group_combo.findData(group_id)
        self.group_combo.setCurrentIndex(pos if pos >= 0 else 0)
        self.replot()

    def _presets_path(self):
        return os.path.join(self.workspace_dir, PRESETS_FILENAME) if self.workspace_dir else None

    def _load_user_presets(self):
        self.user_presets = []
        path = self._presets_path()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.user_presets = [
                        p for p in data if isinstance(p, dict) and "x" in p and "y" in p
                    ]
            except (OSError, json.JSONDecodeError):
                pass
        self._populate_presets()

    def on_save_preset(self):
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:", text=self._current_preset_name()
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        preset = {
            "name": name,
            "x": self.x_edit.text().strip(),
            "y": self.y_edit.toPlainText().strip(),
            "group": self.group_combo.currentData() or "",
        }
        self.user_presets = [p for p in self.user_presets if p["name"] != name]
        self.user_presets.append(preset)
        self._write_user_presets()
        self._populate_presets()
        self.preset_combo.setCurrentIndex(self.preset_combo.findText(name))

    def on_delete_preset(self):
        index = self.preset_combo.currentIndex()
        if 0 <= index < len(DEFAULT_PRESETS):
            self.status_lbl.setText("Built-in presets cannot be deleted.")
            return
        preset = self.preset_combo.itemData(index)
        if preset:
            self.user_presets = [p for p in self.user_presets if p["name"] != preset["name"]]
            self._write_user_presets()
            self._populate_presets()

    def _current_preset_name(self):
        index = self.preset_combo.currentIndex()
        preset = self.preset_combo.itemData(index)
        return preset["name"] if preset else ""

    def _write_user_presets(self):
        path = self._presets_path()
        if not path:
            self.status_lbl.setText("No workspace: preset kept for this session only.")
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.user_presets, f, indent=4)
        except OSError as exc:
            self.status_lbl.setText(f"Could not write presets: {exc}")

    # --- workspace & export --------------------------------------------------------

    def on_choose_workspace(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Workspace setup.json", "", "Setup Files (*.json)"
        )
        if file_path:
            self.workspace_dir = os.path.dirname(file_path)
            self._load_user_presets()
            self.rescan(refresh_filters=True)

    def on_refresh(self):
        self.rescan(refresh_filters=True)

    def on_export_csv(self):
        if not getattr(self, "_last_result", None):
            self.status_lbl.setText("Nothing to export yet.")
            return
        default_name = os.path.join(
            self.workspace_dir or os.path.expanduser("~"), "multi_analysis.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Analysis Table", default_name, "CSV Files (*.csv)"
        )
        if not path:
            return
        result = self._last_result
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["group", "y_expression", "is_live", "timestamp", "file", "x", "x_unc", "y", "y_unc"]
                )
                for series in result.series:
                    for i in range(len(series.x)):
                        writer.writerow(
                            [
                                series.label,
                                series.y_label,
                                int(series.is_live),
                                series.timestamps[i],
                                series.filenames[i],
                                series.x[i],
                                series.x_unc[i],
                                series.y[i],
                                series.y_unc[i],
                            ]
                        )
        except OSError as exc:
            self.status_lbl.setText(f"Export failed: {exc}")
            return
        self.status_lbl.setText(f"Exported to {path}")

    # --- misc ------------------------------------------------------------------------

    def on_log_toggled(self, *_args):
        self.plot.setLogMode(x=self.log_x_check.isChecked(), y=self.log_y_check.isChecked())
