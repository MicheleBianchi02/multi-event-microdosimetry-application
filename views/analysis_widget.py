import logging

import numpy as np
import csv
import os
from services.analysis_service import AnalysisService
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QSlider,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Set global PyQtGraph theme to white background, black foreground
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


class AnalysisWidget(QWidget):
    finished = pyqtSignal()
    mode_changed_signal = pyqtSignal(str)
    plot_base_requested = pyqtSignal(str, str)
    calc_g_requested = pyqtSignal(dict)
    plot_int_requested = pyqtSignal(dict)
    plot_unc_requested = pyqtSignal(dict)
    export_requested = pyqtSignal(dict)
    custom_import_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.mode = "bragg"  # "bragg" or "sobp"
        self.unique_energies = []
        self.target_data = None
        self.vline = None
        self.max_z_overall = 0
        self.selected_keys = []
        self.current_max_z = 0
        self.q_0 = 0
        self.E_0 = 0
        self.custom_setups = []
        self.setup_ui()

    def set_initial_data(self, unique_energies):
        self.unique_energies = unique_energies
        self.cb_energy.clear()
        self.cb_energy.addItems([str(e) for e in self.unique_energies])

    def setup_ui(self):
        main_layout = QHBoxLayout(self)

        # ---------------- Left Panel: Controls ----------------
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFixedWidth(360)
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_scroll.setWidget(controls_widget)

        # --- Phase 0: Mode Selection ---
        gb_mode = QGroupBox("0. Analysis Mode")
        from PyQt6.QtWidgets import QGridLayout
        mode_layout = QGridLayout()
        self.rb_bragg = QRadioButton("Bragg Curve")
        self.rb_sobp = QRadioButton("SOBP")
        self.rb_custom = QRadioButton("Custom Setup")
        self.rb_bragg.setChecked(True)

        self.mode_group = QButtonGroup()
        self.mode_group.addButton(self.rb_bragg)
        self.mode_group.addButton(self.rb_sobp)
        self.mode_group.addButton(self.rb_custom)
        self.mode_group.buttonClicked.connect(self.on_mode_changed)

        mode_layout.addWidget(self.rb_bragg, 0, 0)
        mode_layout.addWidget(self.rb_sobp, 0, 1)
        mode_layout.addWidget(self.rb_custom, 1, 0, 1, 2)
        gb_mode.setLayout(mode_layout)
        controls_layout.addWidget(gb_mode)

        self.left_stack = QStackedWidget()
        self.page_standard = QWidget()
        self.layout_standard = QVBoxLayout(self.page_standard)
        self.layout_standard.setContentsMargins(0, 0, 0, 0)
        
        self.page_custom = QWidget()
        self.layout_custom = QVBoxLayout(self.page_custom)
        self.layout_custom.setContentsMargins(0, 0, 0, 0)
        
        self.left_stack.addWidget(self.page_standard)
        self.left_stack.addWidget(self.page_custom)
        controls_layout.addWidget(self.left_stack)

        # Build Custom Page
        self.build_custom_ui()

        # --- Phase 1: Base Parameters ---
        self.gb_base = QGroupBox("1. Initial Parameters")
        form_base = QFormLayout()

        self.cb_energy = QComboBox()
        self.cb_energy.addItems([str(e) for e in self.unique_energies])
        form_base.addRow("Beam Energy (MeV):", self.cb_energy)

        self.lbl_rate = QLabel("Rate (p/s):")
        self.le_rate = QLineEdit("1e7")
        form_base.addRow(self.lbl_rate, self.le_rate)

        self.le_chord = QLineEdit("0.75")
        form_base.addRow("Mean chord length (um):", self.le_chord)

        self.le_leakage = QLineEdit("3.0")
        form_base.addRow("Leakage current (pA):", self.le_leakage)

        self.le_nmin = QLineEdit("200")
        form_base.addRow("n_min:", self.le_nmin)

        self.btn_plot_base = QPushButton("Plot Energy & Fluence")
        self.btn_plot_base.setStyleSheet("font-weight: bold; padding: 5px;")
        self.btn_plot_base.clicked.connect(self.on_plot_base)
        form_base.addRow(self.btn_plot_base)

        self.gb_base.setLayout(form_base)
        self.layout_standard.addWidget(self.gb_base)

        # --- Phase 2: Z_max & Step Selection ---
        self.gb_step = QGroupBox("2. Step Parameter & Z Max")
        self.gb_step.setEnabled(False)
        form_step = QFormLayout()

        self.cb_step = QComboBox()
        form_step.addRow("s Parameter:", self.cb_step)

        self.slider_zmax = QSlider(Qt.Orientation.Horizontal)
        self.slider_zmax.setMinimum(0)
        self.slider_zmax.setMaximum(1000)
        self.slider_zmax.valueChanged.connect(self.on_slider_changed)

        self.le_zmax = QLineEdit("0.00")
        self.le_zmax.editingFinished.connect(self.on_le_zmax_changed)
        zmax_container = QWidget()
        zmax_layout = QHBoxLayout(zmax_container)
        zmax_layout.setContentsMargins(0, 0, 0, 0)
        zmax_layout.addWidget(self.le_zmax)
        zmax_layout.addWidget(QLabel("cm"))

        form_step.addRow("Select Max z:", self.slider_zmax)
        form_step.addRow("Current z:", zmax_container)

        self.chk_limit_x = QCheckBox("Limit plots X-axis to z_max")
        self.chk_limit_x.setChecked(False)
        self.chk_limit_x.stateChanged.connect(self.on_limit_x_changed)
        form_step.addRow(self.chk_limit_x)

        self.btn_calc_g = QPushButton("Calculate gas multiplication (g)")
        self.btn_calc_g.setStyleSheet("font-weight: bold; padding: 5px;")
        self.btn_calc_g.clicked.connect(self.on_calc_g)
        form_step.addRow(self.btn_calc_g)

        self.lbl_g_result = QLabel("")
        self.lbl_g_result.setStyleSheet("color: blue;")
        form_step.addRow(self.lbl_g_result)

        self.gb_step.setLayout(form_step)
        self.layout_standard.addWidget(self.gb_step)

        # --- Phase 3: Integration Configuration ---
        self.gb_int = QGroupBox("3. Integration Calculation")
        self.gb_int.setEnabled(False)
        form_int = QFormLayout()

        self.le_g_use = QLineEdit("")
        form_int.addRow("Gas mult. (g) to use:", self.le_g_use)

        self.le_c1 = QLineEdit("22")
        form_int.addRow("C_1 (pF):", self.le_c1)

        self.chk_c2 = QCheckBox("C_2 (pF):")
        self.le_c2 = QLineEdit("100")
        form_int.addRow(self.chk_c2, self.le_c2)

        self.chk_c3 = QCheckBox("C_3 (pF):")
        self.le_c3 = QLineEdit("500")
        form_int.addRow(self.chk_c3, self.le_c3)

        self.chk_c4 = QCheckBox("C_4 (pF):")
        self.le_c4 = QLineEdit("3000")
        form_int.addRow(self.chk_c4, self.le_c4)

        self.le_vmin = QLineEdit("0.001")
        form_int.addRow("V_min (V):", self.le_vmin)

        self.le_vmax = QLineEdit("5.0")
        form_int.addRow("V_max (V):", self.le_vmax)

        self.le_yb_min = QLineEdit("1.0")
        self.lbl_yb_min = QLabel("Min y_bar_D (keV/um):")
        form_int.addRow(self.lbl_yb_min, self.le_yb_min)

        self.le_yb_max = QLineEdit("10.0")
        self.lbl_yb_max = QLabel("Max y_bar_D (keV/um):")
        form_int.addRow(self.lbl_yb_max, self.le_yb_max)

        self.btn_plot_int = QPushButton("Calculate & Plot Integration")
        self.btn_plot_int.setStyleSheet("font-weight: bold; padding: 5px;")
        self.btn_plot_int.clicked.connect(self.on_plot_int)
        form_int.addRow(self.btn_plot_int)

        self.gb_int.setLayout(form_int)
        self.layout_standard.addWidget(self.gb_int)

        # --- Phase 4: Uncertainty Calculation ---
        self.gb_unc = QGroupBox("4. Uncertainty Calculation")
        self.gb_unc.setEnabled(False)
        form_unc = QFormLayout()

        self.le_tm_max = QLineEdit("300")
        form_unc.addRow("Maximum measuring time (s):", self.le_tm_max)

        self.le_epsilon = QLineEdit("0.01")
        form_unc.addRow("Relative uncertainty (ε):", self.le_epsilon)

        self.chk_custom_tint = QCheckBox("Custom T_int (ms):")
        self.le_custom_tint = QLineEdit("10.0")
        form_unc.addRow(self.chk_custom_tint, self.le_custom_tint)

        self.btn_plot_unc = QPushButton("Calculate & Plot Measuring Time")
        self.btn_plot_unc.setStyleSheet("font-weight: bold; padding: 5px;")
        self.btn_plot_unc.clicked.connect(self.on_plot_unc)
        form_unc.addRow(self.btn_plot_unc)

        self.gb_unc.setLayout(form_unc)
        self.layout_standard.addWidget(self.gb_unc)

        self.layout_standard.addStretch()

        self.btn_export = QPushButton("Export Data to CSV")
        self.btn_export.setStyleSheet(
            """
            QPushButton {
                background-color: #28a745; 
                color: white; 
                font-weight: bold; 
                padding: 10px 15px; 
                font-size: 12pt; 
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:disabled {
                background-color: #a5d8ad;
            }
            """
        )
        self.btn_export.clicked.connect(self.on_export_csv)
        self.btn_export.setEnabled(False)
        self.layout_standard.addWidget(self.btn_export)

        self.btn_finish = QPushButton("Proceed to Integration\nTime Selection ->")
        self.btn_finish.setStyleSheet(
            """
            QPushButton {
                background-color: #007bff; 
                color: white; 
                font-weight: bold; 
                padding: 10px 15px; 
                font-size: 12pt; 
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #0069d9;
            }
            """
        )
        self.btn_finish.clicked.connect(self.finished.emit)
        controls_layout.addWidget(self.btn_finish)

        # ---------------- Right Panel: PyQtGraph Plots ----------------
        plots_scroll = QScrollArea()
        plots_scroll.setWidgetResizable(True)
        plots_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        plots_scroll.setMinimumWidth(850)

        self.right_stack = QStackedWidget()
        
        # Standard Plots Page
        self.page_plots = QWidget()
        self.plots_layout = QVBoxLayout(self.page_plots)
        self.page_plots.setLayout(self.plots_layout)
        
        plots_scroll.setWidget(self.page_plots)
        
        # Custom Cards Page
        self.page_table = QWidget()
        self.table_layout = QVBoxLayout(self.page_table)
        
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setStyleSheet("QScrollArea { border: none; background-color: white; }")
        
        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background-color: white;")
        self.custom_cards_layout = QVBoxLayout(self.cards_container)
        self.custom_cards_layout.addStretch()
        self.cards_scroll.setWidget(self.cards_container)
        
        self.table_layout.addWidget(self.cards_scroll)
        
        self.right_stack.addWidget(plots_scroll)
        self.right_stack.addWidget(self.page_table)
        
        main_layout.addWidget(controls_scroll)
        main_layout.addWidget(self.right_stack, 1)


        # Plot 1: Energy, Fluence & LET (Dual Axis setup)
        self.plot_base = pg.PlotWidget(title="Normalized Energy/Fluence & LET vs Depth")
        self.plot_base.setMinimumHeight(500)
        self.plot_base.setLabel("left", "Normalized Value (a.u.)")
        self.plot_base.setLabel("bottom", "Depth (cm)")
        self.plot_base.showAxis("right")
        self.plot_base.setLabel("right", "LET (keV/µm)")
        self.plot_base.addLegend()

        self.vb_let = pg.ViewBox()
        self.plot_base.scene().addItem(self.vb_let)
        self.plot_base.getAxis("right").linkToView(self.vb_let)
        self.vb_let.setXLink(self.plot_base.plotItem.vb)

        def updateViews():
            self.vb_let.setGeometry(self.plot_base.plotItem.vb.sceneBoundingRect())
            self.vb_let.linkedViewChanged(self.plot_base.plotItem.vb, self.vb_let.XAxis)

        updateViews()
        self.plot_base.plotItem.vb.sigResized.connect(updateViews)
        self.plots_layout.addWidget(self.plot_base)

        # Plot 2: I(z)
        self.plot_i = pg.PlotWidget(title="Current I(z) vs Depth")
        self.plot_i.setMinimumHeight(500)
        self.plot_i.setLabel("left", "Current I(z) (pA)")
        self.plot_i.setLabel("bottom", "Depth (cm)")
        self.plot_i.addLegend()
        self.plots_layout.addWidget(self.plot_i)

        # Plot 3: T_int_min Contributions
        self.plot_tmin = pg.PlotWidget(title="T_int_min Contributions vs Depth")
        self.plot_tmin.setMinimumHeight(500)
        self.plot_tmin.setLabel("left", "Integration Time (s)")
        self.plot_tmin.setLabel("bottom", "Depth (cm)")
        self.plot_tmin.addLegend()
        self.plots_layout.addWidget(self.plot_tmin)

        # Plot 4: T_int_max Contributions
        self.plot_tmax = pg.PlotWidget(title="T_int_max Contributions vs Depth")
        self.plot_tmax.setMinimumHeight(500)
        self.plot_tmax.setLabel("left", "Integration Time (s)")
        self.plot_tmax.setLabel("bottom", "Depth (cm)")
        self.plot_tmax.addLegend()
        self.plots_layout.addWidget(self.plot_tmax)

        # Plot 5: Final T_int Bounds
        self.plot_tfinal = pg.PlotWidget(title="Final Integration Time Bounds vs Depth")
        self.plot_tfinal.setMinimumHeight(500)
        self.plot_tfinal.setLabel("left", "Integration Time (s)")
        self.plot_tfinal.setLabel("bottom", "Depth (cm)")
        self.plot_tfinal.addLegend()
        self.plots_layout.addWidget(self.plot_tfinal)

        # Plot 6: Measuring Time
        self.plot_tm = pg.PlotWidget(title="Measuring Time Bounds vs Depth")
        self.plot_tm.setMinimumHeight(500)
        self.plot_tm.setLogMode(y=True)
        self.plot_tm.setLabel("left", "Measuring Time (s)")
        self.plot_tm.setLabel("bottom", "Depth (cm)")
        self.plot_tm.addLegend()
        self.plots_layout.addWidget(self.plot_tm)

        # Plot 7: Voltage Analysis
        self.plot_v = pg.PlotWidget(title="Voltage Analysis vs Depth")
        self.plot_v.setMinimumHeight(500)
        self.plot_v.setLabel("left", "Voltage (V)")
        self.plot_v.setLabel("bottom", "Depth (cm)")
        self.plot_v.addLegend()
        self.plots_layout.addWidget(self.plot_v)




    def build_custom_ui(self):
        gb_custom = QGroupBox("Custom Parameters")
        form_custom = QFormLayout()

        self.le_c_energy = QLineEdit("62.0")
        form_custom.addRow("Beam Energy (MeV):", self.le_c_energy)

        self.le_c_beam_curr = QLineEdit("1.0")
        form_custom.addRow("Beam current (nA):", self.le_c_beam_curr)

        self.le_c_z = QLineEdit("0.0")
        form_custom.addRow("H2O Depth z (cm):", self.le_c_z)

        self.le_c_yf = QLineEdit("1.5")
        form_custom.addRow("<html><span style=\"text-decoration: overline\">y</span><sub>F</sub> (keV/µm):</html>", self.le_c_yf)

        self.le_c_yd = QLineEdit("3.0")
        form_custom.addRow("<html><span style=\"text-decoration: overline\">y</span><sub>D</sub> (keV/µm):</html>", self.le_c_yd)

        self.le_c_g = QLineEdit("1000")
        form_custom.addRow("Gas mult. (g):", self.le_c_g)

        # Rate or Current selection
        self.rb_c_rate = QRadioButton("Rate r (p/s)")
        self.rb_c_curr = QRadioButton("Current I (pA)")
        self.rb_c_rate.setChecked(True)
        rc_layout = QVBoxLayout()
        rc_layout.addWidget(self.rb_c_rate)
        rc_layout.addWidget(self.rb_c_curr)
        form_custom.addRow("Input type:", rc_layout)

        self.le_c_val = QLineEdit("1e7")
        form_custom.addRow("Value:", self.le_c_val)

        self.le_c_chord = QLineEdit("0.75")
        form_custom.addRow("<html>Mean chord <span style=\"text-decoration: overline\">l</span> (µm):</html>", self.le_c_chord)

        self.le_c_nmin = QLineEdit("200")
        form_custom.addRow("n_min:", self.le_c_nmin)

        self.le_c_cap = QLineEdit("100")
        form_custom.addRow("Capacitance (pF):", self.le_c_cap)

        self.le_c_vmin = QLineEdit("0.001")
        form_custom.addRow("V_min (V):", self.le_c_vmin)

        self.le_c_vmax = QLineEdit("5.0")
        form_custom.addRow("V_max (V):", self.le_c_vmax)

        self.le_c_tm_max = QLineEdit("300")
        form_custom.addRow("Max measuring time (s):", self.le_c_tm_max)

        self.le_c_eps = QLineEdit("0.01")
        form_custom.addRow("Relative uncertainty (eps):", self.le_c_eps)

        self.chk_c_tint = QCheckBox("Custom T_int (ms):")
        self.le_c_tint = QLineEdit("10.0")
        form_custom.addRow(self.chk_c_tint, self.le_c_tint)

        gb_custom.setLayout(form_custom)
        self.layout_custom.addWidget(gb_custom)

        btn_layout = QVBoxLayout()
        self.btn_add_custom = QPushButton("Add Custom Setup")
        self.btn_add_custom.setStyleSheet("font-weight: bold; padding: 5px; background-color: #28a745; color: white;")
        self.btn_add_custom.clicked.connect(self.on_add_custom)
        
        btn_layout.addWidget(self.btn_add_custom)
        self.layout_custom.addLayout(btn_layout)

        exp_imp_layout = QHBoxLayout()
        self.btn_exp_custom = QPushButton("Export to CSV")
        self.btn_exp_custom.clicked.connect(self.on_export_custom)
        self.btn_imp_custom = QPushButton("Import CSV")
        self.btn_imp_custom.clicked.connect(self.on_import_custom)
        exp_imp_layout.addWidget(self.btn_exp_custom)
        exp_imp_layout.addWidget(self.btn_imp_custom)
        self.layout_custom.addLayout(exp_imp_layout)

        self.layout_custom.addStretch()

    def on_add_custom(self):
        try:
            energy = float(self.le_c_energy.text())
            beam_curr = float(self.le_c_beam_curr.text())
            z = float(self.le_c_z.text())
            yf = float(self.le_c_yf.text())
            yd = float(self.le_c_yd.text())
            g = float(self.le_c_g.text())
            val = float(self.le_c_val.text())
            l = float(self.le_c_chord.text())
            n_min = float(self.le_c_nmin.text())
            cap = float(self.le_c_cap.text())
            vmin = float(self.le_c_vmin.text())
            vmax = float(self.le_c_vmax.text())
            tm_max = float(self.le_c_tm_max.text())
            eps = float(self.le_c_eps.text())
            c_tint = float(self.le_c_tint.text()) if self.chk_c_tint.isChecked() else 0.0
            is_rate = self.rb_c_rate.isChecked()
            use_custom_tint = self.chk_c_tint.isChecked()
        except ValueError:
            QMessageBox.warning(self, "Input Error", "Please ensure all custom parameters are valid numbers.")
            return

        from services.analysis_service import AnalysisService
        setup_data = AnalysisService.calculate_custom_setup(
            energy, beam_curr, z, yf, yd, g, val, is_rate, l, n_min, cap, vmin, vmax, tm_max, eps, c_tint, use_custom_tint
        )
        
        self.custom_setups.append(setup_data)
        self.update_custom_table()

    def on_remove_custom(self, index):
        if 0 <= index < len(self.custom_setups):
            del self.custom_setups[index]
            self.update_custom_table()

    def update_custom_table(self):
        # Clear the current layout
        while self.custom_cards_layout.count() > 1: # keep the stretch at the end
            item = self.custom_cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        # Re-add stretch if it was accidentally removed
        if self.custom_cards_layout.count() == 0:
            self.custom_cards_layout.addStretch()
            
        from PyQt6.QtWidgets import QFrame, QLabel, QGridLayout, QHBoxLayout, QPushButton
        
        for index, data in enumerate(self.custom_setups):
            card = QFrame()
            card.setStyleSheet("QFrame { border: 2px solid #ccc; border-radius: 8px; background-color: #f8f9fa; margin: 10px; } QLabel { border: none; background: transparent; }")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 10, 15, 15)
            
            # Header
            header_layout = QHBoxLayout()
            title = QLabel(f"<b>Custom Setup #{index + 1}</b>")
            title.setStyleSheet("font-size: 16pt; color: #2c3e50;")
            btn_remove = QPushButton("Remove Setup")
            btn_remove.setStyleSheet("background-color: #dc3545; color: white; font-weight: bold; padding: 5px 10px; border-radius: 4px; border: none;")
            btn_remove.setFixedWidth(120)
            btn_remove.clicked.connect(lambda checked, idx=index: self.on_remove_custom(idx))
            
            header_layout.addWidget(title)
            header_layout.addStretch()
            header_layout.addWidget(btn_remove)
            card_layout.addLayout(header_layout)
            
            # Body: Params and Results
            body_layout = QHBoxLayout()
            
            # Params
            gb_params = QGroupBox("Input Parameters")
            gb_params.setStyleSheet("QGroupBox { border: 1px solid #aaa; border-radius: 4px; margin-top: 2ex; font-weight: bold; font-size: 12pt; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }")
            grid_params = QGridLayout()
            grid_params.setContentsMargins(10, 15, 10, 10)
            params = [
                ("Energy (MeV)", f"{data['energy']:.2f}"),
                ("Beam current (nA)", f"{data['beam_curr']:.2f}"),
                ("z (cm)", f"{data['z']:.3f}"),
                ("<span style='text-decoration: overline'>y</span><sub>F</sub> (keV/µm)", f"{data['yf']:.3f}"),
                ("<span style='text-decoration: overline'>y</span><sub>D</sub> (keV/µm)", f"{data['yd']:.3f}"),
                ("g", f"{data['g']:.1f}"),
                ("Rate (p/s)", f"{data['rate']:.2e}"),
                ("Current (pA)", f"{data['current_pa']:.2f}"),
                ("<span style='text-decoration: overline'>l</span> (µm)", f"{data['l']:.2f}"),
                ("n_min", f"{data['n_min']:.0f}"),
                ("C (pF)", f"{data['cap']:.1f}"),
                ("V_min (V)", f"{data['vmin']:.3f}"),
                ("V_max (V)", f"{data['vmax']:.3f}"),
                ("T_exp (s)", f"{data['t_exp']:.4e}"),
            ]
            for i, (k, v) in enumerate(params):
                grid_params.addWidget(QLabel(f"{k}:"), i // 2, (i % 2) * 2)
                val_label = QLabel(v)
                val_label.setStyleSheet("color: #007bff; font-weight: normal;")
                grid_params.addWidget(val_label, i // 2, (i % 2) * 2 + 1)
            gb_params.setLayout(grid_params)
            
            # Results
            gb_results = QGroupBox("Calculated Timing & Voltage")
            gb_results.setStyleSheet("QGroupBox { border: 1px solid #aaa; border-radius: 4px; margin-top: 2ex; font-weight: bold; font-size: 12pt; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }")
            grid_results = QGridLayout()
            grid_results.setContentsMargins(10, 15, 10, 10)
            results = [
                ("T_int_min (s)", f"{data['t_int_min']:.4e}"),
                ("T_int_max (s)", f"{data['t_int_max']:.4e}"),
                ("T_m (s)", f"{data['t_m_min']:.4e}"),
                ("Mean V (V)", f"{data['mean_v']:.3f}"),
                ("std V (V)", f"{data['std_v']:.3f}"),
            ]
            for i, (k, v) in enumerate(results):
                grid_results.addWidget(QLabel(f"{k}:"), i, 0)
                val_label = QLabel(v)
                val_label.setStyleSheet("color: #28a745; font-weight: normal;")
                grid_results.addWidget(val_label, i, 1)
            gb_results.setLayout(grid_results)
            
            body_layout.addWidget(gb_params)
            body_layout.addWidget(gb_results)
            card_layout.addLayout(body_layout)
            
            self.custom_cards_layout.insertWidget(self.custom_cards_layout.count() - 1, card)


    def on_import_custom(self):
        from PyQt6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Custom Setups CSV", "", "CSV Files (*.csv)")
        if file_path:
            self.custom_import_requested.emit(file_path)

    def add_imported_setups(self, setups: list):
        self.custom_setups.extend(setups)
        self.update_custom_table()
    def on_export_custom(self):
        if not self.custom_setups:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "No Data", "No custom setups to export.")
            return
            
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Custom Setups CSV", "custom_setups.csv", "CSV Files (*.csv)")
        if not file_path:
            return
            
        try:
            from services.analysis_service import AnalysisService
            AnalysisService.export_custom_setups_to_csv(file_path, self.custom_setups)
            QMessageBox.information(self, "Export Successful", f"Data exported to {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to write CSV: {e}")

    def on_mode_changed(self, button):
        if button == self.rb_bragg:
            self.mode = "bragg"
            self.left_stack.setCurrentIndex(0)
            self.right_stack.setCurrentIndex(0)
            self.lbl_yb_min.show()
            self.le_yb_min.show()
            self.lbl_yb_max.show()
            self.le_yb_max.show()
            self.lbl_rate.setText("Rate (p/s):")
            self.le_rate.setText("1e7")
        elif button == self.rb_sobp:
            self.mode = "sobp"
            self.left_stack.setCurrentIndex(0)
            self.right_stack.setCurrentIndex(0)
            self.lbl_yb_min.hide()
            self.le_yb_min.hide()
            self.lbl_yb_max.hide()
            self.le_yb_max.hide()
            self.lbl_rate.setText("I_beam [nA]:")
            self.le_rate.setText("1.0")
        else:
            self.mode = "custom"
            self.left_stack.setCurrentIndex(1)
            self.right_stack.setCurrentIndex(1)


        # Reset UI flow
        self.gb_step.setEnabled(False)
        self.gb_int.setEnabled(False)
        self.gb_unc.setEnabled(False)
        self.btn_export.setEnabled(False)

        if hasattr(self, "phase3_results"):
            del self.phase3_results
        if hasattr(self, "phase4_results"):
            del self.phase4_results
        if hasattr(self, "target_data"):
            del self.target_data
        self.gb_unc.setEnabled(False)
        self.plot_base.clear()
        self.vb_let.clear()
        self.plot_i.clear()
        self.plot_tmin.clear()
        self.plot_tmax.clear()
        self.plot_tfinal.clear()
        self.plot_tm.clear()
        self.plot_v.clear()

        self.mode_changed_signal.emit(self.mode)

    def on_plot_base(self):
        energy_str = self.cb_energy.currentText()
        self.plot_base_requested.emit(energy_str, self.mode)

    def update_base_plots(self, selected_keys, plot_data, max_z_overall):
        self.selected_keys = selected_keys
        self.max_z_overall = max_z_overall

        self.cb_step.clear()
        steps = [plot_data[k]["step"] for k in self.selected_keys]
        self.cb_step.addItems(steps)
        if len(steps) == 1:
            self.cb_step.setEnabled(False)
        else:
            self.cb_step.setEnabled(True)

        self.slider_zmax.blockSignals(True)
        self.slider_zmax.setValue(500)
        self.slider_zmax.blockSignals(False)

        self.plot_base.clear()
        self.vb_let.clear()
        if self.plot_base.plotItem.legend is not None:
            self.plot_base.plotItem.legend.scene().removeItem(
                self.plot_base.plotItem.legend
            )
            self.plot_base.plotItem.legend = None
        self.plot_base.addLegend()

        colors = [
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
            "#9467bd",
            "#8c564b",
            "#e377c2",
        ]

        beam_energy = self.cb_energy.currentText()
        for i, key in enumerate(self.selected_keys):
            data = plot_data[key]
            c = pg.mkColor(colors[i % len(colors)])
            label_suffix = f" (s={data['step']})" if data["step"] else ""

            e_max = np.max(data["e"]) if np.max(data["e"]) > 0 else 1.0
            f_max = np.max(data["f"]) if np.max(data["f"]) > 0 else 1.0

            e_norm = data["e"] / e_max
            f_norm = data["f"] / f_max

            self.plot_base.plot(
                data["z"],
                e_norm,
                pen=pg.mkPen(color=c, width=2),
                name=f"Norm. Energy{label_suffix}",
            )
            self.plot_base.plot(
                data["z"],
                f_norm,
                pen=pg.mkPen(color=c, width=2, style=Qt.PenStyle.DashLine),
                name=f"Norm. Fluence{label_suffix}",
            )

            c_let = pg.PlotCurveItem(
                data["z"],
                data["let"],
                pen=pg.mkPen(color=c, width=2, style=Qt.PenStyle.DotLine),
            )
            self.vb_let.addItem(c_let)

            if self.plot_base.plotItem.legend is not None:
                self.plot_base.plotItem.legend.addItem(c_let, f"LET{label_suffix}")

        self.plot_base.getAxis("left").setLabel(f"Normalized Value [{beam_energy} MeV]")
        self.plot_base.showGrid(x=True, y=True, alpha=0.3)

        initial_z = (500 / 1000.0) * self.max_z_overall
        self.le_zmax.blockSignals(True)
        self.le_zmax.setText(f"{initial_z:.3f}")
        self.le_zmax.blockSignals(False)

        self.vline = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen("r", width=2, style=Qt.PenStyle.DashLine),
            label="Selected z_max",
            labelOpts={"position": 0.1, "color": "r", "movable": True},
        )
        self.vline.setValue(initial_z)
        self.plot_base.addItem(self.vline)

        self.gb_step.setEnabled(True)

    def on_slider_changed(self, value):
        if self.max_z_overall == 0:
            return

        current_z = (value / 1000.0) * self.max_z_overall
        self.le_zmax.blockSignals(True)
        self.le_zmax.setText(f"{current_z:.3f}")
        self.le_zmax.blockSignals(False)

        if self.vline is not None:
            self.vline.setValue(current_z)

    def on_le_zmax_changed(self):
        if self.max_z_overall == 0:
            return
        try:
            val = float(self.le_zmax.text())
        except ValueError:
            return

        val = max(0.0, min(val, self.max_z_overall))
        self.le_zmax.blockSignals(True)
        self.le_zmax.setText(f"{val:.3f}")
        self.le_zmax.blockSignals(False)

        slider_val = int((val / self.max_z_overall) * 1000)
        self.slider_zmax.blockSignals(True)
        self.slider_zmax.setValue(slider_val)
        self.slider_zmax.blockSignals(False)

        if self.vline is not None:
            self.vline.setValue(val)

    def on_calc_g(self):
        try:
            rate = float(self.le_rate.text())
            l = float(self.le_chord.text())
            i_leakage = float(self.le_leakage.text())
        except ValueError:
            QMessageBox.warning(
                self,
                "Input Error",
                "Please ensure parameters in Phase 1 are valid numbers.",
            )
            return

        s_param = self.cb_step.currentText()
        z_val = self.slider_zmax.value()
        current_max_z = (z_val / 1000.0) * self.max_z_overall

        params = {
            "s_param": s_param,
            "selected_keys": self.selected_keys,
            "mode": self.mode,
            "rate": rate,
            "l": l,
            "i_leakage": i_leakage,
            "current_max_z": current_max_z,
        }
        self.calc_g_requested.emit(params)

    def update_g_result(
        self,
        target_key,
        target_data,
        current_max_z,
        E_0,
        E_z_max,
        calculated_g,
        q_0,
        let_0,
        rate,
    ):
        self.target_data = target_data
        self.current_max_z = current_max_z
        self.E_0 = E_0
        self.q_0 = q_0

        res_text = (
            f"r(z=0): {rate} p/s\n"
            f"LET(z=0): {let_0 / 1000:.4f} keV/µm\n"
            f"q(z=0): {q_0 * 1e15:.4e} fC\n"
            f"Calculated gas mult. (g): {calculated_g:.6f}"
        )

        self.lbl_g_result.setText(res_text)
        self.le_g_use.setText(f"{calculated_g:.6f}")
        self.gb_int.setEnabled(True)

    def update_phase3_plots(self, phase3_results=None):
        if phase3_results is not None:
            self.phase3_results = phase3_results
            self.gb_unc.setEnabled(True)
            self.btn_export.setEnabled(True)

        if not hasattr(self, "phase3_results"):
            return

        res = self.phase3_results
        z_array = res["z_array"]

        if (
            self.chk_limit_x.isChecked()
            and hasattr(self, "current_max_z")
            and self.current_max_z > 0
        ):
            idx = np.searchsorted(z_array, self.current_max_z, side="right")
            sl = slice(0, idx)
        else:
            sl = slice(None)

        z_p = z_array[sl]
        if len(z_p) == 0:
            return
        I_z_p = res["I_z"][sl]
        T_int_min_z_p = res["T_int_min_z"][sl]
        T_int_min_comp1_p = res["T_int_min_comp1"][sl]
        T_int_min_comp2_p = res["T_int_min_comp2"][sl]
        T_int_max_min_z_p = res["T_int_max_min_z"][sl]
        T_int_max_max_z_p = res["T_int_max_max_z"][sl]
        T_int_max_comp1_p = res["T_int_max_comp1"][sl]
        T_int_max_comp2_min_p = res["T_int_max_comp2_min"][sl]
        T_int_max_comp2_max_p = res["T_int_max_comp2_max"][sl]
        T_exp_min_p = res["T_exp_min"][sl]
        T_exp_max_p = res["T_exp_max"][sl]

        self.plot_i.clear()
        self.plot_i.plot(
            z_p, I_z_p * 1e12, pen=pg.mkPen("#1f77b4", width=2), name="I(z) (pA)"
        )
        self.plot_i.showGrid(x=True, y=True, alpha=0.3)

        self.plot_tmin.clear()
        self.plot_tmin.setLogMode(y=True)
        self.plot_tmin.showGrid(x=True, y=True, alpha=0.3)
        self.plot_tmin.plot(
            z_p, T_int_min_z_p, pen=pg.mkPen("r", width=3), name="T_int_min (Final)"
        )
        self.plot_tmin.plot(
            z_p,
            T_int_min_comp1_p,
            pen=pg.mkPen("g", width=2, style=Qt.PenStyle.DashLine),
            name="n_min / I(z) * g * q(z)",
        )
        self.plot_tmin.plot(
            z_p,
            T_int_min_comp2_p,
            pen=pg.mkPen("b", width=2, style=Qt.PenStyle.DotLine),
            name="n_min / I(z) * g * q1",
        )

        self.plot_tmax.clear()
        self.plot_tmax.setLogMode(y=True)
        self.plot_tmax.showGrid(x=True, y=True, alpha=0.3)

        c_min = pg.PlotCurveItem(z_p, T_int_max_min_z_p)
        c_max = pg.PlotCurveItem(z_p, T_int_max_max_z_p)
        fill = pg.FillBetweenItem(c_min, c_max, brush=pg.mkBrush(255, 0, 255, 50))
        self.plot_tmax.addItem(fill)

        self.plot_tmax.plot(
            z_p,
            T_int_max_min_z_p,
            pen=pg.mkPen("m", width=2),
            name="T_int_max (min y_bar_D limit)",
        )
        self.plot_tmax.plot(
            z_p,
            T_int_max_max_z_p,
            pen=pg.mkPen("m", width=2),
            name="T_int_max (max y_bar_D limit)",
        )
        self.plot_tmax.plot(
            z_p,
            T_int_max_comp1_p,
            pen=pg.mkPen("#ff7f0e", width=2, style=Qt.PenStyle.DashLine),
            name="1/I(z) * Q_max",
        )
        self.plot_tmax.plot(
            z_p,
            T_exp_min_p,
            pen=pg.mkPen("#8B4513", width=2, style=Qt.PenStyle.DashDotLine),
            name="T_exp (min y_bar_D)",
        )
        self.plot_tmax.plot(
            z_p,
            T_exp_max_p,
            pen=pg.mkPen("#8B4513", width=2, style=Qt.PenStyle.DashDotLine),
            name="T_exp (max y_bar_D)",
        )
        self.plot_tmax.plot(
            z_p,
            T_int_max_comp2_min_p,
            pen=pg.mkPen("#7f7f7f", width=2, style=Qt.PenStyle.DotLine),
            name="g/I(z) * min_y_bar/20",
        )
        self.plot_tmax.plot(
            z_p,
            T_int_max_comp2_max_p,
            pen=pg.mkPen("#7f7f7f", width=2, style=Qt.PenStyle.DotLine),
            name="g/I(z) * max_y_bar/20",
        )

        self.plot_tfinal.clear()
        self.plot_tfinal.setLogMode(y=True)
        self.plot_tfinal.showGrid(x=True, y=True, alpha=0.3)

        c_min_fin = pg.PlotCurveItem(z_p, T_int_max_min_z_p)
        c_max_fin = pg.PlotCurveItem(z_p, T_int_max_max_z_p)
        fill_fin = pg.FillBetweenItem(
            c_min_fin, c_max_fin, brush=pg.mkBrush(0, 255, 0, 50)
        )
        self.plot_tfinal.addItem(fill_fin)

        self.plot_tfinal.plot(
            z_p, T_int_min_z_p, pen=pg.mkPen("r", width=3), name="T_int_min"
        )
        self.plot_tfinal.plot(
            z_p,
            T_int_max_comp1_p,
            pen=pg.mkPen("#ff7f0e", width=2, style=Qt.PenStyle.DashLine),
            name="T_int_max (Qmax/I)",
        )
        self.plot_tfinal.plot(
            z_p,
            T_int_max_min_z_p,
            pen=pg.mkPen("g", width=2),
            name="T_int_max (min limit)",
        )
        self.plot_tfinal.plot(
            z_p,
            T_int_max_max_z_p,
            pen=pg.mkPen("g", width=2),
            name="T_int_max (max limit)",
        )

    def update_phase4_plots(self, phase4_results=None):
        if phase4_results is not None:
            self.phase4_results = phase4_results

        if not hasattr(self, "phase4_results"):
            return

        res4 = self.phase4_results
        z_array = res4["z_array"]

        if (
            self.chk_limit_x.isChecked()
            and hasattr(self, "current_max_z")
            and self.current_max_z > 0
        ):
            idx = np.searchsorted(z_array, self.current_max_z, side="right")
            sl = slice(0, idx)
        else:
            sl = slice(None)

        z_p = z_array[sl]
        if len(z_p) == 0:
            return
        Tm_min_p = res4["Tm_min"][sl]
        Tm_max_p = res4["Tm_max"][sl]
        mean_V_p = res4["mean_V"][sl]
        std_V_min_p = res4["std_V_min"][sl]
        std_V_max_p = res4["std_V_max"][sl]

        self.plot_tm.clear()
        self.plot_tm.setLogMode(y=True)
        self.plot_tm.showGrid(x=True, y=True, alpha=0.3)

        c_min_tm = pg.PlotCurveItem(z_p, Tm_min_p)
        c_max_tm = pg.PlotCurveItem(z_p, Tm_max_p)
        fill_tm = pg.FillBetweenItem(
            c_min_tm, c_max_tm, brush=pg.mkBrush(0, 0, 255, 50)
        )
        self.plot_tm.addItem(fill_tm)

        self.plot_tm.plot(
            z_p, Tm_min_p, pen=pg.mkPen("b", width=2), name="T_m (min y_bar_D limit)"
        )
        self.plot_tm.plot(
            z_p, Tm_max_p, pen=pg.mkPen("b", width=2), name="T_m (max y_bar_D limit)"
        )

        y_min_val = np.min(Tm_min_p)
        y_max_val = np.max(Tm_max_p)
        if y_min_val > 0:
            if (y_max_val / y_min_val) < 2.0:
                mid = (np.log10(y_min_val) + np.log10(y_max_val)) / 2.0
                self.plot_tm.setYRange(mid - 1.5, mid + 1.5)
            else:
                self.plot_tm.enableAutoRange(axis="y")

        self.plot_v.clear()
        self.plot_v.showGrid(x=True, y=True, alpha=0.3)

        c_min_std = pg.PlotCurveItem(z_p, std_V_min_p)
        c_max_std = pg.PlotCurveItem(z_p, std_V_max_p)
        fill_std = pg.FillBetweenItem(
            c_min_std, c_max_std, brush=pg.mkBrush(255, 165, 0, 50)
        )
        self.plot_v.addItem(fill_std)

        self.plot_v.plot(
            z_p, mean_V_p, pen=pg.mkPen("k", width=2), name="Mean Voltage (V)"
        )
        self.plot_v.plot(
            z_p,
            std_V_min_p,
            pen=pg.mkPen("orange", width=2, style=Qt.PenStyle.DashLine),
            name="std_V (min y_bar_D limit)",
        )
        self.plot_v.plot(
            z_p,
            std_V_max_p,
            pen=pg.mkPen("orange", width=2, style=Qt.PenStyle.DashLine),
            name="std_V (max y_bar_D limit)",
        )

        v_min_plt = np.min(std_V_min_p)
        v_max_plt = np.max(mean_V_p)
        if v_min_plt > 0:
            if (v_max_plt / v_min_plt) < 2.0:
                mid_v = (v_min_plt + v_max_plt) / 2.0
                self.plot_v.setYRange(mid_v * 0.5, mid_v * 1.5)
            else:
                self.plot_v.enableAutoRange(axis="y")

    def on_limit_x_changed(self):
        self.update_phase3_plots()
        self.update_phase4_plots()

    def on_plot_int(self):
        try:
            g_use = float(self.le_g_use.text())
            c1 = float(self.le_c1.text())
            c2 = float(self.le_c2.text()) if self.chk_c2.isChecked() else 0.0
            c3 = float(self.le_c3.text()) if self.chk_c3.isChecked() else 0.0
            c4 = float(self.le_c4.text()) if self.chk_c4.isChecked() else 0.0
            v_min = float(self.le_vmin.text())
            v_max = float(self.le_vmax.text())

            c_tot = c1 + c2 + c3 + c4
            Q_min_C = (c_tot * 1e-12) * v_min
            Q_max_C = (c_tot * 1e-12) * v_max

            if self.mode == "bragg":
                y_bar_D_min_keV = float(self.le_yb_min.text())
                y_bar_D_max_keV = float(self.le_yb_max.text())
            else:
                y_bar_D_min_keV = 0.0
                y_bar_D_max_keV = 0.0

            rate = float(self.le_rate.text())
            n_min = float(self.le_nmin.text())
            l = float(self.le_chord.text())
        except ValueError:
            QMessageBox.warning(
                self,
                "Input Error",
                "Please ensure all parameters in Phase 3 are valid numbers.",
            )
            return

        params = {
            "target_data": self.target_data,
            "mode": self.mode,
            "g_use": g_use,
            "Q_min_C": Q_min_C,
            "Q_max_C": Q_max_C,
            "y_bar_D_min_keV": y_bar_D_min_keV,
            "y_bar_D_max_keV": y_bar_D_max_keV,
            "rate": rate,
            "n_min": n_min,
            "l": l,
            "q_0": self.q_0,
            "E_0": self.E_0,
            "c_tot": c_tot,
        }
        self.plot_int_requested.emit(params)

    def on_plot_unc(self):
        try:
            tm_max = float(self.le_tm_max.text())
            epsilon = float(self.le_epsilon.text())
            custom_tint_ms = float(self.le_custom_tint.text())
        except ValueError:
            QMessageBox.warning(
                self,
                "Input Error",
                "Please ensure parameters in Phase 4 are valid numbers.",
            )
            return

        if not hasattr(self, "phase3_results"):
            QMessageBox.warning(
                self, "Calculation Error", "Please calculate integration times first."
            )
            return

        params = {
            "phase3_results": self.phase3_results,
            "tm_max": tm_max,
            "epsilon": epsilon,
            "custom_tint_ms": custom_tint_ms,
            "chk_custom_tint": self.chk_custom_tint.isChecked(),
        }
        self.plot_unc_requested.emit(params)

    def on_export_csv(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Directory to Save CSV Files"
        )
        if not folder:
            return

        z = self.phase3_results["z_array"]
        e = self.target_data["e"]
        f = self.target_data["f"]
        let = self.target_data["let"]

        e_max = np.max(e) if np.max(e) > 0 else 1.0
        f_max = np.max(f) if np.max(f) > 0 else 1.0
        e_norm = e / e_max
        f_norm = f / f_max

        data_dict = {
            "Plot1_Energy_Fluence_LET": [
                ("Depth (cm)", z),
                ("Norm_Energy", e_norm),
                ("Norm_Fluence", f_norm),
                ("LET (keV/um)", let),
            ],
            "Plot2_Current": [
                ("Depth (cm)", z),
                ("I_z (pA)", self.phase3_results["I_z"] * 1e12),
            ],
            "Plot3_Tint_min": [
                ("Depth (cm)", z),
                ("T_int_min_Final (s)", self.phase3_results["T_int_min_z"]),
                ("n_min_I_g_qz", self.phase3_results["T_int_min_comp1"]),
                ("n_min_I_g_q1", self.phase3_results["T_int_min_comp2"]),
            ],
            "Plot4_Tint_max": [
                ("Depth (cm)", z),
                ("T_int_max_min_ybar (s)", self.phase3_results["T_int_max_min_z"]),
                ("T_int_max_max_ybar (s)", self.phase3_results["T_int_max_max_z"]),
                ("1_I_Qmax", self.phase3_results["T_int_max_comp1"]),
                ("1_I_min_ybar_20", self.phase3_results["T_int_max_comp2_min"]),
                ("1_I_max_ybar_20", self.phase3_results["T_int_max_comp2_max"]),
                ("T_exp_min_ybar", self.phase3_results["T_exp_min"]),
                ("T_exp_max_ybar", self.phase3_results["T_exp_max"]),
            ],
            "Plot5_Final_Bounds": [
                ("Depth (cm)", z),
                ("T_int_min (s)", self.phase3_results["T_int_min_z"]),
                ("T_int_max_min_limit (s)", self.phase3_results["T_int_max_min_z"]),
                ("T_int_max_max_limit (s)", self.phase3_results["T_int_max_max_z"]),
                ("T_int_max_Qmax_I (s)", self.phase3_results["T_int_max_comp1"]),
            ],
        }

        if hasattr(self, "phase4_results"):
            data_dict["Plot6_Measuring_Time"] = [
                ("Depth (cm)", z),
                ("Tm_min (s)", self.phase4_results["Tm_min"]),
                ("Tm_max (s)", self.phase4_results["Tm_max"]),
            ]
            data_dict["Plot7_Voltage_Analysis"] = [
                ("Depth (cm)", z),
                ("Mean_Voltage (V)", self.phase4_results["mean_V"]),
                ("std_V_min (V)", self.phase4_results["std_V_min"]),
                ("std_V_max (V)", self.phase4_results["std_V_max"]),
            ]

        self.export_requested.emit({"folder": folder, "data_dict": data_dict})
