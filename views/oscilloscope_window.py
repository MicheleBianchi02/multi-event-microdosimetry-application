"""
Standalone oscilloscope window for ADC bench testing (`app.py --oscilloscope`).

A deliberately small tool, outside the measurement workflow: no hardware
config, no roles, no workspace — it talks to the NI USB-6451 directly and
samples on the card's INTERNAL sample clock at 1 MS/s (the measurement
driver only makes sense with the external CONV_START clock; this mode is
its complement for testing the ADC by itself).

The input channel is freely selectable: every one of the 16 inputs in
RSE, plus the 8 differential pairs (bench default: ai7 = AI7/AI15).
Voltage ranges reuse the driver's INPUT_RANGES so the scope and the
measurement driver cannot drift apart.
"""

import collections
import threading

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hardware.ni.ni_daqmx import NIDAQmxAcquisition

RATE_HZ = 1_000_000  # internal sample clock: fixed, per spec
CHUNK_SAMPLES = 25_000  # one reader-thread read (25 ms of data)
# Host-side DAQmx buffer: seconds of stream to survive GUI stalls (same
# philosophy as the measurement driver) — without it, DAQmx's default
# buffer is small and a busy GUI thread causes -200279 overruns.
BUFFER_SECONDS = 5.0

# Channel selector: EVERY input, in both terminations.  The 6451 offers
# 8 differential pairs (AI<n> = +, AI<n+8> = −, n = 0..7; DAQmx uses the
# partner automatically for DIFF on ai<n>) and 16 single-ended inputs
# (ai<n> and ai<n+8> share ADC n).  The first entry is the bench's
# current wiring.
TERMINALS = (
    [("ai7", "ai7 — diff (AI7/AI15)", "differential")]
    + [
        (f"ai{n}", f"ai{n} — diff (AI{n}/AI{n + 8})", "differential")
        for n in range(8)
        if n != 7
    ]
    + [(f"ai{n}", f"ai{n} — SE (AI{n})", "rse") for n in range(16)]
)

# window length: label -> samples at RATE_HZ.  10 s is the practical
# ceiling: the ring buffer holds the largest window plus margin (~81 MB
# at 10 s), and the time curve redraws are throttled to ~2 Hz there.
WINDOWS = (
    ("100 µs", 100),
    ("1 ms", 1_000),
    ("10 ms", 10_000),
    ("100 ms", 100_000),
    ("1 s", 1_000_000),
    ("2 s", 2_000_000),
    ("5 s", 5_000_000),
    ("10 s", 10_000_000),
)

# The spectrum is computed from at most this many of the most recent
# samples (1 s -> 1 Hz bins): beyond that the arrays (freq + last + sum)
# and the FFT cost grow with the window for little diagnostic value.
SPECTRUM_MAX_SAMPLES = 1_000_000

# Measurement section: rolling average width in windows.  Only the
# per-window (mean, std) pair is kept — never the window samples.
ROLLING_WINDOWS = 100


def _resolve_device() -> str:
    """Dev1 if present, else the first DAQmx device (error if none)."""
    from nidaqmx.system import System

    names = [d.name for d in System.local().devices]
    if not names:
        raise RuntimeError("no DAQmx device found (is the card connected?)")
    return "Dev1" if "Dev1" in names else names[0]


