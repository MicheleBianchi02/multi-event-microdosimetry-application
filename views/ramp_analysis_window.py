"""
Standalone ramp-sampling analysis window (`app.py --ramp [path]`).

The offline reader of the AQR1 ramp files a measurement published: browse
single integration windows (overlay any selection on one graph, in
acquisition order), their derivatives (consecutive difference or moving
least-squares slope), and the ensemble-averaged power spectrum — the FFT of
every valid window, averaged, with the across-window standard deviation.

Deliberately outside the measurement UI (like the ``--oscilloscope`` tool):
it only reads files.  The data layer lives in services/ramp_analysis.py
(gap-aware window mapping, PSD averaging, derivatives); this window is the
thin PyQt6/pyqtgraph front end.
"""

import threading

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.ramp_analysis import (
    RampDataset,
    derivative,
    derivative_grid,
    ramp_folder_from_zip,
    ramp_measurements_in_workspace,
)

PAGE_ROWS = 500          # windows per page of the selection table
SELECT_SOFT_CAP = 32     # above this the count label warns (selection works)
REPAINT_DELAY_MS = 250   # selection-change debounce
DERIV_DEFAULT_N = 11

PALETTE = [
    "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
    "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f",
]
BAND_BRUSH = pg.mkBrush(128, 128, 128, 40)
BAND_PEN = pg.mkPen(128, 128, 128, 90, width=1)


def _band_item(x, mean, std, brush, pen):
    """Filled ±σ band as ONE self-contained curve item (a closed polygon).

    A FillBetweenItem built over raw child PlotDataItems reports a
    garbage ~±0.5 bounding rect until its children have been painted —
    the viewbox autoranges to it and the plot "resizes to a strange
    value" (live finding).  A polygon item has real data bounds at
    once."""
    xs = np.concatenate([x, x[::-1], x[:1]])
    ys = np.concatenate([mean + std, (mean - std)[::-1], (mean + std)[:1]])
    return pg.PlotCurveItem(x=xs, y=ys, brush=brush, pen=pen)


class _AverageSignals(QObject):
    """Queued signals from the background ensemble pass to the GUI thread."""

    progress = pyqtSignal(int, int)  # done windows, total windows
    done = pyqtSignal()


