"""
FFT analysis dialog for the frequency-domain variance-covariance estimator.

The user picks the spectrum options, draws a movable threshold line and any
number of exclusion bands directly on the PSD plot, then launches the
calculation. The resulting var_fft block is handed back to the caller only
when the user applies it — nothing is computed or stored automatically.
"""

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.calc_stat import calculate_fft_block, compute_spectrum

DB_EPS = 1e-18

COLOR_CH1 = "#1f77b4"
COLOR_CH2 = "#d62728"
COLOR_PEAKS = "#ff7f0e"
COLOR_THR_CH1 = "#12507b"   # dark blue — CH1 threshold line
COLOR_THR_CH2 = "#7b1f2b"   # dark red — CH2 threshold line


class FFTAnalysisDialog(QDialog):
    """Interactive window to configure and run the FFT (var_fft) estimator."""

    reset_requested = pyqtSignal()

    def __init__(
        self,
        data: np.ndarray,
        sample_freq_hz: float,
        n_channels: int = 2,
        params: dict | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("FFT Analysis — Frequency-Domain Method")
        self.resize(1100, 650)

        self._data = data
        self._fs = sample_freq_hz
        self._n_channels = n_channels
        self._band_regions: list[pg.LinearRegionItem] = []
        self._diag = {}
        self._stats = {}

        # results handed to the caller after accept()
        self.result_block: dict | None = None
        self.applied_params: dict | None = None

        params = params or {}
        self._init_ui(params)
        self._restore_params(params)
        self._refresh_plot()

    # ------------------------------------------------------------------ UI

    def _init_ui(self, params: dict):
        root = QVBoxLayout(self)
        main_row = QHBoxLayout()
        root.addLayout(main_row, stretch=1)

        # --- options column ---
        options = QWidget()
        options.setFixedWidth(280)
        opt_layout = QVBoxLayout(options)
        opt_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        spectrum_group = QGroupBox("Spectrum")
        spectrum_layout = QVBoxLayout(spectrum_group)
        self.method_box = QComboBox()
        self.method_box.addItems(["Periodogram", "Welch"])
        spectrum_layout.addWidget(QLabel("Estimator:"))
        spectrum_layout.addWidget(self.method_box)
        self.window_box = QComboBox()
        self.window_box.addItems(["Rectangular", "Hann"])
        spectrum_layout.addWidget(QLabel("Window:"))
        spectrum_layout.addWidget(self.window_box)
        self.nperseg_spin = QSpinBox()
        self.nperseg_spin.setRange(256, 1 << 20)
        self.nperseg_spin.setValue(8192)
        self.nperseg_spin.setSingleStep(256)
        spectrum_layout.addWidget(QLabel("Welch segment length:"))
        spectrum_layout.addWidget(self.nperseg_spin)
        self.method_box.currentIndexChanged.connect(self._on_method_changed)
        self.window_box.currentIndexChanged.connect(self._refresh_plot)
        self.nperseg_spin.valueChanged.connect(self._refresh_plot)
        opt_layout.addWidget(spectrum_group)

        floor_group = QGroupBox("White noise floor")
        floor_layout = QVBoxLayout(floor_group)
        self.use_threshold_check = QCheckBox("Use threshold lines (one per channel)")
        self.use_threshold_check.toggled.connect(self._on_threshold_toggled)
        floor_layout.addWidget(self.use_threshold_check)
        self.threshold_lbl = QLabel("Threshold: -")
        floor_layout.addWidget(self.threshold_lbl)
        floor_layout.addWidget(QLabel("Excluded bands:"))
        self.band_list = QListWidget()
        floor_layout.addWidget(self.band_list)
        band_row = QHBoxLayout()
        self.add_band_bnt = QPushButton("Add band")
        self.add_band_bnt.clicked.connect(self._on_add_band)
        self.remove_band_bnt = QPushButton("Remove")
        self.remove_band_bnt.clicked.connect(self._on_remove_band)
        band_row.addWidget(self.add_band_bnt)
        band_row.addWidget(self.remove_band_bnt)
        floor_layout.addLayout(band_row)
        opt_layout.addWidget(floor_group)

        view_group = QGroupBox("View")
        view_layout = QVBoxLayout(view_group)
        self.view_box = QComboBox()
        self.view_box.addItems(["PSD", "Magnitude"])
        self.view_box.currentIndexChanged.connect(self._refresh_plot)
        view_layout.addWidget(self.view_box)
        opt_layout.addWidget(view_group)

        main_row.addWidget(options)

        # --- plot + info column ---
        plot_col = QVBoxLayout()
        main_row.addLayout(plot_col, stretch=1)

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "Frequency", units="Hz")
        self.plot.addLegend()
        self._curve_ch1 = self.plot.plot(
            pen=pg.mkPen(COLOR_CH1, width=2), name="CH 1"
        )
        self._curve_ch2 = self.plot.plot(
            pen=pg.mkPen(COLOR_CH2, width=2), name="CH 2"
        )
        # PSD area above the threshold line, filled per channel so the peaks
        # stand out from the noise fuzz
        self._peak_fill_ch1 = self.plot.plot(
            pen=None, brush=pg.mkBrush(255, 127, 14, 90), fillLevel=0
        )
        self._peak_fill_ch2 = self.plot.plot(
            pen=None, brush=pg.mkBrush(255, 127, 14, 60), fillLevel=0
        )
        # markers over the *selected* peaks' bins; the cursor region follows
        # the row highlighted in the peak panel
        self._peak_sel_ch1 = self.plot.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=COLOR_THR_CH1, symbolPen=None,
        )
        self._peak_sel_ch2 = self.plot.plot(
            pen=None, symbol="o", symbolSize=6,
            symbolBrush=COLOR_THR_CH2, symbolPen=None,
        )
        self._peak_cursor = pg.LinearRegionItem(
            values=(0, 0),
            orientation="vertical",
            brush=pg.mkBrush(255, 255, 0, 40),
            movable=False,
        )
        self._peak_cursor.hide()
        self.plot.addItem(self._peak_cursor)
        self._floor_line_ch1 = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(COLOR_CH1, width=2, style=Qt.PenStyle.DashLine),
        )
        self._floor_line_ch2 = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(COLOR_CH2, width=2, style=Qt.PenStyle.DashLine),
        )
        self.plot.addItem(self._floor_line_ch1)
        self.plot.addItem(self._floor_line_ch2)
        self._threshold_line_ch1 = pg.InfiniteLine(
            angle=0,
            movable=True,
            pen=pg.mkPen(COLOR_THR_CH1, width=3),
            label="T CH1",
        )
        self._threshold_line_ch2 = pg.InfiniteLine(
            angle=0,
            movable=True,
            pen=pg.mkPen(COLOR_THR_CH2, width=3),
            label="T CH2",
        )
        for line in (self._threshold_line_ch1, self._threshold_line_ch2):
            line.sigPositionChangeFinished.connect(self._on_threshold_moved)
            self.plot.addItem(line)
            line.hide()
        plot_col.addWidget(self.plot, stretch=1)

        self.info_lbl = QLabel("")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.info_lbl.setText(
            "Draw the threshold line and the exclusion bands, then press "
            "Calculate. The 0 Hz bin displays the DC (mean) level of the "
            "measurement."
        )
        plot_col.addWidget(self.info_lbl)

        # --- peak selection panel (documentation only: the selection never
        # changes the calculation, it decides which peaks are archived) ---
        self.peak_tabs = QTabWidget()
        self.peak_tabs.setMaximumHeight(190)
        self._peak_trees = {}
        self._peak_summaries = {}
        self._peak_minvar_spins = {}
        self._peak_rows = {}
        self._populating_peaks = False
        for tab_idx in range(self._n_channels):
            tag = "CH 1" if tab_idx == 0 else "CH 2"
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(4, 4, 4, 4)
            page_layout.setSpacing(4)

            tree = QTreeWidget()
            tree.setColumnCount(5)
            tree.setHeaderLabels(
                ["", "Range [Hz]", "Center [Hz]", "Var [Ch²]", "Share [%]"]
            )
            tree.setRootIsDecorated(False)
            tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            for col in (2, 3, 4):
                tree.header().setSectionResizeMode(
                    col, QHeaderView.ResizeMode.ResizeToContents
                )
            tree.itemChanged.connect(
                lambda _item, idx=tab_idx: self._on_peak_item_changed(idx)
            )
            tree.itemSelectionChanged.connect(
                lambda idx=tab_idx: self._on_peak_row_selected(idx)
            )
            page_layout.addWidget(tree, stretch=1)

            btn_row = QHBoxLayout()
            select_all = QPushButton("Select all")
            select_all.clicked.connect(lambda _=False, i=tab_idx: self._on_peak_select(i, True))
            select_none = QPushButton("Select none")
            select_none.clicked.connect(lambda _=False, i=tab_idx: self._on_peak_select(i, False))
            btn_row.addWidget(select_all)
            btn_row.addWidget(select_none)
            btn_row.addWidget(QLabel("Min Var [Ch²]:"))
            min_var = QDoubleSpinBox()
            min_var.setDecimals(6)
            min_var.setRange(0.0, 1e9)
            min_var.setSingleStep(0.001)
            min_var.valueChanged.connect(
                lambda _v, i=tab_idx: self._apply_peak_filter(i)
            )
            btn_row.addWidget(min_var)
            btn_row.addStretch()
            page_layout.addLayout(btn_row)

            summary = QLabel("Run Calculate to detect peaks.")
            summary.setWordWrap(True)
            page_layout.addWidget(summary)

            self.peak_tabs.addTab(page, tag)
            self._peak_trees[tab_idx] = tree
            self._peak_summaries[tab_idx] = summary
            self._peak_minvar_spins[tab_idx] = min_var
        plot_col.addWidget(self.peak_tabs)

        # --- results table ---
        results_group = QGroupBox("Results")
        res_grid = QGridLayout()
        res_grid.setVerticalSpacing(6)

        ch1_header = QLabel(f"<b style='color:{COLOR_CH1};'>CH 1</b>")
        ch2_header = QLabel(f"<b style='color:{COLOR_CH2};'>CH 2</b>")
        for col, header in ((1, ch1_header), (2, ch2_header)):
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            res_grid.addWidget(header, 0, col)

        yd_html = (
            "<span style='text-decoration: overline;'>y</span><sub>D</sub> [Ch]"
        )
        self._result_rows = [
            ("N", "N", "N"),
            ("mean", "Mean [Ch]", "Mean"),
            ("white", "Var−Cov (white) [Ch²]", None),  # Variance - Covariance
            ("cov", "Covariance [Ch²]", "Covariance"),
            ("var", "Variance [Ch²]", "Variance"),
            ("yd", yd_html, "y_bar_d"),
        ]
        self._result_value_labels = {}
        for row, (key, label, _stat) in enumerate(self._result_rows, start=1):
            name_lbl = QLabel(label)
            ch1_lbl = QLabel("—")
            ch2_lbl = QLabel("—")
            for lbl in (ch1_lbl, ch2_lbl):
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setTextFormat(Qt.TextFormat.RichText)
            res_grid.addWidget(name_lbl, row, 0)
            res_grid.addWidget(ch1_lbl, row, 1)
            res_grid.addWidget(ch2_lbl, row, 2)
            self._result_value_labels[key] = (ch1_lbl, ch2_lbl)
        res_grid.setColumnStretch(0, 3)
        res_grid.setColumnStretch(1, 2)
        res_grid.setColumnStretch(2, 2)

        self.options_lbl = QLabel("")
        self.options_lbl.setWordWrap(True)
        self.options_lbl.setStyleSheet("color: palette(PlaceholderText);")
        opts_layout = QVBoxLayout()
        opts_layout.addLayout(res_grid)
        opts_layout.addWidget(self.options_lbl)
        results_group.setLayout(opts_layout)
        root.addWidget(results_group)

        # --- buttons ---
        bnt_row = QHBoxLayout()
        self.calculate_bnt = QPushButton("Calculate")
        self.calculate_bnt.clicked.connect(self._on_calculate)
        self.apply_bnt = QPushButton("Apply")
        self.apply_bnt.setEnabled(False)
        self.apply_bnt.clicked.connect(self._on_apply)
        self.reset_bnt = QPushButton("Reset to zero")
        self.reset_bnt.clicked.connect(self._on_reset)
        self.close_bnt = QPushButton("Close")
        self.close_bnt.clicked.connect(self.reject)
        bnt_row.addWidget(self.calculate_bnt)
        bnt_row.addWidget(self.apply_bnt)
        bnt_row.addWidget(self.reset_bnt)
        bnt_row.addStretch()
        bnt_row.addWidget(self.close_bnt)
        root.addLayout(bnt_row)

        self._curve_ch2.setVisible(self._n_channels > 1)
        if self._n_channels < 2:
            for key in ("N", "mean", "white", "cov", "var", "yd"):
                self._result_value_labels[key][1].hide()
            res_grid.itemAtPosition(0, 2).widget().hide()  # CH2 header
            res_grid.setColumnStretch(2, 0)

    def _restore_params(self, params: dict):
        if params.get("method") == "welch":
            self.method_box.setCurrentIndex(1)
        if params.get("window") == "hann":
            self.window_box.setCurrentIndex(1)
        if params.get("nperseg"):
            self.nperseg_spin.setValue(int(params["nperseg"]))
        if params.get("excluded_bands"):
            for f_lo, f_hi in params["excluded_bands"]:
                self._add_band_region(float(f_lo), float(f_hi))
        if params.get("threshold_ch1") is not None or params.get("threshold_ch2") is not None:
            self.use_threshold_check.setChecked(True)
            if params.get("threshold_ch1") is not None:
                db = 10 * np.log10(params["threshold_ch1"] + DB_EPS)
                self._threshold_line_ch1.setPos(db)
            if params.get("threshold_ch2") is not None:
                db = 10 * np.log10(params["threshold_ch2"] + DB_EPS)
                self._threshold_line_ch2.setPos(db)
            self._update_threshold_label()

    # ----------------------------------------------------------- helpers

    @property
    def _use_welch(self) -> bool:
        return self.method_box.currentIndex() == 1

    @property
    def _use_hann(self) -> bool:
        return self.window_box.currentIndex() == 1

    @property
    def _threshold_linear(self) -> float | None:
        return self._threshold_linear_for(0)

    def _threshold_linear_for(self, idx: int) -> float | None:
        """Threshold of one channel in linear PSD units, or None if unused."""
        if not self.use_threshold_check.isChecked():
            return None
        if idx == 1 and self._n_channels < 2:
            return None
        line = self._threshold_line_ch1 if idx == 0 else self._threshold_line_ch2
        db = float(line.value())
        return 10.0 ** (db / 10.0)

    def _band_edges(self) -> list:
        """The excluded frequency regions, as the user drew them."""
        return sorted(
            [tuple(region.getRegion()) for region in self._band_regions]
        )

    def _kept_bands(self) -> list:
        """Complement of the excluded regions within (0, f_Nyquist].

        Overlapping regions are merged first; these are the bands the white
        floor is measured on (the calc API takes kept bands).
        """
        f_nyq = self._fs / 2.0
        merged: list[list] = []
        for lo, hi in self._band_edges():
            lo, hi = max(0.0, lo), min(f_nyq, hi)
            if hi <= lo:
                continue
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        kept, prev = [], 0.0
        for lo, hi in merged:
            if lo > prev:
                kept.append((prev, lo))
            prev = max(prev, hi)
        if prev < f_nyq:
            kept.append((prev, f_nyq))
        return kept

    def _display_psd(self, channel: np.ndarray):
        """Spectrum for display: exactly what calc_stat will integrate.

        Returns (freqs, psd_db); freqs is None when not computable. The 0 Hz
        bin is replaced by the mean level (mean²) for display only — the
        calculation removes the mean, which would otherwise plot bin 0 as a
        -180 dB zero artifact.
        """
        method = "welch" if self._use_welch else "periodogram"
        window = "hann" if self._use_hann else "rect"
        freqs, psd = compute_spectrum(
            channel, self._fs, method=method, window=window,
            nperseg=self.nperseg_spin.value(),
        )
        if freqs is None:
            return None, None
        y = channel[~np.isnan(channel)]
        psd = psd.copy()
        psd[0] = float(y.mean()) ** 2 if len(y) else 0.0
        return freqs, 10 * np.log10(psd + DB_EPS)

    def _magnitude_db(self, channel: np.ndarray):
        y = channel[~np.isnan(channel)]
        values = np.abs(np.fft.rfft(y - y.mean(), norm="forward"))
        values[0] = abs(float(y.mean())) if len(y) else 0.0
        freqs = np.fft.rfftfreq(len(y), d=1.0 / self._fs)
        return freqs, 20 * np.log10(values + DB_EPS)

    # ---------------------------------------------------------- slots

    def _on_method_changed(self):
        self.nperseg_spin.setEnabled(self._use_welch)
        self._clear_overlays()
        self._refresh_plot()

    def _on_threshold_toggled(self):
        enabled = self.use_threshold_check.isChecked()
        for idx, line in (
            (0, self._threshold_line_ch1),
            (1, self._threshold_line_ch2),
        ):
            visible = enabled and (idx == 0 or self._n_channels > 1)
            line.setVisible(visible)
            if visible:
                # place each line just above its own channel's noise fuzz
                line.setPos(self._auto_threshold_db(idx))
        self._update_threshold_label()
        self._clear_overlays()

    def _on_threshold_moved(self):
        self._update_threshold_label()
        self._clear_overlays()

    def _auto_threshold_db(self, idx: int) -> float:
        """A sensible starting height for the line.

        The PSD bins of white noise are chi-squared distributed (median ~
        0.69x the level), so median + 9 dB puts the line at ~5.8x the true
        floor level: above the fuzz, with a clipped-mean bias below 1%.
        """
        _, db = self._display_psd(self._data[:, idx])
        if db is None or len(db) < 2:
            return 0.0
        return float(np.median(db[1:])) + 9.0

    def _update_threshold_label(self):
        if not self.use_threshold_check.isChecked():
            self.threshold_lbl.setText("Thresholds: not used")
            return
        db1 = float(self._threshold_line_ch1.value())
        if self._n_channels > 1:
            db2 = float(self._threshold_line_ch2.value())
            self.threshold_lbl.setText(
                f"Thresholds — CH1: {db1:.1f} dB ({10.0 ** (db1 / 10.0):.3g}"
                f" Ch²/Hz) · CH2: {db2:.1f} dB ({10.0 ** (db2 / 10.0):.3g}"
                " Ch²/Hz)"
            )
        else:
            self.threshold_lbl.setText(
                f"Threshold — CH1: {db1:.1f} dB ({10.0 ** (db1 / 10.0):.3g}"
                " Ch²/Hz)"
            )

    def _on_add_band(self):
        (f_min, f_max), _ = self.plot.viewRange()
        span = (f_max - f_min) / 5.0
        center = (f_min + f_max) / 2.0
        self._add_band_region(center - span / 2.0, center + span / 2.0)

    def _add_band_region(self, f_lo: float, f_hi: float):
        region = pg.LinearRegionItem(
            values=(f_lo, f_hi),
            orientation="vertical",
            brush=pg.mkBrush(0, 0, 0, 40),
            movable=True,
        )
        region.setBounds((0.0, self._fs / 2.0))
        region.sigRegionChangeFinished.connect(self._refresh_band_list)
        self.plot.addItem(region)
        self._band_regions.append(region)
        self._refresh_band_list()

    def _on_remove_band(self):
        row = self.band_list.currentRow()
        if 0 <= row < len(self._band_regions):
            self.plot.removeItem(self._band_regions.pop(row))
            self._refresh_band_list()

    def _refresh_band_list(self):
        self.band_list.clear()
        for f_lo, f_hi in self._band_edges():
            self.band_list.addItem(f"{f_lo:.1f} – {f_hi:.1f} Hz")

    def _refresh_plot(self):
        """Redraw the spectrum with the current estimator/window settings.

        The curves always show the spectrum the calculation would integrate,
        so switching periodogram/Welch or the window updates the plot.
        """
        self._clear_overlays(clear_peak_panel=False)
        is_psd = self.view_box.currentIndex() == 0
        self.plot.setLabel(
            "left", "PSD" if is_psd else "Magnitude", units="dB"
        )
        if self._fs is None or self._fs <= 0 or len(self._data) < 2:
            return
        self._threshold_line_ch1.setVisible(
            is_psd and self.use_threshold_check.isChecked()
        )
        self._threshold_line_ch2.setVisible(
            is_psd
            and self.use_threshold_check.isChecked()
            and self._n_channels > 1
        )
        for idx, curve in ((0, self._curve_ch1), (1, self._curve_ch2)):
            if idx >= self._n_channels:
                continue
            channel = self._data[:, idx]
            if is_psd:
                freqs, db = self._display_psd(channel)
                if freqs is None:
                    curve.setData([], [])
                else:
                    curve.setData(freqs, db)
            else:
                freqs, mag = self._magnitude_db(channel)
                curve.setData(freqs, mag)
        # the detected peaks survive a view toggle; their markers only exist
        # in the PSD view
        if is_psd:
            self._draw_selected_peaks(0)
            self._draw_selected_peaks(1)

    def _clear_overlays(self, clear_peak_panel: bool = True):
        self._peak_fill_ch1.setData([], [])
        self._peak_fill_ch2.setData([], [])
        self._peak_sel_ch1.setData([], [])
        self._peak_sel_ch2.setData([], [])
        self._peak_cursor.hide()
        self._floor_line_ch1.hide()
        self._floor_line_ch2.hide()
        if clear_peak_panel:
            self._clear_peak_panel()
        self.apply_bnt.setEnabled(False)

    def _clear_peak_panel(self):
        """Empty the peak lists (the user must recalculate to refill them)."""
        self._populating_peaks = True
        for idx, tree in self._peak_trees.items():
            tree.clear()
            self._peak_rows[idx] = []
            self._peak_summaries[idx].setText("Run Calculate to detect peaks.")
        self._populating_peaks = False

    # ------------------------------------------------- peak panel (docs only)

    def _populate_peak_panel(self):
        """Fill the trees with the detected peaks, biggest variance first.

        Everything starts selected: unchecking a peak only removes it from
        the documented/archived list — the calculation never changes.
        """
        self._populating_peaks = True
        for idx, tree in self._peak_trees.items():
            tree.clear()
            diag = self._diag.get(idx)
            # ascending frequency: the list mirrors the spectrum left to right
            peaks = sorted(
                diag["peaks"], key=lambda p: p["f_lo"]
            ) if diag is not None else []
            total_var = sum(p["variance"] for p in peaks) or 1.0
            rows = []
            for peak in peaks:
                item = QTreeWidgetItem(tree)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # nothing is archived until the user explicitly checks the
                # peaks they identify as real
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setText(1, f"{peak['f_lo']:.2f} – {peak['f_hi']:.2f}")
                item.setText(2, f"{peak['f_center']:.2f}")
                item.setText(3, f"{peak['variance']:.4g}")
                item.setText(4, f"{100.0 * peak['variance'] / total_var:.1f}")
                item.setData(0, Qt.ItemDataRole.UserRole, peak)
                rows.append(peak)
            self._peak_rows[idx] = rows
            self._apply_peak_filter(idx)
            self._update_peak_summary(idx)
        self._populating_peaks = False

    def _checked_peaks(self, idx: int) -> list:
        tree = self._peak_trees[idx]
        return [
            tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)
            for row in range(tree.topLevelItemCount())
            if not tree.topLevelItem(row).isHidden()
            and tree.topLevelItem(row).checkState(0) == Qt.CheckState.Checked
        ]

    def _on_peak_item_changed(self, idx: int):
        if self._populating_peaks:
            return
        self._update_peak_summary(idx)
        self._draw_selected_peaks(idx)

    def _on_peak_select(self, idx: int, checked: bool):
        tree = self._peak_trees[idx]
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(row)
            if not item.isHidden():
                item.setCheckState(0, state)

    def _apply_peak_filter(self, idx: int):
        """Display-only filter: hide rows below the minimum variance."""
        if self._populating_peaks:
            return
        tree = self._peak_trees[idx]
        min_var = self._peak_minvar_spins[idx].value()
        for row in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(row)
            item.setHidden(item.data(0, Qt.ItemDataRole.UserRole)["variance"] < min_var)
        self._update_peak_summary(idx)

    def _on_peak_row_selected(self, idx: int):
        tree = self._peak_trees[idx]
        items = tree.selectedItems()
        if not items:
            self._peak_cursor.hide()
            return
        peak = items[0].data(0, Qt.ItemDataRole.UserRole)
        self._peak_cursor.setRegion((peak["f_lo"], peak["f_hi"]))
        self._peak_cursor.show()

    def _update_peak_summary(self, idx: int):
        tree = self._peak_trees[idx]
        detected = tree.topLevelItemCount()
        selected = [
            tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)
            for row in range(tree.topLevelItemCount())
            if tree.topLevelItem(row).checkState(0) == Qt.CheckState.Checked
        ]
        sel_var = sum(p["variance"] for p in selected)
        all_var = sum(
            tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)["variance"]
            for row in range(detected)
        )
        self._peak_summaries[idx].setText(
            f"{detected} detected · {len(selected)} selected · "
            f"selected power {sel_var:.4g} of {all_var:.4g} Ch²"
        )

    def _draw_selected_peaks(self, idx: int):
        """Mark the selected peaks' bins on the spectrum (PSD view only)."""
        scatter = self._peak_sel_ch1 if idx == 0 else self._peak_sel_ch2
        if self.view_box.currentIndex() != 0:
            scatter.setData([], [])
            return
        diag = self._diag.get(idx)
        if diag is None or diag["threshold"] is None:
            scatter.setData([], [])
            return
        freqs, psd = diag["freqs"], diag["psd"]
        selected = self._checked_peaks(idx)
        mask = np.zeros_like(freqs, dtype=bool)
        for peak in selected:
            mask |= (freqs >= peak["f_lo"]) & (freqs <= peak["f_hi"])
        mask &= psd > diag["threshold"]
        if mask.any():
            scatter.setData(
                freqs[mask], 10 * np.log10(psd[mask] + DB_EPS)
            )
        else:
            scatter.setData([], [])

    # ------------------------------------------------------ calculation

    def _block_params(self) -> dict:
        """Parameters for calculate_fft_block: bands = KEPT frequencies
        (complement of the excluded regions); one threshold per channel —
        the channels can have different white noise levels."""
        return {
            "bands": self._kept_bands(),
            "threshold_ch1": self._threshold_linear_for(0),
            "threshold_ch2": (
                self._threshold_linear_for(1) if self._n_channels > 1 else None
            ),
            "method": "welch" if self._use_welch else "periodogram",
            "window": "hann" if self._use_hann else "rect",
            "nperseg": self.nperseg_spin.value(),
        }

    def _user_params(self) -> dict:
        """What the user drew — stored for reopening the dialog and archived
        with the analysis later. The excluded regions, the two threshold
        lines and the diagnostics snapshot (floor level, peaks) are stored
        verbatim."""
        params = {
            "excluded_bands": self._band_edges(),
            "threshold_ch1": self._threshold_linear_for(0),
            "threshold_ch2": (
                self._threshold_linear_for(1) if self._n_channels > 1 else None
            ),
            "method": "welch" if self._use_welch else "periodogram",
            "window": "hann" if self._use_hann else "rect",
            "nperseg": self.nperseg_spin.value(),
        }
        if self._stats:
            diagnostics = {}
            for idx, key in ((0, "ch1"), (1, "ch2")):
                diag = self._diag.get(idx)
                if diag is None:
                    continue
                diagnostics[key] = {
                    "floor_level": diag["floor_level"],
                    "threshold": diag["threshold"],
                    # only the peaks the user selected are archived
                    "peaks": self._checked_peaks(idx),
                    "peaks_detected": len(diag["peaks"]),
                }
            params["diagnostics"] = diagnostics
        return params

    def _on_calculate(self):
        if self._fs is None or self._fs <= 0 or len(self._data) < 2:
            self.info_lbl.setText(
                "<b style='color:#d62728;'>Invalid sample frequency or no data.</b>"
            )
            return
        self._clear_overlays()
        self._stats = {}
        self._diag = {}
        params = self._block_params()

        for idx in range(self._n_channels):
            stats, diag = calculate_fft_block(
                self._data[:, idx],
                self._fs,
                bands=params["bands"],
                threshold=(
                    params["threshold_ch1"] if idx == 0 else params["threshold_ch2"]
                ),
                method=params["method"],
                window=params["window"],
                nperseg=params["nperseg"],
            )
            self._stats[idx] = stats
            self._diag[idx] = diag

        self._draw_diagnostics()
        self._populate_peak_panel()
        self._update_info()
        self._update_summary()
        self.apply_bnt.setEnabled(True)

    def _draw_diagnostics(self):
        if self.view_box.currentIndex() != 0:
            return
        diag1 = self._diag.get(0)
        if diag1 is None:
            return
        freqs = diag1["freqs"]
        for channel_idx, fill in ((0, self._peak_fill_ch1), (1, self._peak_fill_ch2)):
            diag = self._diag.get(channel_idx)
            if diag is None or diag["threshold"] is None:
                continue
            thr_db = 10 * np.log10(diag["threshold"] + DB_EPS)
            psd = diag["psd"]
            db = 10 * np.log10(psd + DB_EPS)
            # hug that channel's own threshold where the PSD is below it: the
            # fill has zero height there and only the peaks show colour
            above = np.where(psd > diag["threshold"], db, thr_db)
            fill.setFillLevel(thr_db)
            fill.setData(freqs, above)
        self._floor_line_ch1.setPos(
            10 * np.log10(diag1["floor_level"] + DB_EPS)
        )
        self._floor_line_ch1.show()
        diag2 = self._diag.get(1)
        if self._n_channels > 1 and diag2 is not None:
            self._floor_line_ch2.setPos(
                10 * np.log10(diag2["floor_level"] + DB_EPS)
            )
            self._floor_line_ch2.show()

    def _update_info(self):
        diag = self._diag.get(0)
        if diag is None:
            if self._stats and all(
                (s or {}).get("N", 0) == 0 for s in self._stats.values()
            ):
                if self._kept_bands():
                    self.info_lbl.setText(
                        "<b style='color:#d62728;'>Not computable on this "
                        "data (too few valid samples).</b>"
                    )
                else:
                    self.info_lbl.setText(
                        "<b style='color:#d62728;'>All frequencies are "
                        "excluded — leave at least one region outside the "
                        "bands to measure the noise floor.</b>"
                    )
            else:
                self.info_lbl.setText("")
            return
        freqs, mask = diag["freqs"], diag["kept_mask"]
        n_all = int(mask.size) - 1  # exclude DC
        n_removed = int(np.count_nonzero(~mask[1:]))
        parts = []
        if self._band_edges():
            excluded = ", ".join(
                f"{lo:.1f}–{hi:.1f} Hz" for lo, hi in self._band_edges()
            )
            parts.append(
                f"Excluded {excluded} ({n_removed} of {n_all} bins, "
                f"{100.0 * n_removed / max(n_all, 1):.0f} % of the bandwidth)"
            )
        else:
            parts.append("No bands excluded")
        # the detected peaks are listed in the peak-selection panel below
        self.info_lbl.setText(" · ".join(parts))

    def _update_summary(self):
        for key, label, stat_key in self._result_rows:
            ch1_lbl, ch2_lbl = self._result_value_labels[key]
            for lbl, idx in ((ch1_lbl, 0), (ch2_lbl, 1)):
                if idx >= self._n_channels:
                    lbl.setText("—")
                    continue
                stats = self._stats.get(idx) or {}
                if not stats or stats.get("N", 0) == 0:
                    lbl.setText("—")
                    continue
                if key == "N":
                    lbl.setText(f"{int(stats['N'])}")
                elif key == "white":
                    white = stats["Variance"] - stats["Covariance"]
                    lbl.setText(f"{white:.6g}")
                else:
                    lbl.setText(f"{stats[stat_key]:.6g}")

        params = self._block_params()
        parts = [f"{params['method']}", f"{params['window']} window"]
        if params["threshold_ch1"] is not None or params["threshold_ch2"] is not None:
            def thr_txt(db):
                return f"{10 * np.log10(db + DB_EPS):.1f} dB"
            if self._n_channels > 1:
                t1 = thr_txt(params["threshold_ch1"]) if params["threshold_ch1"] is not None else "-"
                t2 = thr_txt(params["threshold_ch2"]) if params["threshold_ch2"] is not None else "-"
                parts.append(f"thresholds CH1 {t1} / CH2 {t2}")
            elif params["threshold_ch1"] is not None:
                parts.append(f"threshold {thr_txt(params['threshold_ch1'])}")
        else:
            parts.append("no threshold")
        if params["bands"]:
            excluded = ", ".join(f"{lo:.1f}–{hi:.1f} Hz" for lo, hi in self._band_edges())
            parts.append(f"floor from kept bins (excluded: {excluded})")
        else:
            parts.append("full band")
        self.options_lbl.setText(" · ".join(parts))

    # --------------------------------------------------------- apply/reset

    def _on_apply(self):
        if not self._stats:
            self._on_calculate()
            if not self._stats:
                return

        self.result_block = {}
        for key in ("N", "Mean", "Variance", "Covariance", "y_bar_d"):
            self.result_block[key] = [
                self._stat(0, key), 0,
                self._stat(1, key) if self._n_channels > 1 else 0, 0,
            ]
        self.applied_params = self._user_params()
        self.accept()

    def _stat(self, idx: int, key: str):
        stats = self._stats.get(idx) or {}
        return stats[key] if stats.get("N", 0) else 0

    def _on_reset(self):
        self.reset_requested.emit()
        self.accept()