class _ScopeWorker(threading.Thread):
    """Owns the DAQmx task; writes samples into a preallocated ring buffer.

    The ring makes snapshot() an O(window) slice copy taken under the lock
    (never an O(buffer) concatenate): a deque + np.concatenate of the whole
    buffer on every GUI tick stalled the reader behind the lock long enough
    for the DAQmx host buffer to overrun (-200279, live finding at 1 MS/s).
    """

    RING_SAMPLES = max(n for _, n in WINDOWS) + 4 * CHUNK_SAMPLES

    def __init__(
        self, device: str, terminal: str, termination: str, vmin: float, vmax: float
    ):
        super().__init__(daemon=True, name="oscilloscope-reader")
        self.device = device
        self.terminal = terminal
        self.termination = termination
        self.vmin = vmin
        self.vmax = vmax
        self.stop_event = threading.Event()
        self.error = None
        self.total = 0
        self.lock = threading.Lock()
        self._ring = np.empty(self.RING_SAMPLES, dtype=np.float64)

    def _append(self, samples: np.ndarray) -> None:
        """Write one chunk into the ring (caller holds the lock)."""
        k = samples.size
        pos = self.total % self.RING_SAMPLES
        first = min(k, self.RING_SAMPLES - pos)
        self._ring[pos : pos + first] = samples[:first]
        if k > first:
            self._ring[: k - first] = samples[first:]
        self.total += k

    def snapshot(self, n: int) -> np.ndarray:
        """The most recent min(n, total) samples, in acquisition order."""
        with self.lock:
            count = self.total
            if count == 0:
                return np.empty(0)
            take = min(n, count, self.RING_SAMPLES)
            end = count % self.RING_SAMPLES
            if end == 0:
                end = self.RING_SAMPLES
            start = end - take
            if start >= 0:
                return self._ring[start:end].copy()
            # window spans the wraparound: older tail first, then the head
            return np.concatenate(
                (self._ring[self.RING_SAMPLES + start :], self._ring[:end])
            )

    def run(self):
        try:
            import nidaqmx
            from nidaqmx.constants import AcquisitionType, TerminalConfiguration
            from nidaqmx.errors import DaqError
            from nidaqmx.stream_readers import AnalogSingleChannelReader

            with nidaqmx.Task() as task:
                task.ai_channels.add_ai_voltage_chan(
                    f"{self.device}/{self.terminal}",
                    terminal_config=(
                        TerminalConfiguration.DIFF
                        if self.termination == "differential"
                        else TerminalConfiguration.RSE
                    ),
                    min_val=self.vmin,
                    max_val=self.vmax,
                )
                task.timing.cfg_samp_clk_timing(
                    rate=RATE_HZ, sample_mode=AcquisitionType.CONTINUOUS
                )
                task.in_stream.input_buf_size = max(
                    10_000, min(int(RATE_HZ * BUFFER_SECONDS), 50_000_000)
                )
                task.start()
                reader = AnalogSingleChannelReader(task.in_stream)
                buf = np.empty(CHUNK_SAMPLES, dtype=np.float64)
                while not self.stop_event.is_set():
                    try:
                        read = reader.read_many_sample(buf, CHUNK_SAMPLES, timeout=2.0)
                    except DaqError as exc:
                        if getattr(exc, "error_code", None) != -200284:
                            raise
                        read = int(getattr(exc, "samps_per_chan_read", 0) or 0)
                    if read > 0:
                        with self.lock:
                            self._append(buf[:read])
        except Exception as exc:
            self.error = str(exc)