class RampAnalysisWindow(QMainWindow):
    """Browse and analyze the ramp files of one measurement."""

    def __init__(self, path=None):
        super().__init__()
        self.setWindowTitle("ATOMIQA — ramp sampling analysis")
        self.resize(1300, 800)

        self._dataset = None            # RampDataset | None
        self._channel = None            # active RampChannel | None
        self._measurements = []         # workspace scan results
        self._valid = set()             # available window ids (fast membership)
        self._selected = set()
        self._page = 0
        self._table_guard = False       # suppress itemChanged while rebuilding
        self._deriv_cache = {}          # (mode, n) -> (x, mean, std)
        self._spec_ranged_for = None    # log-x state last auto-ranged
        self._spec_cache = {}           # m -> (freqs, ms_mean, count)
        self._ramp_cache = None         # (freqs, ms_mean, count) of the raw ramps
        self._spec_gen = 0              # generation of the newest pass request
        self._spec_thread = None        # background lag-m spectrum pass
        self._spec_result = None        # published by the worker
        self._spec_signals = _AverageSignals()
        self._spec_signals.progress.connect(self._on_spec_progress)
        self._spec_signals.done.connect(self._on_spec_done)
        self._avg_thread = None         # background ensemble pass (or None)
        self._avg_result = None         # (channel, key, band) published by it
        self._avg_signals = _AverageSignals()
        self._avg_signals.progress.connect(self._on_avg_progress)
        self._avg_signals.done.connect(self._on_avg_done)

        self._build_ui()

        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(REPAINT_DELAY_MS)
        self._repaint_timer.timeout.connect(self._replot)

        # debounce for the lag-m spinbox: each arrow click restarts it,
        # only a settled m launches the background spectrum pass
        self._spec_timer = QTimer(self)
        self._spec_timer.setSingleShot(True)
        self._spec_timer.setInterval(400)
        self._spec_timer.timeout.connect(self._spec_start)

        if path:
            self.open_path(path)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        pg.setConfigOptions(antialias=False)

        # ------------------------------------------------------ open bar
        ws_btn = QPushButton("Open workspace…")
        ws_btn.clicked.connect(self._on_open_workspace)
        zip_btn = QPushButton("Open measurement ZIP…")
        zip_btn.clicked.connect(self._on_open_zip)
        folder_btn = QPushButton("Open ramp folder…")
        folder_btn.clicked.connect(self._on_open_folder)

        self.measurement_combo = QComboBox()
        self.measurement_combo.setMinimumWidth(320)
        self.measurement_combo.currentIndexChanged.connect(self._on_measurement_picked)
        self.channel_combo = QComboBox()
        self.channel_combo.currentIndexChanged.connect(self._on_channel_picked)

        # The ensemble passes (per-sample mean/std band, derivative band)
        # read EVERY window — on big files they take long, so they never
        # run on load: this button starts them in a background thread
        # with the progress bar beside it.
        self.avg_btn = QPushButton("Average")
        self.avg_btn.setToolTip(
            "Compute the ensemble statistics and the derivative band over\n"
            "ALL windows in the background (progress bar).\n"
            "Loading and browsing single ramps needs no averaging.")
        self.avg_btn.clicked.connect(self._on_average_clicked)
        self.avg_progress = QProgressBar()
        self.avg_progress.setMaximumWidth(160)
        self.avg_progress.hide()

        bar = QHBoxLayout()
        for w in (ws_btn, zip_btn, folder_btn,
                  QLabel("Measurement:"), self.measurement_combo,
                  QLabel("Channel:"), self.channel_combo,
                  self.avg_btn, self.avg_progress):
            bar.addWidget(w)
        bar.addStretch(1)

        # ---------------------------------------------- selection panel
        self.page_label = QLabel("—")
        prev_btn = QPushButton("◀")
        prev_btn.setFixedWidth(32)
        prev_btn.clicked.connect(lambda: self._turn_page(-1))
        next_btn = QPushButton("▶")
        next_btn.setFixedWidth(32)
        next_btn.clicked.connect(lambda: self._turn_page(+1))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", "ramp #", "railed"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("e.g. 0, 5, 10-20")
        self.range_edit.returnPressed.connect(self._on_select_ranges)
        select_btn = QPushButton("Select")
        select_btn.clicked.connect(self._on_select_ranges)

        self.stride_spin = QSpinBox()
        self.stride_spin.setRange(1, 1_000_000)
        self.stride_spin.setValue(100)
        stride_btn = QPushButton("every k-th")
        stride_btn.clicked.connect(self._on_select_stride)
        self.random_spin = QSpinBox()
        self.random_spin.setRange(1, 100_000)
        self.random_spin.setValue(16)
        random_btn = QPushButton("random")
        random_btn.clicked.connect(self._on_select_random)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear_selection)

        tools = QHBoxLayout()
        tools.addWidget(self.stride_spin)
        tools.addWidget(stride_btn)
        tools.addWidget(self.random_spin)
        tools.addWidget(random_btn)
        tools.addStretch(1)
        tools.addWidget(clear_btn)

        self.selected_label = QLabel("no windows selected")

        panel = QVBoxLayout()
        page_row = QHBoxLayout()
        page_row.addWidget(prev_btn)
        page_row.addWidget(self.page_label, alignment=Qt.AlignmentFlag.AlignCenter)
        page_row.addWidget(next_btn)
        page_row.addStretch(1)
        panel.addLayout(page_row)
        panel.addWidget(self.table)
        range_row = QHBoxLayout()
        range_row.addWidget(self.range_edit, stretch=1)
        range_row.addWidget(select_btn)
        panel.addLayout(range_row)
        panel.addLayout(tools)
        panel.addWidget(self.selected_label)

        panel_box = QGroupBox("Ramp windows (acquisition index)")
        panel_box.setLayout(panel)
        panel_box.setMinimumWidth(300)

        # ------------------------------------------------------ ramp tab
        self.ramp_plot = self._make_plot("time in window", "s", "voltage", "V")
        self.band_check = QCheckBox("mean ± 1σ of all windows")
        self.band_check.setChecked(True)
        self.band_check.toggled.connect(self._schedule_replot)
        self.units_combo = QComboBox()
        self.units_combo.addItem("Volts")
        self.units_combo.addItem("Counts")
        self.units_combo.currentIndexChanged.connect(self._schedule_replot)
        ramp_row = QHBoxLayout()
        ramp_row.addWidget(self.band_check)
        ramp_row.addWidget(QLabel("Units:"))
        ramp_row.addWidget(self.units_combo)
        self.export_btn = QPushButton("Export mean…")
        self.export_btn.setToolTip(
            "Save the ensemble-average ramp (per-sample mean and σ) to a\n"
            "simple CSV; the header carries the applied timing — including\n"
            "the reset time — so several exports can be plotted together.")
        self.export_btn.clicked.connect(self._on_export_mean)
        ramp_row.addWidget(self.export_btn)
        ramp_row.addStretch(1)
        ramp_page = QWidget()
        ramp_layout = QVBoxLayout(ramp_page)
        ramp_layout.addWidget(self.ramp_plot)
        ramp_layout.addLayout(ramp_row)

        # ------------------------------------------------- derivative tab
        self.deriv_plot = self._make_plot("time in window", "s", "dV/dt", "V/s")
        self.deriv_mode = QComboBox()
        self.deriv_mode.addItem("consecutive difference", "diff")
        self.deriv_mode.addItem("moving linear fit", "linear")
        self.deriv_mode.addItem("two-means slope", "means")
        self.deriv_mode.setCurrentIndex(1)
        self.deriv_mode.currentIndexChanged.connect(self._on_deriv_params)
        self.deriv_n = QSpinBox()
        self.deriv_n.setRange(3, 501)
        self.deriv_n.setSingleStep(2)  # odd only: centered symmetric fit
        self.deriv_n.setValue(DERIV_DEFAULT_N)
        self.deriv_n.valueChanged.connect(self._on_deriv_params)
        self.deriv_band_check = QCheckBox("ensemble average")
        self.deriv_band_check.setChecked(True)
        self.deriv_band_check.toggled.connect(self._schedule_replot)
        self.deriv_source = QComboBox()
        self.deriv_source.addItem("of the mean (fast)", "mean")
        self.deriv_source.addItem("per-window mean ± 1σ", "band")
        self.deriv_source.setToolTip(
            "Differentiate the per-sample ensemble MEAN directly: every\n"
            "derivative mode is a linear convolution, so this is\n"
            "mathematically identical to the mean of the per-window\n"
            "derivatives — but it needs no streaming pass over the file\n"
            "(instant for any mode/width once the statistics exist).")
        self.deriv_source.currentIndexChanged.connect(self._schedule_replot)
        deriv_row = QHBoxLayout()
        deriv_row.addWidget(QLabel("Mode:"))
        deriv_row.addWidget(self.deriv_mode)
        deriv_row.addWidget(QLabel("fit points:"))
        deriv_row.addWidget(self.deriv_n)
        deriv_row.addWidget(self.deriv_band_check)
        deriv_row.addWidget(self.deriv_source)
        deriv_row.addStretch(1)
        deriv_page = QWidget()
        deriv_layout = QVBoxLayout(deriv_page)
        deriv_layout.addWidget(self.deriv_plot)
        deriv_layout.addLayout(deriv_row)

        # --------------------------------------------------- spectrum tab
        # Ensemble-averaged spectrum of the lag-m difference v[j+m] − v[j]
        # in dBV (rms per FFT line): the 2|sin(πf·m·dt)| transfer function
        # notches out the ramp's 1/f while the noise floor stays.  The
        # user picks m; Δt = m·dt is shown in the status line.  The pass
        # reads every window, so it runs in the background with its own
        # progress bar (never on load — on entering the tab or changing m).
        self.spectrum_plot = self._make_plot(
            "frequency", "Hz", "amplitude", "dBV", downsample=False
        )
        self.spectrum_plot.setLogMode(True, False)  # log frequency axis
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.25)

        self.lag_spin = QSpinBox()
        self.lag_spin.setMinimum(1)
        self.lag_spin.setValue(1)
        self.lag_spin.valueChanged.connect(self._on_lag_changed)
        self.lag_spin.setToolTip(
            "Difference non-overlapping samples m apart before the FFT:\n"
            "d_k = v[(k+1)m] − v[km], one per Δt = m·dt.  The spectrum\n"
            "lives on the Δt grid: Nyquist = 1/(2·m·dt) shrinks with m\n"
            "(the 2|sin(πf·Δt)| notch sits exactly at 2×Nyquist — never\n"
            "in band).  Frequency resolution stays ~1/T_window.")
        self.logx_check = QCheckBox("Log X")
        self.logx_check.setChecked(True)
        self.logx_check.toggled.connect(self._draw_spectrum)
        self.ramp_check = QCheckBox("with raw ramp FFT")
        self.ramp_check.setToolTip(
            "Overlay the ensemble-averaged plain FFT of the ramps\n"
            "(same dBV normalization, full band to 1/(2·dt)).\n"
            "The ratio to the difference curve at any frequency is the\n"
            "2|sin(πf·Δt)| transfer function — the visible 1/f suppression.")
        self.ramp_check.toggled.connect(self._on_ramp_check)
        self.spec_progress = QProgressBar()
        self.spec_progress.setMaximumWidth(140)
        self.spec_progress.hide()
        self.spectrum_status = QLabel("—")

        spec_row = QHBoxLayout()
        spec_row.addWidget(QLabel("m (samples apart):"))
        spec_row.addWidget(self.lag_spin)
        spec_row.addWidget(self.logx_check)
        spec_row.addWidget(self.ramp_check)
        spec_row.addWidget(self.spec_progress)
        spec_row.addStretch(1)
        spec_page = QWidget()
        spec_layout = QVBoxLayout(spec_page)
        spec_layout.addWidget(self.spectrum_plot)
        spec_layout.addLayout(spec_row)
        spec_layout.addWidget(self.spectrum_status)
        self.spectrum_plot.scene().sigMouseMoved.connect(self._on_spectrum_hover)

        # ------------------------------------------------------- assemble
        self.tabs = QTabWidget()
        self.tabs.addTab(ramp_page, "Ramps")
        self.tabs.addTab(deriv_page, "Derivative")
        self.tabs.addTab(spec_page, "Spectrum")
        # entering the Spectrum tab is the explicit user intent that
        # starts the (heavy) lag-m pass — loading alone never does
        self.tabs.currentChanged.connect(
            lambda index: self._spec_start() if index == 2 else None
        )

        splitter = QSplitter()
        splitter.addWidget(panel_box)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 980])

        self.facts_label = QLabel("Open a workspace, a measurement ZIP or a ramp folder.")
        # one line in the status bar — a word-wrapped label in the central
        # layout could grow to half the window on narrow splitters
        self.facts_label.setWordWrap(False)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addLayout(bar)
        root_layout.addWidget(splitter)
        self.setCentralWidget(root)
        self.statusBar().addPermanentWidget(self.facts_label)

    @staticmethod
    def _make_plot(x_label, x_unit, y_label, y_unit, downsample=True):
        plot = pg.PlotWidget()
        plot.setLabel("bottom", x_label, units=x_unit)
        plot.setLabel("left", y_label, units=y_unit)
        plot.showGrid(x=True, y=True, alpha=0.25)
        # downsampling is right for the time-domain tabs, WRONG for the
        # spectrum: pyqtgraph's auto mode thins in bin index — on a
        # log-frequency axis the low decades hold few bins but most of the
        # width and get crushed (same finding as the oscilloscope's page).
        plot.setDownsampling(auto=downsample)
        plot.setClipToView(True)
        plot.addLegend(offset=(10, 10))
        return plot

    # ---------------------------------------------------------- opening

    def _on_open_workspace(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the workspace folder")
        if path:
            self.open_path(path)

    def _on_open_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a measurement archive", "", "Measurement ZIP (*.zip)")
        if path:
            self.open_path(path)

    def _on_open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the ramp folder")
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        """Open a workspace folder, a measurement ZIP or a ramp folder."""
        import os

        if not os.path.exists(path):
            self._error(f"{path} does not exist")
            return
        if os.path.isfile(path):
            folder = ramp_folder_from_zip(path)
            if folder is None:
                self._error(f"{path}: the archive references no ramp folder "
                            "beside it (no ramp section, or the folder is missing)")
                return
            self._open_folder(folder)
            return
        # a folder: ramp folder when it holds bins, else a workspace.
        # Hidden files don't count: a workspace ROOT can carry crash
        # leftovers (.active_run.ramp.*.bin) without being a ramp folder.
        if any(
            not name.startswith(".") and name.endswith(".bin")
            for name in os.listdir(path)
        ):
            self._open_folder(path)
            return
        self._open_workspace(path)

    def _open_workspace(self, workspace_dir: str):
        self._measurements = ramp_measurements_in_workspace(workspace_dir)
        combo = self.measurement_combo
        combo.blockSignals(True)
        combo.clear()
        for entry in self._measurements:
            combo.addItem(entry["label"], entry["folder"])
        combo.blockSignals(False)
        if not self._measurements:
            # a status-bar whisper reads as "nothing happens": say it out loud
            QMessageBox.warning(
                self,
                "No Ramp Measurements",
                f"{workspace_dir}:\nno measurement with ramp files was found.\n\n"
                "The scans only list archives whose settings.json references an\n"
                "existing ramp folder (a 'ramp' section beside the ZIP).\n"
                "To open a single ramp folder directly, use 'Open ramp folder…'.",
            )
            return
        combo.setCurrentIndex(0)  # newest first
        self._open_folder(self._measurements[0]["folder"])

    def _on_measurement_picked(self, index):
        if index >= 0 and self.measurement_combo.count():
            self._open_folder(self.measurement_combo.itemData(index))

    def _open_folder(self, folder: str):
        try:
            self._dataset = RampDataset(folder)
        except ValueError as exc:
            self._error(str(exc))
            return
        combo = self.channel_combo
        combo.blockSignals(True)
        combo.clear()
        for label in sorted(self._dataset.channels):
            combo.addItem(label)
        combo.blockSignals(False)
        combo.setCurrentIndex(0)
        if combo.count():
            self._set_channel(combo.currentText())

    def _on_channel_picked(self, index):
        if index >= 0 and self._dataset is not None:
            self._set_channel(self.channel_combo.currentText())

    def _set_channel(self, label):
        self._channel = self._dataset.channels[label]
        self._valid = set(self._channel.window_ids.tolist())
        self._selected = set()
        self._page = 0
        self._deriv_cache.clear()
        self._spec_cache.clear()
        self._ramp_cache = None
        self._spec_gen += 1              # any running pass becomes stale
        self._spec_thread = None
        self.spec_progress.hide()
        self.lag_spin.setMaximum(max(1, self._channel.n // 9))
        self._spec_ranged_for = None
        self._refresh_table()
        self._update_selected_label()
        self._update_facts()
        self._clear_plots()
        self._replot()

    def _error(self, message):
        self.facts_label.setText(message)
        self.statusBar().showMessage(message)

    # ------------------------------------------------------------- facts

    def _update_facts(self):
        ch = self._channel
        if ch is None:
            return
        index = (self._dataset.index or {}).get("channels", {}).get(ch.label, {})
        gaps = len(index.get("gaps") or [])
        dropped = index.get("dropped_blocks", 0)
        total = ch.view.shape[0] // ch.n
        span = ch.vmax - ch.vmin
        railed = ""
        if ch.has_stats:
            railed = f" · railed samples: {ch.stats['railed_total']}"
        self.facts_label.setText(
            f"{ch.label} · N = {ch.n} samples/window · "
            f"{ch.n_windows:,} valid windows (of {total:,}) · "
            f"dt = {ch.dt * 1e6:g} µs · T_int = {ch.header['integration_time_s'] * 1e3:g} ms · "
            f"{ch.bits} bit · {ch.vmin:g}…{ch.vmax:g} V (span {span:g} V) · "
            f"gaps: {gaps} · dropped blocks: {dropped} · "
            f"{ch.state} · mode: {ch.timing_mode}{railed}"
        )

    # -------------------------------------------------- selection table

    def _refresh_table(self):
        ch = self._channel
        self._table_guard = True
        if ch is None or ch.n_windows == 0:
            self.table.setRowCount(0)
            self.page_label.setText("—")
            self._table_guard = False
            return
        pages = max(1, -(-ch.n_windows // PAGE_ROWS))
        self._page = min(self._page, pages - 1)
        start = self._page * PAGE_ROWS
        rows = min(PAGE_ROWS, ch.n_windows - start)
        self.table.setRowCount(rows)
        stats = ch.stats if ch.has_stats else None
        for row in range(rows):
            pos = start + row
            w = int(ch.window_ids[pos])

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(
                Qt.CheckState.Checked if w in self._selected else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(str(w)))
            railed = str(int(stats["railed_per_window"][pos])) if stats is not None else ""
            self.table.setItem(row, 2, QTableWidgetItem(railed))
        self.page_label.setText(f"page {self._page + 1}/{pages}")
        self._table_guard = False

    def _turn_page(self, step):
        ch = self._channel
        if ch is None or ch.n_windows == 0:
            return
        pages = max(1, -(-ch.n_windows // PAGE_ROWS))
        self._page = (self._page + step) % pages
        self._refresh_table()

    def _on_table_item_changed(self, item):
        if self._table_guard or item.column() != 0:
            return
        row = item.row()
        start = self._page * PAGE_ROWS
        if row >= self._channel.n_windows - start:
            return
        w = int(self._channel.window_ids[start + row])
        if item.checkState() == Qt.CheckState.Checked:
            self._selected.add(w)
        else:
            self._selected.discard(w)
        self._selection_changed()

    def _on_cell_double_clicked(self, row, _column):
        start = self._page * PAGE_ROWS
        ch = self._channel
        if ch is None or row >= ch.n_windows - start:
            return
        w = int(ch.window_ids[start + row])
        self._selected.symmetric_difference_update({w})
        self._refresh_table()
        self._selection_changed()

    def _on_select_ranges(self):
        ids = set()
        for part in self.range_edit.text().replace(" ", "").split(","):
            if not part:
                continue
            try:
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    ids.update(range(int(lo), int(hi) + 1))
                else:
                    ids.add(int(part))
            except ValueError:
                self.selected_label.setText(f"cannot parse {part!r} — use 0, 5, 10-20")
                return
        self._selected |= ids & self._valid
        self._refresh_table()
        self._selection_changed()

    def _on_select_stride(self):
        ch = self._channel
        if ch is None:
            return
        k = self.stride_spin.value()
        self._selected |= set(ch.window_ids[::k].tolist())
        self._refresh_table()
        self._selection_changed()

    def _on_select_random(self):
        import random

        ch = self._channel
        if ch is None or ch.n_windows == 0:
            return
        m = min(self.random_spin.value(), ch.n_windows)
        ids = random.sample(ch.window_ids.tolist(), m)
        self._selected |= set(ids)
        self._refresh_table()
        self._selection_changed()

    def _on_clear_selection(self):
        self._selected.clear()
        self._refresh_table()
        self._selection_changed()

    def _selection_changed(self):
        self._update_selected_label()
        self._schedule_replot()

    def _update_selected_label(self):
        n = len(self._selected)
        text = f"{n} window(s) selected"
        if n > SELECT_SOFT_CAP:
            text += f"  —  more than {SELECT_SOFT_CAP}: the overlay gets hard to read"
            self.selected_label.setStyleSheet("color: #c0392b;")
        else:
            self.selected_label.setStyleSheet("")
        self.selected_label.setText(text)

    def _schedule_replot(self):
        if self._channel is not None:
            self._repaint_timer.start()

    # ------------------------------------------------------------ plots

    def _clear_plots(self):
        for plot in (self.ramp_plot, self.deriv_plot, self.spectrum_plot):
            plot.clear()
        self.spectrum_status.setText("—")

    def _replot(self):
        if self._channel is None:
            return
        self._draw_ramps()
        self._draw_derivative()
        self._draw_spectrum()

    # -- ramps ------------------------------------------------------------

    def _draw_ramps(self):
        plot = self.ramp_plot
        plot.clear()
        ch = self._channel
        x = np.arange(ch.n) * ch.dt
        in_volts = self.units_combo.currentIndex() == 0
        plot.setLabel("left", "voltage" if in_volts else "counts",
                      units="V" if in_volts else None)

        # The ensemble band never auto-computes: only after the Average
        # button has run the whole-file pass (ch.has_stats stays False on
        # load, so opening a huge file is instant).
        if self.band_check.isChecked() and ch.n_windows and ch.has_stats:
            stats = ch.stats
            if in_volts:
                mean, std = stats["mean"], stats["std"]
            else:  # counts band: rescale the volts band through the header range
                k = (2**ch.bits - 1) / (ch.vmax - ch.vmin)
                mean = (stats["mean"] - ch.vmin) * k
                std = stats["std"] * k
            plot.addItem(_band_item(x, mean, std, BAND_BRUSH, BAND_PEN))
            plot.plot(x, mean, pen=pg.mkPen("#444444", width=1, style=Qt.PenStyle.DashLine))

        for i, w in enumerate(sorted(self._selected)):
            y = ch.window(w) if in_volts else ch.window_counts(w)
            plot.plot(
                x, y,
                pen=pg.mkPen(PALETTE[i % len(PALETTE)], width=1),
                name=f"#{w}",
            )

    # -- derivative -------------------------------------------------------

    def _on_deriv_params(self):
        # typing an even n directly bypasses the spinbox's odd-only step:
        # both windowed modes (fit, two-means) need an odd window — snap it
        if (self.deriv_mode.currentData() in ("linear", "means")
                and self.deriv_n.value() % 2 == 0):
            self.deriv_n.setValue(self.deriv_n.value() + 1)
            return  # the re-triggered call continues below
        # a parameter change invalidates the cached ensemble band
        self._deriv_cache.clear()
        self._schedule_replot()

    def _derivative_band(self, mode: str, n: int, progress=None):
        """Mean ± std of the derivative across ALL valid windows (streamed).

        For 'diff' this equals the derivative of the ensemble mean/std;
        for the moving fit it is computed exactly.  ``progress(done,
        total)`` is called per batch for the background worker.  The
        caller caches the result — never computed inline while drawing.
        """
        ch = self._channel
        sum_d = None
        sum_dq = None
        count = 0
        total = ch.n_windows
        for _, volts in ch.iter_batches():
            d = derivative(volts, ch.dt, mode=mode, n=n)
            if d.shape[-1] == 0:
                continue
            if sum_d is None:
                sum_d = np.zeros(d.shape[1])
                sum_dq = np.zeros(d.shape[1])
            sum_d += d.sum(axis=0)
            sum_dq += (d**2).sum(axis=0)
            count += d.shape[0]
            if progress is not None:
                progress(count, total)
        if count == 0:
            return (None, None, None)
        mean = sum_d / count
        var = np.zeros_like(mean) if count < 2 else (sum_dq - count * mean**2) / (count - 1)
        x = derivative_grid(ch.n, ch.dt, mode, n)
        return (x, mean, np.sqrt(np.maximum(var, 0.0)))

    def _draw_derivative(self):
        plot = self.deriv_plot
        plot.clear()
        ch = self._channel
        mode = self.deriv_mode.currentData()
        n = self.deriv_n.value()

        # the ensemble curve only exists after the Average button ran —
        # never computed inline (whole-file pass)
        if self.deriv_band_check.isChecked() and ch.n_windows:
            if self.deriv_source.currentData() == "mean":
                # derivative OF the per-sample mean: linear modes make
                # this identical to the mean of the per-window derivative
                # (the band's central line) at a fraction of the cost
                if ch.has_stats:
                    d_mean = derivative(ch.stats["mean"][None, :], ch.dt,
                                        mode=mode, n=n)[0]
                    if d_mean.size:
                        x = derivative_grid(ch.n, ch.dt, mode, n)
                        plot.plot(x, d_mean,
                                  pen=pg.mkPen("#444444", width=1,
                                               style=Qt.PenStyle.DashLine))
            else:
                cached = self._deriv_cache.get((mode, n))
                if cached is not None and cached[0] is not None:
                    x, mean, std = cached
                    plot.addItem(_band_item(x, mean, std, BAND_BRUSH, BAND_PEN))
                    plot.plot(x, mean, pen=pg.mkPen("#444444", width=1,
                                                    style=Qt.PenStyle.DashLine))

        for i, w in enumerate(sorted(self._selected)):
            d = derivative(ch.window(w)[None, :], ch.dt, mode=mode, n=n)[0]
            if d.shape[0] == 0:
                continue
            x = derivative_grid(ch.n, ch.dt, mode, n)
            plot.plot(
                x, d,
                pen=pg.mkPen(PALETTE[i % len(PALETTE)], width=1),
                name=f"#{w}",
            )

    # -- spectrum (lag-m difference, ensemble-averaged) --------------------

    def _on_lag_changed(self):
        self._spec_timer.start()  # debounce: only a settled m launches a pass

    def _on_ramp_check(self, _checked):
        # enabling the overlay may need its own whole-file pass
        self._spec_start()

    def _spec_start(self):
        """Launch the background passes for whatever is missing (the
        lag-m spectrum and, when its overlay is on, the raw-ramp FFT) —
        or just draw the cached results.  Nothing heavy ever runs on
        load: only on entering the tab or settling a new m/overlay."""
        ch = self._channel
        if ch is None or ch.n_windows == 0:
            return
        if self.tabs.currentIndex() != 2:
            return  # the tab entry handler starts it
        m = self.lag_spin.value()
        need_lag = m not in self._spec_cache
        need_ramp = self.ramp_check.isChecked() and self._ramp_cache is None
        if not need_lag and not need_ramp:
            self._draw_spectrum()
            return
        self._spec_gen += 1
        gen = self._spec_gen
        signals = self._spec_signals
        n = ch.n_windows
        total = n * (int(need_lag) + int(need_ramp))
        self.spec_progress.setRange(0, total)
        self.spec_progress.setValue(0)
        self.spec_progress.show()
        self.spectrum_status.setText(
            f"computing ({'difference' if need_lag else ''}"
            f"{' + ' if need_lag and need_ramp else ''}"
            f"{'ramp FFT' if need_ramp else ''})…")

        def prog(offset):
            return lambda d, _t: signals.progress.emit(offset + d, total)

        def work():
            # try/finally guarantees `done` is ALWAYS emitted — an
            # exception must not strand the progress bar (same rule as
            # the Average pass)
            error = ""
            lag_res = ramp_res = None
            offset = 0
            try:
                if need_lag:
                    lag_res = ch.lag_difference_spectrum(m, progress=prog(0))
                    offset = lag_res[2] or 0
                if need_ramp:
                    ramp_res = ch.ramp_spectrum_averaged(
                        progress=prog(offset))
            except Exception as exc:  # noqa: BLE001 — surfaced via the signal
                error = str(exc)
            self._spec_result = (gen, ch, m, lag_res, ramp_res, error)
            signals.done.emit()

        self._spec_thread = threading.Thread(
            target=work, daemon=True, name="ramp-spectrum")
        self._spec_thread.start()

    def _on_spec_progress(self, done: int, total: int):
        self.spec_progress.setRange(0, total)
        self.spec_progress.setValue(min(done, total))

    def _on_spec_done(self):
        result, self._spec_result = self._spec_result, None
        if result is None:
            return
        gen, ch, m, lag_res, ramp_res, error = result
        if gen != self._spec_gen:
            return  # superseded (channel, m or overlay changed): drop silently
        self._spec_thread = None
        self.spec_progress.hide()
        if error:
            self.spectrum_status.setText(f"spectrum failed: {error}")
            return
        if lag_res is not None and lag_res[0] is not None:
            self._spec_cache[m] = lag_res
        if ramp_res is not None and ramp_res[0] is not None:
            self._ramp_cache = ramp_res
        self._draw_spectrum()

    def _draw_spectrum(self):
        plot = self.spectrum_plot
        plot.clear()
        ch = self._channel
        if ch is None or ch.n_windows == 0:
            self.spectrum_status.setText("—")
            return

        spans = []  # (f0, f1) of every drawn curve, for the re-range
        m = self.lag_spin.value()
        cached = self._spec_cache.get(m)
        if cached is not None:
            freqs, ms, count = cached
            db = 10.0 * np.log10(np.maximum(ms, 1e-30))
            plot.plot(freqs, db, pen=pg.mkPen("#1f4e9c", width=2),
                      name=f"v[(k+1){m}] − v[k{m}]")
            spans.append((float(freqs[0]), float(freqs[-1])))
            n_diff = ch.n // m - 1
            dt = ch.dt
            diff_txt = (f"Δt = m·dt = {m * dt:.6g} s · "
                        f"fs = 1/Δt = {1.0 / (m * dt):.6g} Hz · "
                        f"Nyquist = 1/(2Δt) = {0.5 / (m * dt):.6g} Hz · "
                        f"{n_diff:,} samples/FFT · {count:,} windows")
        else:
            diff_txt = "no difference spectrum for this m yet"

        ramp_txt = ""
        if self.ramp_check.isChecked():
            if self._ramp_cache is not None:
                rf, rms, rcount = self._ramp_cache
                rdb = 10.0 * np.log10(np.maximum(rms, 1e-30))
                plot.plot(rf, rdb, pen=pg.mkPen("#e5a13c", width=1),
                          name="ramp FFT")
                spans.append((float(rf[0]), float(rf[-1])))
                ramp_txt = (f" | ramp FFT: Nyquist = 1/(2dt) = "
                            f"{0.5 / ch.dt:.6g} Hz · {rcount:,} windows")
            else:
                ramp_txt = " | ramp FFT pending"

        if not spans:
            self.spectrum_status.setText(
                "no spectrum yet — re-enter the tab or change m")
            return
        log_x = self.logx_check.isChecked()
        plot.setLogMode(log_x, False)
        # numeric re-range when the axis mode or the curve set changed
        # (the log axis would otherwise paint once with the stale linear
        # range and overflow its tick generator — same pyqtgraph quirk
        # as the oscilloscope)
        range_key = (log_x, tuple(spans))
        if self._spec_ranged_for != range_key:
            self._spec_ranged_for = range_key
            f0 = min(s[0] for s in spans)
            f1 = max(s[1] for s in spans)
            if log_x:
                f0, f1 = np.log10(f0), np.log10(f1)
            plot.getViewBox().setXRange(f0, f1, padding=0)

        df_txt = ""
        if cached is not None:
            df_txt = f" · df = {freqs[1] - freqs[0]:g} Hz"
        self.spectrum_status.setText(f"{diff_txt}{df_txt}{ramp_txt}")

    def _on_spectrum_hover(self, pos):
        if self._channel is None:
            return
        item = self.spectrum_plot.getPlotItem()
        if not item.sceneBoundingRect().contains(pos):
            return
        mouse = item.vb.mapSceneToView(pos)
        self.statusBar().showMessage(f"f = {mouse.x():.4g} Hz   {mouse.y():.2f} dBV")

    # -- export -----------------------------------------------------------

    def _on_export_mean(self):
        """Write the ensemble-average ramp to CSV (header: applied timing)."""
        import os

        ch = self._channel
        if ch is None or ch.n_windows == 0:
            self._error("open a measurement first")
            return
        if not ch.has_stats:
            self.statusBar().showMessage(
                "press Average first — no ensemble mean to export yet", 10000)
            return

        default = f"{os.path.basename(self._dataset.folder)}_{ch.label}_mean.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export average ramp", default, "CSV (*.csv)")
        if not path:
            return

        h = ch.header
        stats = ch.stats
        header = [
            "# ATOMIQA ramp ensemble average",
            f"# measurement: {h.get('measurement_uuid', '')}",
            f"# channel: {ch.label}",
            f"# windows_averaged: {stats['count']}",
            f"# samples_per_window: {ch.n}",
            f"# integration_time_s: {h['integration_time_s']}",
            f"# reset_time_s: {h['reset_time_s']}",
            f"# ramp_dt_s: {h['ramp_dt_s']}",
            f"# period_s: {h['period_s']}",
            f"# timing_mode: {ch.timing_mode}",
            f"# adc_bits: {h['adc_bits']}",
            f"# input_range_v: [{h['range_min_v']}, {h['range_max_v']}]",
            f"# railed_samples_total: {stats['railed_total']}",
            f"# driver: {h.get('driver_code', '')} "
            f"firmware {h.get('firmware_version', '')}",
            f"# start: {h.get('start_iso', '')}",
        ]
        t = np.arange(ch.n) * ch.dt
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(header) + "\n")
            f.write("time_s,mean_v,std_v\n")
            f.write("\n".join(
                "%.9g,%.9g,%.9g" % row
                for row in zip(t, stats["mean"], stats["std"])
            ) + "\n")
        self.statusBar().showMessage(
            f"exported {ch.n} samples ({stats['count']} windows averaged) to {path}",
            10000)

    # -- background averaging ---------------------------------------------

    def _on_average_clicked(self):
        ch = self._channel
        if ch is None or ch.n_windows == 0 or self._avg_thread is not None:
            return
        mode = self.deriv_mode.currentData()
        n = self.deriv_n.value()
        # the per-window band pass is only needed when that source is
        # selected — "of the mean" needs the statistics alone (the
        # derivative of the mean IS the mean of the derivatives)
        want_band = self.deriv_source.currentData() != "mean"
        passes = 2 if want_band else 1
        total = passes * ch.n_windows
        signals = self._avg_signals
        self.avg_btn.setEnabled(False)
        self.avg_progress.setRange(0, total)
        self.avg_progress.setValue(0)
        self.avg_progress.show()
        self.statusBar().showMessage("averaging all windows…")

        def work():
            # pass 1: per-sample mean/std + railed counts (cached on the
            # channel); pass 2 (optional): the per-window derivative band.
            # The try/finally guarantees `done` is ALWAYS emitted — an
            # exception in the thread must not leave the button disabled
            # and the progress bar stuck.
            error = None
            band = None
            try:
                ch.compute_stats(progress=lambda d, _t: signals.progress.emit(d, total))
                if want_band:
                    offset = ch.n_windows
                    band = self._derivative_band(
                        mode, n,
                        progress=lambda d, _t: signals.progress.emit(offset + d, total),
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced via the signal
                error = exc
            self._avg_result = (ch, (mode, n), band, str(error) if error else "")
            signals.done.emit()

        self._avg_thread = threading.Thread(target=work, daemon=True, name="ramp-average")
        self._avg_thread.start()

    def _on_avg_progress(self, done: int, total: int):
        self.avg_progress.setRange(0, total)
        self.avg_progress.setValue(min(done, total))

    def _on_avg_done(self):
        self._avg_thread = None
        self.avg_btn.setEnabled(True)
        self.avg_progress.hide()
        self.statusBar().clearMessage()
        result, self._avg_result = self._avg_result, None
        if result is not None:
            ch, key, band, error = result
            if error:
                self.statusBar().showMessage(f"averaging failed: {error}", 10000)
            # the user may have switched channel mid-run: keep the result
            # only for the channel it was computed on, and only bands
            # that were actually computed
            if ch is self._channel and not error and band is not None:
                self._deriv_cache[key] = band
        self._update_facts()
        self._replot()

    # ------------------------------------------------------------- utils
