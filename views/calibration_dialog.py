import numpy as np
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox, 
    QPushButton, QScrollArea, QWidget, QFrame, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QLocale
from PyQt6.QtGui import QFont
import pyqtgraph as pg

class PeakCard(QFrame):
    def __init__(self, peak_idx, mean_channel):
        super().__init__()
        self.mean_channel = mean_channel
        self.setStyleSheet("""
            PeakCard {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 8px;
                padding: 10px;
                margin-bottom: 5px;
            }
        """)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        info_layout = QVBoxLayout()
        title = QLabel(f"Peak {peak_idx + 1}")
        title.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #343a40; border: none;")
        
        chan_label = QLabel(f"Mean Channel: {mean_channel:.2f}")
        chan_label.setStyleSheet("color: #6c757d; border: none;")
        
        info_layout.addWidget(title)
        info_layout.addWidget(chan_label)
        
        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedSize(24, 24)
        self.remove_btn.setStyleSheet("QPushButton { color: red; font-weight: bold; border: none; } QPushButton:hover { background-color: #f8d7da; border-radius: 12px; }")
        
        self.voltage_spin = QDoubleSpinBox()
        self.voltage_spin.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        self.voltage_spin.setRange(0, 10000)
        self.voltage_spin.setDecimals(3)
        self.voltage_spin.setSuffix(" V")
        self.voltage_spin.setValue(0.0)
        self.voltage_spin.setStyleSheet("""
            QDoubleSpinBox {
                padding: 5px;
                border: 1px solid #ced4da;
                border-radius: 4px;
                font-size: 12pt;
                background-color: white;
            }
        """)
        
        layout.addLayout(info_layout)
        layout.addStretch()
        layout.addWidget(self.voltage_spin)
        layout.addWidget(self.remove_btn)

class CalibrationDialog(QDialog):
    calibration_applied = pyqtSignal(float, float) # slope, intercept

    def __init__(self, peaks, parent=None):
        super().__init__(parent)
        self.peaks = peaks
        self.setWindowTitle("Calibration Mapping")
        self.resize(1200, 750)
        
        self.cards = []
        self.init_ui()
        self.update_plot()
        
    def init_ui(self):
        main_layout = QHBoxLayout(self)
        
        # Left Panel - Peak Cards
        left_panel = QVBoxLayout()
        title = QLabel("Map Peaks to Voltages")
        title.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        left_panel.addWidget(title)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        
        scroll_widget = QWidget()
        self.cards_layout = QVBoxLayout(scroll_widget)
        self.cards_layout.setSpacing(10)
        
        for i, peak in enumerate(self.peaks):
            card = PeakCard(i, peak)
            card.voltage_spin.valueChanged.connect(self.update_plot)
            card.remove_btn.clicked.connect(lambda _, c=card: self.remove_card(c))
            self.cards_layout.addWidget(card)
            self.cards.append(card)
            
        self.cards_layout.addStretch()
        scroll.setWidget(scroll_widget)
        left_panel.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        apply_btn = QPushButton("Apply Calibration")
        apply_btn.setStyleSheet("""
            QPushButton {
                background-color: #0d6efd;
                color: white;
                padding: 10px;
                border-radius: 5px;
                font-weight: bold;
                font-size: 12pt;
            }
            QPushButton:hover { background-color: #0b5ed7; }
        """)
        apply_btn.clicked.connect(self.apply_calibration)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                padding: 10px;
                border-radius: 5px;
                font-weight: bold;
                font-size: 12pt;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(apply_btn)
        left_panel.addLayout(btn_layout)
        
        # Right Panel - Plot
        right_panel = QVBoxLayout()
        self.plot_widget = pg.PlotWidget(background='w')
        self.plot_widget.setLabel('bottom', 'Channel')
        self.plot_widget.setLabel('left', 'Voltage', units='V')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        
        self.scatter = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None), brush=pg.mkBrush(0, 114, 210, 255))
        self.fit_line = pg.PlotDataItem(pen=pg.mkPen('r', width=2, style=Qt.PenStyle.DashLine))
        self.plot_widget.addItem(self.scatter)
        self.plot_widget.addItem(self.fit_line)
        
        self.equation_label = QLabel("Enter at least 2 non-zero voltages to fit.")
        self.equation_label.setFont(QFont("Arial", 12))
        self.equation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        right_panel.addWidget(self.equation_label)
        right_panel.addWidget(self.plot_widget)
        
        main_layout.addLayout(left_panel, 4)
        main_layout.addLayout(right_panel, 6)
        

    def remove_card(self, card):
        self.cards.remove(card)
        self.cards_layout.removeWidget(card)
        card.deleteLater()
        self.update_plot()
    def update_plot(self):
        channels = []
        voltages = []
        
        for card in self.cards:
            v = card.voltage_spin.value()
            if v > 0: # Only use non-zero entered voltages
                channels.append(card.mean_channel)
                voltages.append(v)
                
        self.scatter.setData(channels, voltages)
        
        if len(channels) >= 2:
            channels = np.array(channels)
            voltages = np.array(voltages)
            
            # V = m * Ch + q
            m, q = np.polyfit(channels, voltages, 1)
            
            # Create line points
            x_min, x_max = min(channels) * 0.9, max(channels) * 1.1
            x_fit = np.array([x_min, x_max])
            y_fit = m * x_fit + q
            self.fit_line.setData(x_fit, y_fit)
            
            # Calculate R^2
            y_pred = m * channels + q
            ss_res = np.sum((voltages - y_pred)**2)
            ss_tot = np.sum((voltages - np.mean(voltages))**2)
            r2 = 1 - (ss_res / ss_tot) if ss_tot != 0 else 1.0
            
            sign = "+" if q >= 0 else "-"
            self.equation_label.setText(f"Fit: V = {m:.4f} * Ch {sign} {abs(q):.4f} (R² = {r2:.4f})")
            self.equation_label.setStyleSheet("color: #198754; font-weight: bold;")
        else:
            self.fit_line.setData([], [])
            self.equation_label.setText("Enter at least 2 non-zero voltages to fit.")
            self.equation_label.setStyleSheet("color: #dc3545; font-weight: bold;")
            
    def apply_calibration(self):
        channels = []
        voltages = []
        for card in self.cards:
            v = card.voltage_spin.value()
            if v > 0:
                channels.append(card.mean_channel)
                voltages.append(v)
                
        if len(channels) < 2:
            QMessageBox.warning(self, "Not Enough Points", "Please enter voltages for at least 2 peaks to apply calibration.")
            return
            
        m, q = np.polyfit(channels, voltages, 1)
        self.calibration_applied.emit(m, q)
        self.accept()