class OscilloscopeWindow(QMainWindow):
    """Free-running single-channel scope on the USB-6451 ADC."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ATOMIQA — oscilloscope (USB-6451 ADC test)")
        self.resize(1000, 600)

        self.worker = None
        self.device = None
        self._current_terminal = None
        self._single_pending = False
        # rolling measurement: last ROLLING_WINDOWS (mean, std) pairs
        self._stat_windows = collections.deque(maxlen=ROLLING_WINDOWS)
        self._stat_base = None  # worker.total at the last absorbed window
        self._scope_drawn_total = None  # worker.total at the last time redraw
        # frequency spectrum (Spectrum tab): peak-normalized amplitude
        # spectrum of the last window (V^2 mean-square, one bin = one FFT
        # line, no per-Hz normalization) plus a cumulative power average
        # (sum/count — one array in memory regardless of run length)
        self._ms_last = None
        self._ms_sum = None
        self._ms_count = 0
        self._ms_freq = None
        self._spec_ranged_for = None  # (log-x, freq shape) last auto-ranged

        # ---------------------------------------------------------- plot
        pg.setConfigOptions(antialias=False)
        self.plot = pg.PlotWidget()
        self.curve = self.plot.plot(pen=pg.mkPen("#39d0d8", width=1))
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setLabel("left", "voltage", units="V")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setDownsampling(auto=True)
        self.plot.setClipToView(True)

        # ------------------------------------------------------- controls
        self.run_btn = QPushButton("Run")
        self.run_btn.setCheckable(True)
        self.run_btn.toggled.connect(self._on_run_toggled)

        self.single_btn = QPushButton("Single")
        self.single_btn.clicked.connect(self._on_single)

        # Pause: stops the acquisition but KEEPS the accumulated state —
        # the spectrum average (sum/count), the rolling measurement — so
        # after resuming the average continues where it left off.  Use it
        # to disturb the bench (cables, source) and discard the transient
        # without throwing away a long average.
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.toggled.connect(self._on_pause_toggled)

        self.channel_combo = QComboBox()
        for terminal, label, _term in TERMINALS:
            self.channel_combo.addItem(label, terminal)
        self.channel_combo.currentIndexChanged.connect(self._on_settings_changed)

        self.range_combo = QComboBox()
        for r in NIDAQmxAcquisition.INPUT_RANGES:  # driver's hardcoded list
            self.range_combo.addItem(f"±{r.max_volts:g} V", (r.min_volts, r.max_volts))
        self.range_combo.currentIndexChanged.connect(self._on_settings_changed)

        self.window_combo = QComboBox()
        for label, n in WINDOWS:
            self.window_combo.addItem(label, n)
        self.window_combo.setCurrentIndex(2)  # 10 ms
        self.window_combo.currentIndexChanged.connect(self._on_window_changed)

        self.stats_label = QLabel("—")
        self.measure_label = QLabel("rolling: —")
        self.status_label = QLabel("stopped")

        controls = QHBoxLayout()
        for w in (self.run_btn, self.single_btn, self.pause_btn):
            controls.addWidget(w)
        controls.addWidget(QLabel("Channel:"))
        controls.addWidget(self.channel_combo)
        controls.addWidget(QLabel("Range:"))
        controls.addWidget(self.range_combo)
        controls.addWidget(QLabel("Window:"))
        controls.addWidget(self.window_combo)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(self.plot)
        layout.addLayout(controls)
        layout.addWidget(self.stats_label)
        layout.addWidget(self.measure_label)
        layout.addWidget(self.status_label)
        # ------------------------------------------------- spectrum page
        self.spectrum_plot = pg.PlotWidget()
        self.spectrum_curve = self.spectrum_plot.plot(pen=pg.mkPen("#e5a13c", width=1))
        # NO downsampling: pyqtgraph's auto mode thins uniformly in bin
        # index — correct for a linear axis, wrong for a log-frequency
        # axis, where the low-frequency decades hold few bins but most of
        # the screen width and end up crushed into block min/max points
        # (live finding: 1 Hz bins painted 0 of the 1–10 Hz decade).
        # A plain line of up to 500k points paints fine at the per-window
        # redraw rate.
        self.spectrum_curve.setDownsampling(auto=False)
        self.spectrum_plot.setLabel("bottom", "frequency", units="Hz")
        self.spectrum_plot.setLabel("left", "amplitude", units="dBV")
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.25)
        self.spectrum_plot.setLogMode(True, False)  # log frequency axis
        self.spectrum_plot.setClipToView(True)

        self.avg_check = QCheckBox("Average")
        self.avg_check.setChecked(True)
        self.avg_check.toggled.connect(self._draw_spectrum)
        self.logx_check = QCheckBox("Log X")
        self.logx_check.setChecked(True)
        self.logx_check.toggled.connect(self._draw_spectrum)
        self.spectrum_status = QLabel("—")

        spectrum_controls = QHBoxLayout()
        spectrum_controls.addWidget(self.avg_check)
        spectrum_controls.addWidget(self.logx_check)
        spectrum_controls.addStretch(1)

        spectrum_layout = QVBoxLayout()
        spectrum_layout.addWidget(self.spectrum_plot)
        spectrum_layout.addLayout(spectrum_controls)
        spectrum_layout.addWidget(self.spectrum_status)

        # ------------------------------------------------------- assemble
        scope_page = QWidget()
        scope_page.setLayout(layout)
        spectrum_page = QWidget()
        spectrum_page.setLayout(spectrum_layout)

        self.tabs = QTabWidget()
        self.tabs.addTab(scope_page, "Scope")
        self.tabs.addTab(spectrum_page, "Spectrum")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(50)  # 20 Hz display

    # ------------------------------------------------------------ actions

    def _on_run_toggled(self, checked: bool):
        if checked:
            self.pause_btn.setEnabled(True)
            self._start_acquisition()
        else:
            # leaving Run also leaves Pause (a fresh start resets everything)
            self.pause_btn.setEnabled(False)
            if self.pause_btn.isChecked():
                self.pause_btn.setChecked(False)  # re-enters _on_pause_toggled
            self._stop_acquisition()

    def _on_single(self):
        """One window of data, then stop and freeze the display."""
        self._single_pending = True
        if not (self.worker and self.worker.is_alive()):
            self.run_btn.setChecked(True)  # triggers _start_acquisition

    def _on_pause_toggled(self, checked: bool):
        if checked:
            # halt the task, keep every accumulated statistic
            self._halt_worker()
            self.status_label.setText("paused — averaging kept (Run or Pause to resume)")
        elif self.run_btn.isChecked():
            # resume: fresh ring, but the average sum/count continues
            self._spawn_worker()
            self._stat_base = None          # re-baseline the rolling windows
            self._scope_drawn_total = None  # force one time-curve redraw
            self.status_label.setText(
                f"running — {self.device}/{self._current_terminal}, "
                f"averaging continued ({self._ms_count} windows)")

    def _on_settings_changed(self):
        if self.worker and self.worker.is_alive():
            self._stop_acquisition()
            self._start_acquisition()
        elif self.pause_btn.isChecked():
            # paused, but channel/range changed: the frozen average would
            # mix different inputs — clear it, stay paused
            self._stat_windows.clear()
            self._stat_base = None
            self._scope_drawn_total = None
            self._ms_last = None
            self._ms_sum = None
            self._ms_count = 0
            self._ms_freq = None
            self.status_label.setText("paused — settings changed, average reset")

    def _on_window_changed(self):
        n = self.window_combo.currentData()
        self.plot.setXRange(0, n / RATE_HZ, padding=0)
        # window length changed: restart the rolling measurement and the
        # spectrum average (bin count changes with the window), and force
        # one immediate time-curve redraw
        self._stat_windows.clear()
        self._stat_base = None
        self._scope_drawn_total = None
        self._ms_last = None
        self._ms_sum = None
        self._ms_count = 0
        self._ms_freq = None

    def _spawn_worker(self):
        """Start (or restart) the acquisition thread for the current
        channel/range settings — without touching accumulated state."""
        try:
            from nidaqmx.constants import LogicFamily  # noqa: F401 — presence probe
            from nidaqmx.system import PhysicalChannel, System  # noqa: F401
        except ImportError:
            QMessageBox.critical(
                self, "Oscilloscope", "The 'nidaqmx' package is not installed."
            )
            self.run_btn.setChecked(False)
            return False
        try:
            self.device = _resolve_device()
        except Exception as exc:
            QMessageBox.critical(self, "Oscilloscope", str(exc))
            self.run_btn.setChecked(False)
            return False

        terminal, _label, termination = TERMINALS[self.channel_combo.currentIndex()]
        vmin, vmax = self.range_combo.currentData()
        self._current_terminal = terminal
        self.worker = _ScopeWorker(self.device, terminal, termination, vmin, vmax)
        self.worker.start()
        return True

    def _start_acquisition(self):
        if not self._spawn_worker():
            return
        # a fresh Run resets every accumulated statistic
        self._stat_windows.clear()
        self._stat_base = None
        self._scope_drawn_total = None
        self._ms_last = None
        self._ms_sum = None
        self._ms_count = 0
        self._ms_freq = None
        self.status_label.setText(
            f"running — {self.device}/{self._current_terminal}, "
            f"internal clock {RATE_HZ / 1e6:g} MS/s"
        )

    def _halt_worker(self):
        """Stop the acquisition thread, keep all accumulated state."""
        self._single_pending = False
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            self.worker.join(timeout=3.0)
        self.worker = None

    def _stop_acquisition(self):
        self._halt_worker()
        self.run_btn.setChecked(False)
        self.status_label.setText("stopped")

    # ------------------------------------------------------------- timer

    def _refresh(self):
        if self.worker is None:
            return
        if self.worker.error:
            error, self.worker = self.worker.error, None
            self.run_btn.setChecked(False)
            self.status_label.setText("error")
            QMessageBox.critical(self, "Oscilloscope", f"Acquisition failed:\n{error}")
            return

        n = self.window_combo.currentData()
        if self._single_pending:
            if self.worker.total < n:
                return  # wait for the full window
            data = self.worker.snapshot(n)
            self._stop_acquisition()  # freezes the display
        else:
            # Throttled time-curve redraw: copying a long window on every
            # 50 ms tick would memcpy up to ~1.6 GB/s at the 10 s window —
            # redraw once >= 5% of the window is new (still every tick at
            # the short windows).
            if self._scope_drawn_total is None:
                self._scope_drawn_total = self.worker.total
            if self.worker.total - self._scope_drawn_total < max(1, n // 20):
                data = None
            else:
                data = self.worker.snapshot(n)
                self._scope_drawn_total = self.worker.total
            # rolling measurement: absorb one (mean, std) per completed
            # window — only the pair is stored, never the samples
            if self._stat_base is None:
                self._stat_base = self.worker.total
            if self.worker.total - self._stat_base >= n:
                w = self.worker.snapshot(n)
                self._stat_windows.append((float(w.mean()), float(w.std())))
                self._stat_base = self.worker.total
                self._update_spectrum(w)

        if data is None or data.size == 0:
            return
        x = np.arange(data.size) / RATE_HZ
        self.curve.setData(x, data)
        self.stats_label.setText(
            "window: mean = %.6g V   noise (rms) = %.3g V   Vpp = %.6g V   N = %d"
            % (data.mean(), data.std(), np.ptp(data), data.size)
        )
        if self._stat_windows:
            means, stds = zip(*self._stat_windows)
            self.measure_label.setText(
                "rolling %d/%d windows:  mean = %.6g V   mean noise (rms) = %.3g V"
                % (
                    len(means),
                    ROLLING_WINDOWS,
                    float(np.mean(means)),
                    float(np.mean(stds)),
                )
            )

    # ---------------------------------------------------------- spectrum

    def _on_tab_changed(self, _index: int):
        """Redraw on entering the Spectrum tab: the average accumulates
        on every completed window regardless of the visible tab, so only
        the drawing needs catching up."""
        if self.tabs.currentIndex() == 1:
            self._draw_spectrum()

    def _update_spectrum(self, window: np.ndarray) -> None:
        """RMS-normalized amplitude spectrum (V^2) of one window.

        Runs on EVERY completed window — the average accumulates even
        while the Scope tab is shown (the whole point: press Run, walk
        away, come back to a converged average).  At most
        SPECTRUM_MAX_SAMPLES of the most recent samples are transformed
        (windows longer than 1 s keep 1 Hz bins instead of growing the
        FFT).  Each bin carries the mean-square of that frequency
        component (dBV = 20·log10 of the rms value — a sine of peak
        amplitude A reads A/sqrt(2) in its bin).  No per-sqrt(Hz)
        normalization: dividing the bin value by the bin width, e_n =
        10^(dBV/20)/sqrt(delta_f), recovers the conventional one-sided
        noise density.  DC is removed before the FFT; the DC line and the
        Nyquist line are dropped: the former is a mean-removal residue,
        the latter has no negative-frequency twin, so its one-sided level
        sits 6 dB under the noise floor by convention (and an
        exactly-Nyquist tone is not reliably measurable anyway — its
        sampled amplitude depends on phase).  The average is a running
        sum/count of the mean-square spectra (power averaging) so memory
        stays at one spectrum.
        """
        window = window[-SPECTRUM_MAX_SAMPLES:]
        n = window.size
        if n < 4:
            return
        centered = window - window.mean()
        spec = np.abs(np.fft.rfft(centered))
        ms = 2.0 * (spec / n) ** 2  # V^2 rms per bin (one FFT line)
        freqs = np.fft.rfftfreq(n, d=1.0 / RATE_HZ)
        if n % 2 == 0:
            ms, freqs = ms[1:-1], freqs[1:-1]  # drop DC and Nyquist
        else:
            ms, freqs = ms[1:], freqs[1:]  # odd n: no Nyquist line
        self._ms_freq = freqs
        self._ms_last = ms
        self._ms_sum = ms.copy() if self._ms_sum is None else self._ms_sum + ms
        self._ms_count += 1
        if self.tabs.currentIndex() == 1:
            self._draw_spectrum()  # skip the drawing while the tab is hidden

    def _draw_spectrum(self) -> None:
        if self._ms_last is None or self._ms_freq is None:
            return
        self.spectrum_plot.setLogMode(self.logx_check.isChecked(), False)
        if self.avg_check.isChecked():
            ms = self._ms_sum / self._ms_count
            source = f"average of {self._ms_count} window(s)"
        else:
            ms = self._ms_last
            source = "last window"
        # dBV = 20·log10 of the bin's rms value, referenced to 1 V
        db = 10.0 * np.log10(np.maximum(ms, 1e-30))
        # setData needs a FRESH x array object: handing pyqtgraph the same
        # array is treated as a no-op, so its log/linear display cache
        # survives a Log X toggle and the curve paints empty afterwards.
        self.spectrum_curve.setData(self._ms_freq.copy(), db)
        # Re-range when the mode or the bin grid changed (first draw
        # included): the log axis otherwise paints once with the stale
        # linear range reinterpreted as log values, and its tick
        # generator overflows (10**<linear range>).  In log mode
        # setXRange takes log10 coordinates.  Never on plain refreshes,
        # so user zoom is preserved.
        range_key = (self.logx_check.isChecked(), self._ms_freq.shape)
        if self._spec_ranged_for != range_key:
            self._spec_ranged_for = range_key
            f0, f1 = float(self._ms_freq[0]), float(self._ms_freq[-1])
            if self.logx_check.isChecked():
                f0, f1 = np.log10(f0), np.log10(f1)
            self.spectrum_plot.getViewBox().setXRange(f0, f1, padding=0)
        self.spectrum_status.setText(
            "%s | %.0f bins, %.0f – %.0f Hz (bin width %.3g Hz)"
            % (
                source,
                self._ms_freq.size,
                self._ms_freq[0],
                self._ms_freq[-1],
                self._ms_freq[1] - self._ms_freq[0],
            )
        )

    # -------------------------------------------------------------- exit

    def closeEvent(self, event):
        self._stop_acquisition()
        super().closeEvent(event)
