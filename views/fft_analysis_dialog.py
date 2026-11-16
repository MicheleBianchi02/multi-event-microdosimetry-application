"""
FFT analysis dialog for the frequency-domain variance-covariance estimator.

The user picks the spectrum options, draws a movable threshold line and any
number of exclusion bands directly on the PSD plot (or types their edges
manually, to re-enter the same band on every experiment), then launches the
calculation. The resulting var_fft block is handed back to the caller only
when the user applies it — nothing is computed or stored automatically.

The calculated block carries uncertainties: mean and variance from the same
circular block bootstrap as the other estimators, y_d (and the white
variance) from the chi-square floor statistics y·sqrt(1/M) with M the kept
frequency bins, and a covariance uncertainty of 0 (not known).
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
        # the Welch resolution moves the first bin above DC (the lowest
        # selectable frequency): keep the regions' drag bounds in step
        self.nperseg_spin.valueChanged.connect(
            lambda _v: self._refresh_region_bounds()
        )
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
        self.add_band_bnt.setToolTip(
            "Add an exclusion band centred on the visible frequency range."
        )
        self.add_band_bnt.clicked.connect(self._on_add_band)
        self.remove_band_bnt = QPushButton("Remove")
        self.remove_band_bnt.clicked.connect(self._on_remove_band)
        band_row.addWidget(self.add_band_bnt)
        band_row.addWidget(self.remove_band_bnt)
        floor_layout.addLayout(band_row)
        manual_row = QHBoxLayout()
        self.band_min_spin = QDoubleSpinBox()
        self.band_max_spin = QDoubleSpinBox()
        f_nyq = self._fs / 2.0 if self._fs else 1e9
        for spin, default in ((self.band_min_spin, 0.0), (self.band_max_spin, f_nyq)):
            spin.setDecimals(6)
            spin.setRange(0.0, f_nyq)
            spin.setSingleStep(1.0)
            spin.setSuffix(" Hz")
            spin.setKeyboardTracking(False)
            spin.setValue(default)
        self.add_manual_band_bnt = QPushButton("Add")
        self.add_manual_band_bnt.setToolTip(
            "Add an exclusion band with the typed edges (Hz) — handy to "
            "re-enter the same band on every experiment."
        )
        self.add_manual_band_bnt.clicked.connect(self._on_add_manual_band)
        manual_row.addWidget(self.band_min_spin)
        manual_row.addWidget(self.band_max_spin)
        manual_row.addWidget(self.add_manual_band_bnt)
        floor_layout.addLayout(manual_row)
        opt_layout.addWidget(floor_group)

        view_group = QGroupBox("View")
        view_layout = QVBoxLayout(view_group)
        self.view_box = QComboBox()
        self.view_box.addItems(["PSD", "Magnitude"])
        self.view_box.currentIndexChanged.connect(self._refresh_plot)
        view_layout.addWidget(self.view_box)
        self.log_x_bnt = QPushButton("Log frequency axis")
        self.log_x_bnt.setCheckable(True)
        self.log_x_bnt.setToolTip(
            "Toggle the frequency axis between linear and logarithmic "
            "scale. The 0 Hz bin (display-only DC level) is hidden in log "
            "scale; the calculation always integrates the full spectrum."
        )
        self.log_x_bnt.toggled.connect(self._on_log_x_toggled)
        view_layout.addWidget(self.log_x_bnt)
        self.lin_y_bnt = QPushButton("Linear Y axis")
        self.lin_y_bnt.setCheckable(True)
        self.lin_y_bnt.setToolTip(
            "Show the amplitudes on a linear scale (Ch²/Hz for the PSD, Ch "
            "for the magnitude) instead of dB — the height of the lines and "
            "their covariance contribution is easier to judge. The 0 Hz DC "
            "marker is hidden in linear mode (it sits decades above the "
            "floor); the calculation is unaffected."
        )
        self.lin_y_bnt.toggled.connect(self._on_lin_y_toggled)
        view_layout.addWidget(self.lin_y_bnt)
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
        self._exclude_from_autorange(self._peak_cursor)
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
        # (row key, label, stats key, uncertainty stats key); the "white" row
        # is derived (Variance − Covariance, uncertainty from the floor's
        # sqrt(2/k) relative error) and has no direct stats key
        self._result_rows = [
            ("N", "N", "N", None),
            ("mbins", "M [bins]", "M", None),
            ("mean", "Mean [Ch]", "Mean", "Mean_unc"),
            ("white", "Var−Cov (white) [Ch²]", None, None),
            ("cov", "Covariance [Ch²]", "Covariance", "Covariance_unc"),
            ("var", "Variance [Ch²]", "Variance", "Variance_unc"),
            ("ratio", "Cov / Var", "cov_var_ratio", "cov_var_ratio_unc"),
            ("yd", yd_html, "y_bar_d", "y_bar_d_unc"),
        ]
        self._result_value_labels = {}
        for row, (key, label, _stat, _unc) in enumerate(self._result_rows, start=1):
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
            for key in ("N", "mbins", "mean", "white", "cov", "var", "ratio", "yd"):
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
                self._threshold_line_ch1.setPos(
                    self._linear_to_display(float(params["threshold_ch1"]))
                )
            if params.get("threshold_ch2") is not None:
                self._threshold_line_ch2.setPos(
                    self._linear_to_display(float(params["threshold_ch2"]))
                )
            self._update_threshold_label()

    # ----------------------------------------------------------- helpers

    @property
    def _use_welch(self) -> bool:
        return self.method_box.currentIndex() == 1

    @property
    def _log_x(self) -> bool:
        return self.log_x_bnt.isChecked()

    @property
    def _lin_y(self) -> bool:
        return self.lin_y_bnt.isChecked()

    def _on_lin_y_toggled(self, checked: bool):
        # view-only: thresholds and floors are authoritative in linear PSD
        # units — only the line positions are re-expressed in the new scale
        for line in (self._threshold_line_ch1, self._threshold_line_ch2):
            v = float(line.value())
            lin = v if not checked else 10.0 ** (v / 10.0)  # old scale
            line.setPos(lin if checked else 10.0 * np.log10(lin + DB_EPS))
        self._refresh_plot()
        self._draw_diagnostics()
        self._update_threshold_label()

    def _on_log_x_toggled(self, checked: bool):
        # view-only: the calculation always integrates the full spectrum
        self.plot.setLogMode(x=checked, y=False)
        # pyqtgraph regions operate in VIEW coordinates, which are log10(Hz)
        # while the axis is logarithmic: convert the stored band edges
        # between the two spaces (a region left in linear units would
        # render and drag at 10^f Hz; a log-mode drag clamped by linear
        # bounds gets stuck just above 1 Hz)
        for region in self._band_regions:
            lo, hi = region.getRegion()
            if checked:
                new_values = self._region_values(lo, hi)
            else:
                new_values = self._canonical_edge_hz(lo, hi)
            region.setBounds(self._region_bounds())
            region.setRegion(new_values)
        self._refresh_plot()
        # bring the threshold fills / floor lines back on the new scale
        # (_refresh_plot only redraws the curves and the peak markers)
        self._draw_diagnostics()

    def _canonical_edge_hz(self, lo: float, hi: float) -> tuple[float, float]:
        """Log-space region values -> canonical linear Hz edges.

        Snapped back to 10 significant digits (the Hz → log10 → Hz
        round-trip loses the last float bits), and a left edge sitting at
        the log clamp — the first AC bin, the lowest place a log-axis band
        can reach — is mapped to 0 Hz: "exclude from the bottom" is the
        same intent on both axes, so switching scale must not change the
        kept set (leaving the edge at the first bin would re-include that
        bin's near-DC power in the floor on the linear axis).

        Total on ANY stored value: a non-finite edge falls back to the
        range ends, and an edge beyond the largest possible log10 value
        cannot be a log value at all — it is a stale linear-Hz edge (e.g.
        a region left unconverted by an earlier version) and is used as
        Hz directly instead of overflowing 10**v."""
        f_nyq = self._fs / 2.0 if self._fs else 1.0
        log_max = float(np.log10(max(f_nyq, 1e-12))) + 1.0

        def to_hz(v: float, fallback: float) -> float:
            if not np.isfinite(v):
                return fallback
            if v > log_max:
                return float(f"{min(max(v, 0.0), f_nyq):.10e}")
            return float(f"{10.0 ** v:.10e}")

        lo_hz = to_hz(lo, 0.0)
        hi_hz = to_hz(hi, f_nyq)
        if hi_hz < lo_hz:
            lo_hz, hi_hz = hi_hz, lo_hz
        lower = self._band_lower_bound()
        if lower > 0.0 and lo_hz <= lower * (1.0 + 1e-9):
            lo_hz = 0.0
        return (lo_hz, hi_hz)

    def _band_lower_bound(self) -> float:
        """Lowest frequency a band's vertical lines can reach on a LOG
        axis: the first bin above DC of the displayed spectrum (fs/n for
        the periodogram, fs/segment-length for Welch — mirrors
        compute_spectrum's resolution). On a linear axis bands reach 0 Hz
        (DC itself is always excluded from the floor by the calculation)."""
        if self._fs is None or self._fs <= 0 or len(self._data) < 2:
            return 0.0
        n = max(
            int(np.count_nonzero(~np.isnan(self._data[:, idx])))
            for idx in range(min(self._n_channels, self._data.shape[1]))
        )
        if n < 2:
            return 0.0
        if self._use_welch:
            n_segments = max(1, int(round(n / self.nperseg_spin.value())))
            n = n // n_segments
        return self._fs / n

    def _region_values(self, f_lo: float, f_hi: float) -> tuple[float, float]:
        """Linear-Hz band edges -> region-item values (log10 Hz in log
        mode, linear Hz otherwise), clamped to the selectable range."""
        if not self._log_x:
            return (f_lo, f_hi)
        # self-heal a mixed state: a negative "Hz" cannot be linear, it is
        # a stale log10 value — exponentiate it back before clamping
        if f_lo < 0.0:
            f_lo = 10.0**f_lo
        if f_hi < 0.0:
            f_hi = 10.0**f_hi
        lower = max(self._band_lower_bound(), 1e-12)
        upper = max(self._fs / 2.0, lower)
        f_lo = min(max(f_lo, lower), upper)
        f_hi = min(max(f_hi, lower), upper)
        if f_hi < f_lo:
            f_lo, f_hi = f_hi, f_lo
        return (float(np.log10(f_lo)), float(np.log10(f_hi)))

    def _region_bounds(self) -> tuple[float, float]:
        """Drag bounds of a region item, in its own coordinate space.

        Linear scale: bands may start at 0 Hz — DC itself never feeds the
        floor (always excluded), but the first AC bin must be excludable,
        otherwise the kept complement keeps a (0, first-bin] sliver and the
        near-DC bin leaks into the floor. Log scale: 0 Hz is unreachable,
        so the first bin above DC is the lowest selectable frequency."""
        if not self._log_x:
            return (0.0, self._fs / 2.0)
        lower = max(self._band_lower_bound(), 1e-12)
        return (float(np.log10(lower)), float(np.log10(self._fs / 2.0)))

    def _plot_data(self, item, x, y):
        """setData on a curve/fill/scatter, dropping the 0 Hz bin in log-x
        mode (log(0) is undefined) and in linear-y mode (the display-only
        mean² marker sits decades above the floor and would flatten the
        axis auto-range)."""
        if (self._log_x or self._lin_y) and len(x) and float(x[0]) == 0.0:
            x, y = x[1:], y[1:]
        item.setData(x, y)

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
        return self._display_to_linear(float(line.value()))

    def _region_edge_hz(self, region: pg.LinearRegionItem) -> tuple:
        """One region's edges in Hz (log10-stored while the axis is
        logarithmic; canonicalized by _canonical_edge_hz)."""
        lo, hi = region.getRegion()
        return self._canonical_edge_hz(lo, hi) if self._log_x else (lo, hi)

    def _band_edges(self) -> list:
        """The excluded frequency regions in Hz, sorted ascending, as the
        user drew them (a band at the log clamp is archived — and
        re-entered — as 'from 0 Hz')."""
        return sorted(self._region_edge_hz(r) for r in self._band_regions)

    def _sorted_region_indices(self) -> list:
        """Map band-list rows to regions: the list shows the edges sorted
        ascending while _band_regions keeps creation order, so removing
        by raw row would delete the wrong band."""
        return [
            i
            for _, i in sorted(
                (self._region_edge_hz(r), i)
                for i, r in enumerate(self._band_regions)
            )
        ]

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
        if self._log_x:
            # a band dragged to the log axis' left edge stops at the first
            # AC bin (0 Hz is unreachable there); drop the (0, first-bin]
            # sliver it would otherwise keep, so "exclude from the bottom"
            # gives the same kept set as a band starting at 0 Hz in linear
            # mode (DC is excluded by the calculation either way)
            lower = self._band_lower_bound()
            kept = [(lo, hi) for lo, hi in kept if hi > lower]
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
        return freqs, self._psd_to_display(psd)

    def _magnitude_display(self, channel: np.ndarray):
        """Magnitude spectrum (Ch), in the current y-scale."""
        y = channel[~np.isnan(channel)]
        values = np.abs(np.fft.rfft(y - y.mean(), norm="forward"))
        values[0] = abs(float(y.mean())) if len(y) else 0.0
        freqs = np.fft.rfftfreq(len(y), d=1.0 / self._fs)
        if self._lin_y:
            return freqs, values
        return freqs, 20 * np.log10(values + DB_EPS)

    def _psd_to_display(self, psd: np.ndarray) -> np.ndarray:
        """Linear PSD values -> plotted y values (dB by default)."""
        if self._lin_y:
            return psd
        return 10 * np.log10(psd + DB_EPS)

    def _linear_to_display(self, lin: float) -> float:
        """Linear amplitude (Ch²/Hz) -> plotted y position."""
        if self._lin_y:
            return float(lin)
        return float(10.0 * np.log10(lin + DB_EPS))

    def _display_to_linear(self, disp: float) -> float:
        """Plotted y position -> linear amplitude (Ch²/Hz)."""
        if self._lin_y:
            return float(disp)
        return float(10.0 ** (disp / 10.0))

    # ---------------------------------------------------------- slots

    def _on_method_changed(self):
        self.nperseg_spin.setEnabled(self._use_welch)
        self._refresh_region_bounds()
        self._clear_overlays()
        self._refresh_plot()

    def _refresh_region_bounds(self):
        """Re-apply the drag bounds after the spectrum resolution changed
        (the first bin above DC — the lowest selectable frequency — moves
        with the estimator and the Welch segment length)."""
        bounds = self._region_bounds()
        for region in self._band_regions:
            region.setBounds(bounds)

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
                line.setPos(self._auto_threshold_pos(idx))
        self._update_threshold_label()
        self._clear_overlays()

    def _on_threshold_moved(self):
        self._update_threshold_label()
        self._clear_overlays()

    def _auto_threshold_pos(self, idx: int) -> float:
        """A sensible starting height for the line, in DISPLAY units.

        The PSD bins of white noise are chi-squared distributed (median ~
        0.69x the level), so median + 9 dB (×5.8 in linear units) puts the
        line at ~5.8x the true floor level: above the fuzz, with a
        clipped-mean bias below 1%.
        """
        _, disp = self._display_psd(self._data[:, idx])
        if disp is None or len(disp) < 2:
            return 0.0
        if self._lin_y:
            return float(np.median(disp[1:])) * 10.0**0.9
        return float(np.median(disp[1:])) + 9.0

    def _update_threshold_label(self):
        if not self.use_threshold_check.isChecked():
            self.threshold_lbl.setText("Thresholds: not used")
            return
        lin1 = self._threshold_linear_for(0)
        db1 = 10 * np.log10(lin1 + DB_EPS)
        if self._n_channels > 1:
            lin2 = self._threshold_linear_for(1)
            db2 = 10 * np.log10(lin2 + DB_EPS)
            self.threshold_lbl.setText(
                f"Thresholds — CH1: {db1:.1f} dB ({lin1:.3g}"
                f" Ch²/Hz) · CH2: {db2:.1f} dB ({lin2:.3g}"
                " Ch²/Hz)"
            )
        else:
            self.threshold_lbl.setText(
                f"Threshold — CH1: {db1:.1f} dB ({lin1:.3g}" " Ch²/Hz)"
            )

    def _on_add_band(self):
        (f_min, f_max), _ = self.plot.viewRange()
        if self._log_x:
            # view range is in log10 units: center the new band on the
            # geometric mean, spanning a fifth of the visible log range
            log_lo, log_hi = f_min, f_max
            mid = (log_lo + log_hi) / 2.0
            half = (log_hi - log_lo) / 10.0
            self._add_band_region(
                10.0 ** (mid - half), 10.0 ** (mid + half)
            )
            return
        span = (f_max - f_min) / 5.0
        center = (f_min + f_max) / 2.0
        self._add_band_region(center - span / 2.0, center + span / 2.0)

    def _on_add_manual_band(self):
        """Add a band with typed edges in Hz — the same band can be re-entered
        on every experiment. Reversed edges are swapped; the values are
        clamped to the selectable range [first bin above DC, Nyquist]."""
        f_lo = self.band_min_spin.value()
        f_hi = self.band_max_spin.value()
        if f_hi < f_lo:
            f_lo, f_hi = f_hi, f_lo
        if not f_hi > f_lo:
            return
        self._add_band_region(f_lo, f_hi)
        self.band_list.setCurrentRow(len(self._band_regions) - 1)

    def _add_band_region(self, f_lo: float, f_hi: float):
        region = pg.LinearRegionItem(
            values=self._region_values(f_lo, f_hi),
            orientation="vertical",
            brush=pg.mkBrush(0, 0, 0, 40),
            movable=True,
        )
        region.setBounds(self._region_bounds())
        self._exclude_from_autorange(region)
        region.sigRegionChangeFinished.connect(self._on_band_region_changed)
        self.plot.addItem(region)
        self._band_regions.append(region)
        self._on_band_region_changed()

    def _on_band_region_changed(self):
        """A band was added, moved or removed: the floor line, the results
        table and the Apply button hold the PREVIOUS selection until the
        next Calculate — clear them like a threshold move does, so a stale
        floor can never be misread (or applied) as the current one."""
        self._refresh_band_list()
        self._clear_overlays()

    @staticmethod
    def _exclude_from_autorange(region: pg.LinearRegionItem):
        """Regions are overlays on a fixed spectrum: they must not drive the
        axis auto-range. In log-x mode they would poison it outright — a
        region reports its bounds in linear Hz, which the log-scaled view
        maps into the double-float limits (the view zooms out to ~1e308)."""
        region.dataBounds = lambda *args, **kwargs: None

    def _on_remove_band(self):
        row = self.band_list.currentRow()
        indices = self._sorted_region_indices()
        if 0 <= row < len(indices):
            self.plot.removeItem(self._band_regions.pop(indices[row]))
            self._on_band_region_changed()

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
        if is_psd:
            unit = "Ch²/Hz" if self._lin_y else "dB"
        else:
            unit = "Ch" if self._lin_y else "dB"
        self.plot.setLabel("left", "PSD" if is_psd else "Magnitude", units=unit)
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
                    self._plot_data(curve, freqs, db)
            else:
                freqs, mag = self._magnitude_display(channel)
                self._plot_data(curve, freqs, mag)
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
        self._peak_cursor.setRegion(
            self._region_values(peak["f_lo"], peak["f_hi"])
        )
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
            self._plot_data(scatter, freqs[mask], self._psd_to_display(psd[mask]))
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
            thr_disp = self._linear_to_display(diag["threshold"])
            psd = diag["psd"]
            disp = self._psd_to_display(psd)
            # hug that channel's own threshold where the PSD is below it: the
            # fill has zero height there and only the peaks show colour
            above = np.where(psd > diag["threshold"], disp, thr_disp)
            fill.setFillLevel(thr_disp)
            self._plot_data(fill, freqs, above)
        self._floor_line_ch1.setPos(
            self._linear_to_display(diag1["floor_level"])
        )
        self._floor_line_ch1.show()
        diag2 = self._diag.get(1)
        if self._n_channels > 1 and diag2 is not None:
            self._floor_line_ch2.setPos(
                self._linear_to_display(diag2["floor_level"])
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
        for key, label, stat_key, unc_key in self._result_rows:
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
                elif key == "mbins":
                    lbl.setText(f"{int(stats['M'])}")
                elif key == "white":
                    # the white variance IS the floor measurement: its
                    # relative error is the same sqrt(1/M) as y_bar_d's
                    white = stats["Variance"] - stats["Covariance"]
                    rel = np.sqrt(1.0 / stats["M"])
                    lbl.setText(f"{white:.6g} ± {white * rel:.6g}")
                elif unc_key is None:
                    lbl.setText(f"{stats[stat_key]:.6g}")
                else:
                    lbl.setText(f"{stats[stat_key]:.6g} ± {stats[unc_key]:.6g}")

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
        for key in ("N", "M", "Mean", "Variance", "Covariance", "y_bar_d", "cov_var_ratio"):
            unc_key = f"{key}_unc" if key not in ("N", "M") else None
            self.result_block[key] = [
                self._stat(0, key),
                self._stat(0, unc_key) if unc_key else 0,
                self._stat(1, key) if self._n_channels > 1 else 0,
                (
                    self._stat(1, unc_key) if unc_key else 0
                ) if self._n_channels > 1 else 0,
            ]
        self.applied_params = self._user_params()
        self.accept()

    def _stat(self, idx: int, key: str):
        stats = self._stats.get(idx) or {}
        return stats[key] if key and stats.get("N", 0) else 0

    def _on_reset(self):
        self.reset_requested.emit()
        self.accept()
