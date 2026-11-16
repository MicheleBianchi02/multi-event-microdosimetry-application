"""
Live ramp analysis window: stream-through acquisition with results only.

Opened from the timing window's "Live Ramp Analysis…" button (enabled only
with the timing applied in ramp mode).  The acquisition path is exactly the
normal ramp run — arm, timing 'S', NI reader pushing blocks into a sink —
but the sink is a LiveRampSink: full windows are assembled in memory one at
a time, the per-window results are accumulated (power-averaged FFT of the
selected segment + the CDS start/stop difference series), and the samples
are discarded.  Nothing is written to disk except through the Export button.

Stop/Start preserves the accumulated results (like the oscilloscope's
Pause); the Reset button — or any selection change — restarts the averaging.
"""
import logging
import time
from datetime import datetime

import numpy as np
import pyqtgraph as pg

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from services.live_ramp import LiveRampSink

logger = logging.getLogger(__name__)

POLL_MS = 250              # UI poll of the sink (never on the sink thread)
ARM_WAIT_S = 3.0           # same arm -> start contract as a normal run
PREVIEW_MAX_POINTS = 200_000   # mean-pool longer ramps before painting
CDS_HIST_BINS = 256

# Stall diagnosis: with no progress on the input counters for this long,
# say so loudly (log + status bar) and say WHICH stall it is — zero samples
# (CONV_START/timing problem, the NI watchdog should also warn) vs samples
# but no complete window (geometry/alignment mismatch).
STALL_DIAG_S = 10
LOG_EVERY_N_WINDOWS = 25   # INFO heartbeat while averaging

# (label, seconds) — one shared unit for the four selection inputs
TIME_UNITS = (("µs", 1e-6), ("ms", 1e-3), ("s", 1.0))


class LiveRampWindow(QMainWindow):
    """Live ramp spectra + CDS statistics without recording the ramps."""

    #: emitted after a completed close: the owner discards the window so
    #: the next open is a FRESH one (all ramps/results/selection dropped —
    #: closing the analysis resets it, by design)
    window_closed = pyqtSignal()

    def __init__(self, session, hardware_service, hardware_manager,
                 simulate: bool, is_measurement_running, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ATOMIQA — Live Ramp Analysis")
        self.resize(1150, 750)

        self.session = session
        self.hardware = hardware_service          # services.hardware_service.HardwareService
        self.hardware_manager = hardware_manager
        self.simulate = bool(simulate)
        self._measurement_running = is_measurement_running  # callable -> bool

        self.sink = None
        self._receiving = False
        self._arm_timer = None
        self._arm_deadline = 0.0

        # diagnostics (all main-thread): progress tracking for the stall
        # detector, one-shot flags so a persistent error is not re-raised
        # into a dialog every 250 ms
        self._receiving_since = None
        self._last_progress_key = None
        self._last_progress_t = None
        self._next_log_count = LOG_EVERY_N_WINDOWS
        self._sink_error_shown = False
        self._driver_error_shown = False

        # redraw bookkeeping (current channel only)
        self._channel = None
        self._last_gen = -1
        self._last_count = -1
        self._last_cds_len = -1
        self._spec_ranged_for = None   # (log_x, freq shape) last auto-ranged
        self._preview_volts = None     # decimated latest window (display only)
        self._syncing_selection = False
        self._unit_index = 0           # index into TIME_UNITS
        self._hp_wanted = (False, 100.0)   # desired high-pass (applied at build)

        self._init_ui()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._refresh_facts()

    # ------------------------------------------------------------------ UI

    def _init_ui(self):
        pg.setConfigOptions(antialias=False)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ------------------------------------------------- controls bar
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._on_start_stop)
        self.reset_btn = QPushButton("Reset averaging")
        self.reset_btn.clicked.connect(self._on_reset)
        self.export_btn = QPushButton("Export…")
        self.export_btn.clicked.connect(self._on_export)

        self.channel_combo = QComboBox()
        self.channel_combo.currentIndexChanged.connect(self._on_channel_picked)

        self.facts_label = QLabel("—")
        self.statusBar().addPermanentWidget(self.facts_label)

        controls = QHBoxLayout()
        for w in (self.start_btn, self.reset_btn, self.export_btn):
            controls.addWidget(w)
        controls.addWidget(QLabel("Channel:"))
        controls.addWidget(self.channel_combo)
        controls.addStretch(1)
        layout.addLayout(controls)

        # -------------------------------------------- selection inputs
        sel_group = QGroupBox("Analysis selection (changes restart the averaging)")
        sel_form = QFormLayout(sel_group)

        self.unit_combo = QComboBox()
        for label, _ in TIME_UNITS:
            self.unit_combo.addItem(label)
        self.unit_combo.setCurrentIndex(0)  # µs — the natural ramp-dt unit
        self.unit_combo.currentIndexChanged.connect(self._on_unit_changed)

        def spin():
            s = QDoubleSpinBox()
            s.setDecimals(6)
            s.setMinimum(0.0)
            s.setSingleStep(1.0)
            return s

        self.fft_from_spin = spin()
        self.fft_to_spin = spin()
        self.cds_from_spin = spin()
        self.cds_to_spin = spin()
        for s in (self.fft_from_spin, self.fft_to_spin,
                  self.cds_from_spin, self.cds_to_spin):
            s.editingFinished.connect(self._on_selection_edited)

        fft_row = QHBoxLayout()
        fft_row.addWidget(QLabel("from"))
        fft_row.addWidget(self.fft_from_spin)
        fft_row.addWidget(QLabel("to"))
        fft_row.addWidget(self.fft_to_spin)
        fft_row.addWidget(QLabel("unit"))
        fft_row.addWidget(self.unit_combo)
        fft_row.addStretch(1)
        sel_form.addRow("FFT segment:", fft_row)

        cds_row = QHBoxLayout()
        cds_row.addWidget(QLabel("start at"))
        cds_row.addWidget(self.cds_from_spin)
        cds_row.addWidget(QLabel("stop at"))
        cds_row.addWidget(self.cds_to_spin)
        cds_row.addStretch(1)
        sel_form.addRow("CDS points:", cds_row)

        layout.addWidget(sel_group)

        # ------------------------------------------------------------ tabs
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # --- tab 1: latest ramp preview
        self.preview_plot = pg.PlotWidget()
        self.preview_plot.addLegend(offset=(8, 8))
        self.preview_curve = self.preview_plot.plot(
            pen=pg.mkPen("#1f77b4", width=1), name="latest ramp")
        self.preview_plot.setLabel("bottom", "time", units="s")
        self.preview_plot.setLabel("left", "voltage", units="V")
        self.preview_plot.showGrid(x=True, y=True, alpha=0.25)
        self.preview_plot.setDownsampling(auto=True)
        self.preview_plot.setClipToView(True)
        # static selection feedback on the preview
        self.fft_region = pg.LinearRegionItem(
            movable=False, brush=pg.mkBrush(31, 78, 156, 40),
            pen=pg.mkPen("#1f77b4", style=Qt.PenStyle.DashLine))
        self.fft_region.setZValue(-10)
        self.cds_start_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#d62728", width=2))
        self.cds_stop_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#2ca02c", width=2))
        for item in (self.fft_region, self.cds_start_line, self.cds_stop_line):
            self.preview_plot.addItem(item)
        self.preview_status = QLabel("waiting for the first window…")
        pv = QWidget()
        pv_l = QVBoxLayout(pv)
        pv_l.addWidget(self.preview_plot, 1)
        pv_l.addWidget(self.preview_status)
        self.tabs.addTab(pv, "Ramp preview")

        # --- tab 2: averaged spectrum
        self.spectrum_plot = pg.PlotWidget()
        self.spectrum_curve = self.spectrum_plot.plot(
            pen=pg.mkPen("#e5a13c", width=1))
        # NO auto-downsampling: it thins uniformly in bin index and crushes
        # the low decades on a log-frequency axis (oscilloscope finding)
        self.spectrum_curve.setDownsampling(auto=False)
        self.spectrum_plot.setLabel("bottom", "frequency", units="Hz")
        self.spectrum_plot.setLabel("left", "amplitude", units="dBV")
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spectrum_plot.setLogMode(True, False)
        self.spectrum_plot.setClipToView(True)
        self.spectrum_plot.scene().sigMouseMoved.connect(self._on_spectrum_hover)

        self.logx_check = QCheckBox("Log X")
        self.logx_check.setChecked(True)
        self.logx_check.toggled.connect(self._draw_spectrum)

        # High-pass on the FFT path: the ramp slope's 1/f² Fourier tail
        # buries the parallel-current noise at every frequency, so the
        # plain ramp FFT cannot show it — a 2nd-order Butterworth high-pass
        # removes the linear trend (1st order would not) and exposes the
        # noise's 1/f region between fc and the white voltage floor.
        # Reflection padding settles the filter, so it works at any fc.
        self.hp_check = QCheckBox("High-pass ramp")
        self.hp_check.toggled.connect(self._on_hp_changed)
        self.hp_fc_spin = QDoubleSpinBox()
        self.hp_fc_spin.setRange(0.01, 1e6)
        self.hp_fc_spin.setDecimals(2)
        self.hp_fc_spin.setSuffix(" Hz")
        self.hp_fc_spin.setValue(100.0)
        self.hp_fc_spin.setEnabled(False)
        self.hp_fc_spin.editingFinished.connect(self._on_hp_changed)
        self.hp_check.setToolTip(
            "2nd-order Butterworth high-pass applied to the FFT segment "
            "before the transform (reflection-padded, so the filter "
 "transient is settled). Removes the ramp slope's 1/f² tail so the "
            "parallel-current noise becomes visible. Changing it restarts "
            "the averaging.")

        self.spectrum_status = QLabel("—")
        sp_controls = QHBoxLayout()
        sp_controls.addWidget(self.logx_check)
        sp_controls.addWidget(self.hp_check)
        sp_controls.addWidget(self.hp_fc_spin)
        sp_controls.addStretch(1)
        sp = QWidget()
        sp_l = QVBoxLayout(sp)
        sp_l.addWidget(self.spectrum_plot, 1)
        sp_l.addLayout(sp_controls)
        sp_l.addWidget(self.spectrum_status)
        self.tabs.addTab(sp, "Spectrum")

        # --- tab 3: CDS statistics
        self.cds_stats_label = QLabel("—")
        self.cds_stats_label.setAlignment(Qt.AlignmentFlag.AlignTop |
                                          Qt.AlignmentFlag.AlignLeft)
        self.cds_hist_plot = pg.PlotWidget()
        self.cds_hist_plot.setLabel("bottom", "stop − start", units="V")
        self.cds_hist_plot.setLabel("left", "counts")
        self.cds_hist_plot.showGrid(x=True, y=True, alpha=0.25)
        cds = QWidget()
        cds_l = QVBoxLayout(cds)
        cds_l.addWidget(self.cds_stats_label)
        cds_l.addWidget(self.cds_hist_plot, 1)
        self.tabs.addTab(cds, "CDS")

    # --------------------------------------------------------- properties

    @property
    def is_acquiring(self) -> bool:
        """True while this window drives the acquisition (arming included)."""
        return self._receiving or self._arm_timer is not None

    # ------------------------------------------------------- start / stop

    def _on_start_stop(self):
        if self._receiving or self._arm_timer is not None:
            self._stop_receiving()
            return
        self._try_start()

    def _try_start(self):
        session = self.session
        if session.timing_mode != "ramp":
            QMessageBox.warning(
                self, "Live Ramp Analysis",
                "Ramp sampling is not active: re-apply the timing "
                "configuration with ramp sampling enabled.")
            return
        n_per_window = int(getattr(session, "samples_per_window", 0) or 0)
        if n_per_window < 2:
            QMessageBox.warning(
                self, "Live Ramp Analysis",
                "Invalid ramp window geometry — apply the timing "
                "configuration first.")
            return
        if self._measurement_running is not None and self._measurement_running():
            QMessageBox.warning(
                self, "Live Ramp Analysis",
                "A measurement is running. Stop it before starting the "
                "live ramp analysis.")
            return
        if getattr(self.hardware, "is_receiving", False):
            QMessageBox.warning(
                self, "Live Ramp Analysis",
                "The acquisition is already active (another run or analysis "
                "is receiving data).")
            return
        hw_mgr = self.hardware_manager
        if not self.simulate and hw_mgr is not None and \
                getattr(hw_mgr, "acquisition_is_virtual", False):
            QMessageBox.information(
                self, "Virtual Acquisition",
                "The acquisition interface is virtual (dummy): no data will "
                "be collected, so there is nothing to analyze.")
            return

        # (re)build the sink when there is none or the applied geometry
        # changed since it was built (a re-Apply between runs)
        if self.sink is not None and (
            self.sink.n != n_per_window
            or self.sink.dt != float(session.ramp_dt_s)
        ):
            self.sink.close()
            self.sink = None
        if self.sink is None:
            try:
                self.sink = self._build_sink(n_per_window)
            except ValueError as exc:
                QMessageBox.critical(self, "Live Ramp Analysis", str(exc))
                return
            self._on_sink_built()

        if self.simulate:
            self._begin_receiving()
            return

        # real hardware: same arm -> 3 s -> start contract as a normal run
        timing = hw_mgr.timing if hw_mgr is not None else None
        if timing is None:
            QMessageBox.critical(self, "Live Ramp Analysis",
                                 "No timing hardware available.")
            return
        if not hw_mgr.arm():
            QMessageBox.critical(self, "Live Ramp Analysis",
                                 "Arming command failed.")
            return
        self._arm_deadline = time.time() + ARM_WAIT_S
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Arming…")
        self._arm_timer = QTimer(self)
        self._arm_timer.setInterval(200)
        self._arm_timer.timeout.connect(self._on_arm_tick)
        self._arm_timer.start()
        self._on_arm_tick()

    def _on_arm_tick(self):
        remaining = self._arm_deadline - time.time()
        if remaining > 0:
            self.statusBar().showMessage(
                f"armed — starting in {remaining:.1f} s")
            return
        self._arm_timer.stop()
        self._arm_timer.deleteLater()
        self._arm_timer = None
        self._begin_receiving()

    def _begin_receiving(self):
        session = self.session
        rate = 0.0
        if self.simulate:
            period = session.int_time + session.reset_time
            if period <= 0:
                QMessageBox.critical(
                    self, "Live Ramp Analysis",
                    "Invalid timing configuration for simulation.")
                self._reset_start_button()
                return
            rate = 10 ** -3 / period
        ok = self.hardware.start_receiving(
            simulate=self.simulate, rate=rate, ramp_sink=self.sink
        )
        if not ok:
            logger.error("live ramp: start_receiving FAILED (see the log "
                         "above for the refused step)")
            QMessageBox.critical(self, "Live Ramp Analysis",
                                 "Start command failed.")
            self._reset_start_button()
            return
        self._receiving = True
        self._receiving_since = time.monotonic()
        self._last_progress_key = None
        self._last_progress_t = time.monotonic()
        self._driver_error_shown = False   # a fresh start clears the driver flag
        self._reset_start_button()
        self.start_btn.setText("Stop")
        logger.info(
            "live ramp: receiving started — N=%d samples/window, dt=%.6g s, "
            "channels=%s, simulate=%s, fft=%s, cds=%s, hp=%s",
            self.sink.n, self.sink.dt, self.sink.channels, self.simulate,
            self.sink.fft_span, self.sink.cds_points,
            (f"{self.sink.highpass[1]:g} Hz" if self.sink.highpass[0] else "off"),
        )
        self.statusBar().showMessage("acquiring — averaging in progress", 5000)

    def _stop_receiving(self):
        if self._arm_timer is not None:  # armed but not started
            self._arm_timer.stop()
            self._arm_timer.deleteLater()
            self._arm_timer = None
            self._reset_start_button()
            self.statusBar().showMessage("arming cancelled", 5000)
            return
        ok = self.hardware.stop_receiving(not self.simulate)
        self._receiving = False
        self._receiving_since = None
        self._reset_start_button()
        logger.info("live ramp: receiving stopped (driver stop %s) — %s",
                    "ok" if ok else "FAILED",
                    self.sink.summary() if self.sink else "no sink")
        # results stay: Stop behaves like the oscilloscope's Pause
        self.statusBar().showMessage(
            "stopped — results kept (Reset clears them)", 10000)
        if not ok:
            QMessageBox.critical(self, "Live Ramp Analysis",
                                 "Stop command failed (see the log).")

    def _reset_start_button(self):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start")

    def _build_sink(self, n_per_window: int) -> LiveRampSink:
        session = self.session
        hw_mgr = self.hardware_manager
        if hw_mgr is not None and hw_mgr.is_loaded:
            channels = list(range(len(hw_mgr.active_channels)))
        else:
            channels = [0] + ([1] if session.multiple_channel else [])

        in_range = session.adc_input_range
        if in_range is None and hw_mgr is not None and hw_mgr.is_loaded:
            adc = hw_mgr.get_adc_settings() or {}
            volts = (adc.get("input_range_volts") or {}).get("min_volts"), \
                    (adc.get("input_range_volts") or {}).get("max_volts")
            if volts[0] is not None and volts[1] is not None:
                in_range = volts
        if in_range is None:
            raise ValueError(
                "ADC input range unknown — apply the timing configuration "
                "before starting the live ramp analysis.")

        return LiveRampSink(
            samples_per_window=n_per_window,
            channels=channels,
            ramp_dt_s=float(session.ramp_dt_s),
            adc_bits=int(session.adc_bits),
            volts_min=float(in_range[0]),
            volts_max=float(in_range[1]),
        )

    def _on_sink_built(self):
        """First creation only: channel list, default selection, markers."""
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        for ch in self.sink.channels:
            self.channel_combo.addItem(f"CH{ch + 1}", ch)
        self.channel_combo.blockSignals(False)
        self._channel = self.sink.channels[0] if self.sink.channels else None
        self._last_gen = self._last_count = self._last_cds_len = -1
        self._preview_volts = None
        self._sink_error_shown = False
        self._next_log_count = LOG_EVERY_N_WINDOWS

        n = self.sink.n
        t_int = (n - 1) * self.sink.dt
        self._syncing_selection = True
        for s in (self.fft_from_spin, self.fft_to_spin,
                  self.cds_from_spin, self.cds_to_spin):
            s.setMaximum(1e12)  # any unit; values are clamped on apply
            s.setValue(0.0)
        self.fft_to_spin.setValue(self._to_unit(t_int))
        self.cds_to_spin.setValue(self._to_unit(t_int))
        self._syncing_selection = False
        # apply the remembered high-pass to the fresh sink (no reset needed)
        if self._hp_wanted[0]:
            try:
                self.sink.set_highpass(True, self._hp_wanted[1])
            except ValueError:
                pass
        self._update_markers()
        self._refresh_facts()

    # ------------------------------------------------------- selection UI

    def _unit_seconds(self) -> float:
        return TIME_UNITS[self.unit_combo.currentIndex()][1]

    def _to_unit(self, seconds: float) -> float:
        return seconds / self._unit_seconds()

    def _on_unit_changed(self):
        """Re-express the four values in the newly chosen unit (no reset —
        the underlying selection does not change)."""
        new_index = self.unit_combo.currentIndex()
        old_scale = TIME_UNITS[self._unit_index][1]
        new_scale = TIME_UNITS[new_index][1]
        self._unit_index = new_index
        factor = old_scale / new_scale
        self._syncing_selection = True
        for s in (self.fft_from_spin, self.fft_to_spin,
                  self.cds_from_spin, self.cds_to_spin):
            s.setValue(s.value() * factor)
        self._syncing_selection = False

    def _sync_selection_from_sink(self):
        """Set the spinboxes from the sink's current selection (in the
        current unit)."""
        if self.sink is None:
            return
        dt = self.sink.dt
        i0, i1 = self.sink.fft_span
        a, b = self.sink.cds_points
        self._syncing_selection = True
        self.fft_from_spin.setValue(self._to_unit(i0 * dt))
        # the spin holds the INCLUSIVE end time of the exclusive span [i0, i1)
        self.fft_to_spin.setValue(self._to_unit((i1 - 1) * dt))
        self.cds_from_spin.setValue(self._to_unit(a * dt))
        self.cds_to_spin.setValue(self._to_unit(b * dt))
        self._syncing_selection = False

    def _on_selection_edited(self):
        if self._syncing_selection:
            return
        self._apply_selection(reset=True, announce=True)

    def _on_hp_changed(self):
        """Push the high-pass state to the sink (restarting the averaging —
        an average is only meaningful under one filter state).  Remembered
        until a sink exists and applied when it is built."""
        enabled = self.hp_check.isChecked()
        self.hp_fc_spin.setEnabled(enabled)
        self._hp_wanted = (enabled, self.hp_fc_spin.value())
        if self.sink is None:
            return
        try:
            changed = self.sink.set_highpass(enabled, self.hp_fc_spin.value())
        except ValueError as exc:
            self._error(f"Invalid high-pass cutoff: {exc}")
            return
        if changed:
            self._last_count = self._last_cds_len = -1
            self._draw_spectrum()
            self._draw_cds()
            self.statusBar().showMessage(
                "high-pass changed — averaging restarted", 10000)
        else:
            self._draw_spectrum()

    def _apply_selection(self, reset: bool, announce: bool):
        """Read the spinboxes, resolve sample indices, push to the sink."""
        if self.sink is None:
            return
        dt = self.sink.dt
        unit = self._unit_seconds()
        t0 = self.fft_from_spin.value() * unit
        t1 = self.fft_to_spin.value() * unit
        ta = self.cds_from_spin.value() * unit
        tb = self.cds_to_spin.value() * unit
        n = self.sink.n

        # FFT: "from" = first sample at/after t0; "to" = INCLUSIVE end time,
        # so typing T_int selects the full window, last sample included
        i0 = int(np.clip(round(t0 / dt), 0, n))
        i1 = int(np.clip(round(t1 / dt) + 1, 0, n))
        a = int(np.clip(round(ta / dt), 0, n - 1))
        b = int(np.clip(round(tb / dt), 0, n - 1))

        if i1 - i0 < 2:
            self.statusBar().showMessage(
                "FFT segment shorter than 2 samples — not applied", 10000)
            self._sync_selection_from_sink()
            return

        changed = self.sink.set_selection(fft_span=(i0, i1), cds_points=(a, b))
        if changed and reset:
            self._last_count = self._last_cds_len = -1
            self._draw_spectrum()
            self._draw_cds()
            if announce:
                self.statusBar().showMessage(
                    "selection changed — averaging restarted", 10000)
        self._update_markers()
        self._refresh_facts()

    def _update_markers(self):
        """Position the preview region/lines at the current selection."""
        if self.sink is None:
            return
        dt = self.sink.dt
        i0, i1 = self.sink.fft_span
        a, b = self.sink.cds_points
        self.fft_region.setRegion((i0 * dt, i1 * dt))
        self.cds_start_line.setPos(a * dt)
        self.cds_stop_line.setPos(b * dt)

    # ----------------------------------------------------------- polling

    def _on_channel_picked(self, _index):
        ch = self.channel_combo.currentData()
        if ch == self._channel:
            return
        self._channel = ch
        self._last_gen = self._last_count = self._last_cds_len = -1
        self._preview_volts = None
        self._draw_spectrum()
        self._draw_cds()

    def _poll(self):
        sink = self.sink
        if sink is None or self._channel is None:
            return
        snap = sink.snapshot(self._channel)
        self._poll_diagnostics(snap)

        if snap["latest_gen"] != self._last_gen:
            self._last_gen = snap["latest_gen"]
            gen, volts = sink.latest_volts(self._channel)
            if volts is not None:
                idx, vals = self._decimate(volts)
                self._preview_volts = vals
                t = idx * sink.dt
                self.preview_curve.setData(t, vals)
                self.preview_status.setText(
                    f"latest window · {volts.size:,} samples · "
                    f"gen {gen} · {snap['windows_seen']:,} windows seen")
            elif self._receiving:
                # alive-and-waiting feedback: the input counters prove the
                # stream is (or is not) flowing even before window 0
                elapsed = time.monotonic() - (self._receiving_since or time.monotonic())
                self.preview_status.setText(
                    f"waiting for the first window… ({elapsed:.0f} s · "
                    f"{snap['blocks_in']:,} blocks · "
                    f"{snap['samples_in']:,} samples in)")

        if snap["count"] != self._last_count:
            self._last_count = snap["count"]
            self._draw_spectrum_with(snap)
        if snap["diff_v"].size != self._last_cds_len:
            self._last_cds_len = snap["diff_v"].size
            self._draw_cds_with(snap)

    def _poll_diagnostics(self, snap: dict):
        """Errors and stalls say so loudly — a silent empty plot is the
        worst possible failure mode (everything logged, the persistent
        ones also raised once into a dialog)."""
        sink = self.sink
        error = sink.summary()["error"]
        if error and not self._sink_error_shown:
            self._sink_error_shown = True
            logger.error("live ramp sink error: %s", error)
            QMessageBox.critical(
                self, "Live Ramp Analysis",
                f"The analysis sink failed:\n{error}\n\nSee the log for the "
                "traceback — stop and restart to retry.")
        acq = getattr(self.hardware_manager, "acquisition", None) \
            if self.hardware_manager is not None else None
        if self._receiving and acq is not None and getattr(acq, "error", False) \
                and not self._driver_error_shown:
            self._driver_error_shown = True
            logger.error("live ramp: the acquisition driver reported a read "
                         "error (see the DAQmx traceback above)")
            QMessageBox.critical(
                self, "Live Ramp Analysis",
                "The ADC driver reported a read error — see the log/console "
                "for the DAQmx traceback.")

        if not self._receiving:
            return
        now = time.monotonic()
        key = (snap["windows_seen"], snap["samples_in"])
        if key != self._last_progress_key:
            self._last_progress_key = key
            self._last_progress_t = now
            if snap["count"] >= self._next_log_count:
                self._next_log_count = snap["count"] + LOG_EVERY_N_WINDOWS
                logger.info("live ramp: %d windows averaged (ch%s)",
                            snap["count"], self._channel)
        elif now - self._last_progress_t >= STALL_DIAG_S:
            stalled_for = now - self._last_progress_t
            self._last_progress_t = now
            if snap["samples_in"] == 0:
                msg = (f"no samples received for {stalled_for:.0f} s — the "
                       "ADC task is armed but nothing arrives: is the "
                       "timing engine (CONV_START) running?")
            elif snap["windows_seen"] == 0:
                msg = (f"{snap['samples_in']:,} samples received but no "
                       f"complete window ({sink.n:,} needed, "
                       f"{sink.filled(self._channel):,} assembled): "
                       "window-geometry mismatch?")
            else:
                msg = (f"no new data for {stalled_for:.0f} s "
                       f"({snap['windows_seen']:,} windows so far): the "
                       "stream stalled — check the log for gaps/errors")
            logger.warning(
                "live ramp: %s [ch%s blocks=%s samples=%s windows=%s]",
                msg, self._channel, snap["blocks_in"], snap["samples_in"],
                snap["windows_seen"])
            self.statusBar().showMessage(f"⚠ {msg}", 30000)

    @staticmethod
    def _decimate(v: np.ndarray, target: int = PREVIEW_MAX_POINTS):
        """Mean-pool to ~target points for painting (display only)."""
        if v.size <= target:
            return np.arange(v.size), v
        b = int(np.ceil(v.size / target))
        m = v.size // b
        pooled = v[: m * b].reshape(m, b).mean(axis=1)
        idx = np.arange(m) * b + b // 2
        return idx, pooled

    # ---------------------------------------------------------- drawing

    def _draw_spectrum(self):
        if self.sink is None or self._channel is None:
            self.spectrum_curve.setData([], [])
            self.spectrum_status.setText("—")
            return
        self._draw_spectrum_with(self.sink.snapshot(self._channel))

    def _draw_spectrum_with(self, snap: dict):
        freqs, ms, count = snap["freqs"], snap["ms"], snap["count"]
        hp_txt = ""
        if self.sink is not None:
            hp_on, hp_fc = self.sink.highpass
            hp_txt = (f" · HP {hp_fc:g} Hz" if hp_on else
                      " · HP off (ramp tail not removed)")
        if freqs is None or count == 0:
            self.spectrum_curve.setData([], [])
            self.spectrum_status.setText(
                f"no spectrum yet — waiting for windows{hp_txt}")
            return
        db = 10.0 * np.log10(np.maximum(ms, 1e-30))
        # fresh arrays every time: a stale display cache breaks the log axis
        self.spectrum_curve.setData(freqs.copy(), db.copy())

        log_x = self.logx_check.isChecked()
        self.spectrum_plot.setLogMode(log_x, False)
        range_key = (log_x, freqs.shape)
        if self._spec_ranged_for != range_key:
            self._spec_ranged_for = range_key
            f0, f1 = float(freqs[0]), float(freqs[-1])
            if log_x:
                f0, f1 = np.log10(f0), np.log10(f1)
            self.spectrum_plot.getViewBox().setXRange(f0, f1, padding=0)

        if self.sink is not None:
            i0, i1 = self.sink.fft_span
            n_seg = i1 - i0
            dt = self.sink.dt
            df = 1.0 / (n_seg * dt)
            self.spectrum_status.setText(
                f"{count:,} windows averaged · segment "
                f"[{i0 * dt:.6g}, {i1 * dt:.6g}] s{hp_txt} · "
                f"df = {df:.6g} Hz · Nyquist = {0.5 / dt:.6g} Hz")

    def _on_spectrum_hover(self, pos):
        item = self.spectrum_plot.getPlotItem()
        if not item.sceneBoundingRect().contains(pos):
            return
        mouse = item.vb.mapSceneToView(pos)
        self.statusBar().showMessage(
            f"f = {mouse.x():.4g} Hz   {mouse.y():.2f} dBV")

    def _draw_cds(self):
        if self.sink is None or self._channel is None:
            self.cds_stats_label.setText("—")
            self.cds_hist_plot.clear()
            return
        self._draw_cds_with(self.sink.snapshot(self._channel))

    def _draw_cds_with(self, snap: dict):
        diff = snap["diff_v"]
        if diff.size == 0:
            self.cds_stats_label.setText("no CDS values yet — waiting for windows")
            self.cds_hist_plot.clear()
            return

        def stats_line(name, arr):
            if arr.size == 0:
                return f"{name}: —"
            return (f"{name}: mean {arr.mean():+.6e} V · "
                    f"std {arr.std(ddof=1) if arr.size > 1 else 0.0:.3e} V · "
                    f"min {arr.min():+.6e} · max {arr.max():+.6e}")

        lines = [
            f"windows: {diff.size:,}",
            stats_line("start", snap["start_v"]),
            stats_line("stop ", snap["stop_v"]),
            stats_line("diff ", diff),
            f"diff variance: "
            f"{diff.var(ddof=1) if diff.size > 1 else 0.0:.6e} V²",
        ]
        self.cds_stats_label.setText("\n".join(lines))

        self.cds_hist_plot.clear()
        hist, edges = np.histogram(diff, bins=CDS_HIST_BINS)
        centers = 0.5 * (edges[:-1] + edges[1:])
        widths = np.maximum(edges[1:] - edges[:-1], 1e-15)
        bg = pg.BarGraphItem(x=centers, height=hist, width=widths,
                             brush="#1f77b4", pen=None)
        self.cds_hist_plot.addItem(bg)

    # ------------------------------------------------------------ reset

    def _on_reset(self):
        if self.sink is None:
            self.statusBar().showMessage("nothing to reset — no sink yet", 10000)
            return
        self.sink.reset()
        self._last_count = self._last_cds_len = -1
        self._draw_spectrum()
        self._draw_cds()
        self.statusBar().showMessage("averaging restarted", 10000)

    # ----------------------------------------------------------- export

    def _on_export(self):
        import os

        sink = self.sink
        if sink is None:
            self._error("nothing to export — start the acquisition first")
            return
        default = f"live_ramp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        base, _ = QFileDialog.getSaveFileName(
            self, "Export live ramp results (base name)", default, "CSV (*.csv)")
        if not base:
            return
        if base.endswith(".csv"):
            base = base[:-len(".csv")]

        written = []
        for ch in sink.channels:
            snap = sink.snapshot(ch)
            if snap["count"] == 0 and snap["diff_v"].size == 0:
                continue
            label = f"ch{ch + 1}"
            written.append(self._export_spectrum(base, label, snap))
            written.append(self._export_cds(base, label, snap))

        if written:
            self.statusBar().showMessage(
                f"exported {len(written)} files to {os.path.dirname(base) or '.'}",
                10000)
        else:
            self.statusBar().showMessage("no results to export yet", 10000)

    def _export_header(self, snap: dict) -> list:
        session = self.session
        sink = self.sink
        i0, i1 = sink.fft_span
        a, b = sink.cds_points
        dt = sink.dt
        rng = session.adc_input_range
        hp_on, hp_fc = sink.highpass
        return [
            "# ATOMIQA live ramp analysis",
            f"# exported: {datetime.now().astimezone().isoformat()}",
            f"# timing_mode: {session.timing_mode}",
            f"# integration_time_s: {session.int_time}",
            f"# reset_time_s: {session.reset_time}",
            f"# period_s: {session.period_s}",
            f"# ramp_dt_s: {dt}",
            f"# samples_per_window: {sink.n}",
            f"# adc_bits: {session.adc_bits}",
            f"# input_range_v: {list(rng) if rng else 'unknown'}",
            f"# fft_segment_samples: [{i0}, {i1})",
            f"# fft_segment_s: [{i0 * dt}, {i1 * dt})",
            f"# cds_points_samples: ({a}, {b})",
            f"# cds_points_s: ({a * dt}, {b * dt})",
            f"# highpass_hz: {hp_fc if hp_on else 'off'}",
            "# highpass: " + (
                "2nd-order Butterworth on the FFT segment, reflection-padded"
                if hp_on else "none (plain ramp FFT)"),
            f"# windows_in_spectrum: {snap['count']}",
            f"# windows_in_cds: {snap['diff_v'].size}",
        ]

    def _export_spectrum(self, base: str, label: str, snap: dict) -> str:
        path = f"{base}_{label}_spectrum.csv"
        freqs, ms = snap["freqs"], snap["ms"]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(self._export_header(snap)) + "\n")
            f.write("freq_hz,ms_v2,dbv\n")
            if freqs is not None and ms is not None:
                db = 10.0 * np.log10(np.maximum(ms, 1e-30))
                f.write("\n".join(
                    "%.9g,%.9e,%.6f" % row
                    for row in zip(freqs, ms, db)) + "\n")
        return path

    def _export_cds(self, base: str, label: str, snap: dict) -> str:
        path = f"{base}_{label}_cds.csv"
        start_v, stop_v, diff = snap["start_v"], snap["stop_v"], snap["diff_v"]
        extra = []
        if diff.size:
            var = diff.var(ddof=1) if diff.size > 1 else 0.0
            extra = [
                f"# cds_diff_mean_v: {diff.mean():.9e}",
                f"# cds_diff_std_v: "
                f"{diff.std(ddof=1) if diff.size > 1 else 0.0:.9e}",
                f"# cds_diff_variance_v2: {var:.9e}",
            ]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(self._export_header(snap) + extra) + "\n")
            f.write("window,start_v,stop_v,diff_v\n")
            f.write("\n".join(
                "%d,%.9e,%.9e,%.9e" % row
                for row in zip(range(start_v.size), start_v, stop_v, diff)
            ) + "\n")
        return path

    # ------------------------------------------------------------- misc

    def _refresh_facts(self):
        """Permanent status-bar facts: geometry + selection resolution."""
        if self.sink is None:
            n = int(getattr(self.session, "samples_per_window", 0) or 0)
            dt = float(getattr(self.session, "ramp_dt_s", 0.0) or 0.0)
            if n < 2 or dt <= 0:
                self.facts_label.setText("timing not applied")
                return
        else:
            n, dt = self.sink.n, self.sink.dt
        self.facts_label.setText(
            f"N = {n:,} · dt = {dt * 1e6:.4g} µs · "
            f"T_int = {(n - 1) * dt:.6g} s")

    def _error(self, message: str):
        QMessageBox.warning(self, "Live Ramp Analysis", message)

    def closeEvent(self, event):
        # cancel a pending arm countdown first: its tick would otherwise
        # start the acquisition on a closed window
        if self._arm_timer is not None:
            self._arm_timer.stop()
            self._arm_timer.deleteLater()
            self._arm_timer = None
        if self._receiving:
            answer = QMessageBox.question(
                self, "Live Ramp Analysis",
                "Acquisition is running. Stop it and close the window?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._stop_receiving()
        if self.sink is not None:
            self.sink.close()
            self.sink = None
        self._poll_timer.stop()
        logger.info("live ramp window closed — everything is reset; the "
                    "next open starts from scratch")
        self.window_closed.emit()
        event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        # belt and braces: a reused window must never sit with a dead poll
        # timer (the closed-window reuse bug: data flowed into the sink,
        # nothing reached the screen)
        if not self._poll_timer.isActive():
            self._poll_timer.start()
