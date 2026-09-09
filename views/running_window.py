import logging
import os
import re

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QLogValueAxis,
    QValueAxis,
)
from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.constants import NUM_BINS
from views.info_dialog import InfoDialog

logger = logging.getLogger(__name__)

# TODO: Aggiungere tab dove si mostrano piu spettri sovrapposti. Utile per confrontare
# diverse misure oppure visualizzare simulazioni


class InteractiveChartView(QChartView):
    def __init__(self, chart, parent=None):
        super().__init__(chart, parent)
        self.setRubberBand(QChartView.RubberBand.RectangleRubberBand)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.chart().zoomReset()
        super().mouseReleaseEvent(event)


class RunningWindow(QWidget):
    restart_signal = pyqtSignal()
    start_measurement_requested = pyqtSignal(bool)
    stop_measurement_requested = pyqtSignal()
    calculate_results_requested = pyqtSignal(int, int, bool, int, bool, str, str, int)
    recalculate_state_requested = pyqtSignal()
    save_data_requested = pyqtSignal(str)
    load_data_requested = pyqtSignal(str)

    @property
    def is_running(self):
        return self.start_bnt.text() == "STOP"

    @property
    def already_runned(self):
        return self.data_model is not None and self.data_model.count > 0

    def __init__(
        self, session_model, data_model, simulate: bool, hardware_manager=None
    ) -> None:
        super().__init__()
        self.session = session_model
        self.data_model = data_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        self.rate = None
        self.init_variables()
        self.time_chart_type = "moving"
        self.init_ui()

    def init_variables(self):
        self.chart_type = "mc"
        self.central_widget_type = "plot"
        self.already_saved = False
        self.n_point_moving_win = 10_000
        self.starting_time = 0
        self.time_roi_regions = {}
        self.channel_roi_regions = {}
        self.calib_line = None
        self.calib_scatter = None
        self.detected_peaks = []

    def clear_plot(self):
        """Clears the underlying data model and refreshes the plots/dashboard to wipe previous state."""
        self.data_model.clear()
        self.update_dashboard()

        # Reset result UI
        for macro in self.data_model.result.values():
            for param in macro:
                macro[param] = [0, 0, 0, 0]
        self.refresh_result_ui()

    def init_ui(self):
        # --- Common Styles & Icons ---
        self.icon_start = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.icon_arm = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxWarning
        )
        self.icon_stop = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        self.icon_restart = self.style().standardIcon(
            QStyle.StandardPixmap.SP_BrowserReload
        )
        self.icon_save = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogSaveButton
        )
        self.icon_load = self.style().standardIcon(
            QStyle.StandardPixmap.SP_DialogOpenButton
        )

        self.setting_tab = RunSettingTab(self.session, self.hardware_manager)
        self.setting_tab.update_signal.connect(self.on_update_setting)
        self.setting_tab.calib_mode_btn.toggled.connect(self.on_calib_toggled)
        self.setting_tab.calib_extract_btn.clicked.connect(self.on_calib_extract)
        self.setting_tab.time_roi_added.connect(self.on_time_roi_added)
        self.setting_tab.time_roi_removed.connect(self.on_time_roi_removed)
        self.setting_tab.apply_time_roi_button.clicked.connect(
            self.hide_time_roi_regions
        )
        self.setting_tab.channel_roi_added.connect(self.on_channel_roi_added)
        self.setting_tab.channel_roi_removed.connect(self.on_channel_roi_removed)
        self.setting_tab.apply_roi_button.clicked.connect(self.hide_channel_roi_regions)

        # --- MenuBar ---
        self.menu_bar = QMenuBar()
        self.options_menu = self.menu_bar.addMenu("File")

        self.save_file_action = QAction(self.icon_save, "Save As...", self)
        self.options_menu.addAction(self.save_file_action)
        self.save_file_action.triggered.connect(lambda: self.save_data(save_as=True))

        self.load_action = QAction(self.icon_load, "Load Data", self)
        self.options_menu.addAction(self.load_action)
        self.load_action.triggered.connect(self.on_load_press)

        self.view_menu = self.menu_bar.addMenu("View")
        self.show_setting = QAction("Show Settings Panel", self)
        self.show_setting.setCheckable(True)
        self.show_setting.setChecked(True)
        self.show_setting.toggled.connect(self.toggle_settings_panel)
        self.view_menu.addAction(self.show_setting)

        self.show_result_checkbox = QAction("Show Results Panel")
        self.show_result_checkbox.setCheckable(True)
        self.show_result_checkbox.setChecked(True)
        self.show_result_checkbox.toggled.connect(
            lambda state: self.result_table.setVisible(state)
        )
        self.view_menu.addAction(self.show_result_checkbox)

        self.view_menu.addSeparator()

        self.view_plt_points = QAction("Show points")
        self.view_plt_points.setCheckable(True)
        self.view_plt_points.setChecked(False)
        self.view_plt_points.toggled.connect(self.show_points_time_plt)
        self.view_menu.addAction(self.view_plt_points)

        self.show_plt_log_y = QAction("Log scale")
        self.show_plt_log_y.setCheckable(True)
        self.show_plt_log_y.setChecked(False)
        self.show_plt_log_y.toggled.connect(self.show_mc)
        self.view_menu.addAction(self.show_plt_log_y)

        self.view_menu.addSeparator()

        self.show_plots_ch1_action = QAction("Show CH1 Plots")
        self.show_plots_ch1_action.setCheckable(True)
        self.show_plots_ch1_action.setChecked(True)
        self.show_plots_ch1_action.toggled.connect(self.toggle_ch1_plots)
        self.view_menu.addAction(self.show_plots_ch1_action)

        self.show_plots_ch2_action = QAction("Show CH2 Plots")
        self.show_plots_ch2_action.setCheckable(True)
        self.show_plots_ch2_action.setChecked(True)
        self.show_plots_ch2_action.toggled.connect(self.toggle_ch2_plots)
        self.view_menu.addAction(self.show_plots_ch2_action)

        # --- Top Toolbar ---
        self.toolbar = QFrame()
        self.toolbar.setStyleSheet("""
            QFrame { 
                /* background-color: #f5f5f5; border-bottom: 1px solid #d0d0d0; */
                background-color: palette(Base);
                border-bottom: 1px solid palette(Mid);
                max-height: 60px;
            }
        """)
        toolbar_layout = QHBoxLayout(self.toolbar)
        toolbar_layout.setContentsMargins(10, 5, 10, 5)

        # Controls
        self.start_bnt = QPushButton("Start Measure")
        # Initial style setup (will be overwritten by change_start_bnt)
        self.start_bnt.setIcon(self.icon_start)
        self.start_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_bnt.clicked.connect(self.on_start_stop_press)

        self.change_start_bnt("start", hide=False, enable=True)
        self.change_start_bnt("arm", hide=False, enable=True)

        self.restart_bnt = QPushButton()
        self.restart_bnt.setIcon(self.icon_restart)
        self.restart_bnt.setToolTip("Restart")
        self.restart_bnt.clicked.connect(self.on_restart_press)
        self.restart_bnt.setStyleSheet("padding: 8px;")

        # Metrics Labels
        self.measuring_time_label = QLabel("0.0 s")
        self.measuring_time_label.setStyleSheet(
            "font-size: 16pt; font-weight: bold; color: palette(text); margin-left: 10px;"
        )

        # Info Badge Widget
        self.info_widget = QWidget()
        info_layout = QHBoxLayout(self.info_widget)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(10)

        def create_badge(label, value_widget):
            container = QFrame()
            # Removed vertical padding to make it smaller
            container.setStyleSheet(
                "background: #dfe3e8; border: none; border-radius: 4px;"
            )
            lay = QHBoxLayout(container)
            lay.setContentsMargins(8, 2, 8, 2)
            lay.setSpacing(5)
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #666; font-size: 12pt; font-weight: bold;")
            val = value_widget
            val.setStyleSheet("color: #2f3b45; font-size: 12pt;")
            lay.addWidget(lbl)
            lay.addWidget(val)
            return container

        self.int_time_label = QLabel("-")
        self.rst_time_label = QLabel("-")
        self.sample_freq_label = QLabel("-")

        info_layout.addWidget(create_badge("INTEGRATION:", self.int_time_label))
        info_layout.addWidget(create_badge("RESET:", self.rst_time_label))
        info_layout.addWidget(create_badge("FREQUENCY:", self.sample_freq_label))

        self.edit_info_btn = QPushButton("Edit Info")
        self.edit_info_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c757d; color: white; padding: 6px 12px;
                border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        self.edit_info_btn.clicked.connect(self.on_edit_info)
        info_layout.addWidget(self.edit_info_btn)

        toolbar_layout.addWidget(self.start_bnt)
        toolbar_layout.addSpacing(10)
        toolbar_layout.addWidget(self.restart_bnt)
        toolbar_layout.addSpacing(20)
        toolbar_layout.addWidget(QLabel("Time:"))
        toolbar_layout.addWidget(self.measuring_time_label)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self.info_widget)

        # --- Chart Control Strip ---
        self.chart_controls = QFrame()
        cc_layout = QHBoxLayout(self.chart_controls)
        cc_layout.setContentsMargins(0, 5, 0, 5)

        self.view_result_bnt = QPushButton("Results")
        self.view_result_bnt.setCheckable(True)
        self.view_result_bnt.setChecked(True)
        self.view_result_bnt.clicked.connect(self.on_show_results_press)
        self.view_result_bnt.setFixedWidth(100)

        self.view_hist_btn = QPushButton("Histogram")
        self.view_hist_btn.setCheckable(True)
        self.view_hist_btn.setChecked(True)
        self.view_hist_btn.clicked.connect(self.on_show_hist)
        self.view_hist_btn.setFixedWidth(120)

        self.view_time_btn = QPushButton("Time Series")
        self.view_time_btn.setCheckable(True)
        self.view_time_btn.clicked.connect(self.on_show_time)
        self.view_time_btn.setFixedWidth(120)

        self.update_view_buttons()

        # Time Window Controls
        self.moving_widget = QWidget()
        mw_layout = QHBoxLayout(self.moving_widget)
        mw_layout.setContentsMargins(0, 0, 0, 0)
        mw_layout.addWidget(QLabel("Window [s]:"))

        self.n_point_moving_line = QDoubleSpinBox()
        self.n_point_moving_line.setDecimals(1)
        self.n_point_moving_line.setRange(0, 1_000_000)
        # self.n_point_moving_line.setValue(10)
        self.n_point_moving_line.valueChanged.connect(self.on_change_n_point_moving)
        self.n_point_moving_line.setFixedWidth(100)
        mw_layout.addWidget(self.n_point_moving_line)
        self.moving_widget.setVisible(False)

        self.chart_time_mode_box = QComboBox()
        self.chart_time_mode_box.addItems(["Moving Window", "Full History"])
        self.chart_time_mode_box.currentIndexChanged.connect(
            self.on_change_time_chart_type
        )
        self.chart_time_mode_box.setVisible(False)
        for region in self.time_roi_regions.values():
            region.setVisible(False)

        self.memory_usage_widget = QWidget()
        memory_layout = QHBoxLayout(self.memory_usage_widget)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        info_btn = QToolButton()
        info_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
        info_btn.setAutoRaise(True)  # shows only on hover
        info_btn.setToolTip("Memory usage details")
        info_btn.clicked.connect(self.on_mem_info_press)
        memory_layout.addWidget(info_btn)

        memory_layout.addWidget(QLabel("Memory [MB]"), stretch=4)
        self.memory_label = QLabel(None)
        memory_layout.addWidget(self.memory_label, stretch=4)

        cc_layout.addWidget(self.view_result_bnt)
        cc_layout.addSpacing(10)
        cc_layout.addWidget(self.view_hist_btn)
        cc_layout.addWidget(self.view_time_btn)
        cc_layout.addSpacing(20)
        cc_layout.addWidget(self.chart_time_mode_box)
        cc_layout.addWidget(self.moving_widget)
        cc_layout.addStretch(1)
        cc_layout.addWidget(self.memory_usage_widget)

        # --- Plot Widget ---
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.legend = self.plot_widget.addLegend()
        self.legend.anchor(itemPos=(0, 1), parentPos=(0, 1), offset=(10, -10))
        self.legend.setBrush(pg.mkBrush(255, 255, 255, 255))
        self.legend.setPen("k")
        self.legend.setLabelTextColor("k")

        self.init_plot_items()

        # --- Results Table ---
        self.result_table = QTableWidget()
        self.setup_result_table()

        self.setup_window()

        self.show_points_time_plt()

    def update_view_buttons(self):
        # New active purple style with all corners rounded
        result_active = """
            QPushButton { 
                background-color: #6f42c1;
                color: white;
                border: 1px solid #5a32a3;
                border-radius: 4px;
                padding: 5px 20px;
                font-weight: bold;
            }
        """

        active_style = """
            QPushButton { 
                background-color: #0078d7;
                color: white;
                border: 1px solid #005a9e;
                border-radius: 0px;
                padding: 5px 20px;
                font-weight: bold;
            }
        """
        inactive_style = """
            QPushButton { 
                background-color: #f8f9fa;
                color: #333;
                border: 1px solid #ccc;
                border-radius: 0px;
                padding: 5px 20px;
            }
            QPushButton:hover { background-color: #e2e6ea; }
        """

        left_active = (
            active_style
            + "QPushButton { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }"
        )
        left_inactive = (
            inactive_style
            + "QPushButton { border-top-left-radius: 4px; border-bottom-left-radius: 4px; }"
        )
        right_active = (
            active_style
            + "QPushButton { border-top-right-radius: 4px; border-bottom-right-radius: 4px; border-left: none; }"
        )
        right_inactive = (
            inactive_style
            + "QPushButton { border-top-right-radius: 4px; border-bottom-right-radius: 4px;}"
        )

        result_inactive = inactive_style + "QPushButton { border-radius: 4px; }"

        if self.central_widget_type == "result":
            self.view_result_bnt.setStyleSheet(result_active)
            self.view_result_bnt.setChecked(True)

            self.view_time_btn.setStyleSheet(right_inactive)
            self.view_hist_btn.setStyleSheet(left_inactive)
            self.view_time_btn.setChecked(False)
            self.view_hist_btn.setChecked(False)
        else:
            self.view_result_bnt.setChecked(False)
            self.view_result_bnt.setStyleSheet(result_inactive)

            if self.chart_type == "mc":
                self.view_hist_btn.setStyleSheet(left_active)
                self.view_time_btn.setStyleSheet(right_inactive)
                self.view_hist_btn.setChecked(True)
                self.view_time_btn.setChecked(False)
            else:
                self.view_hist_btn.setStyleSheet(left_inactive)
                self.view_time_btn.setStyleSheet(right_active)
                self.view_hist_btn.setChecked(False)
                self.view_time_btn.setChecked(True)

    def init_plot_items(self):
        self.mca_x_axis = np.arange(NUM_BINS + 1)
        self.mca_item_ch1 = pg.PlotCurveItem(
            self.mca_x_axis,
            np.zeros(NUM_BINS),
            pen=pg.mkPen(color="#0078d7", width=2),
            brush=pg.mkBrush(color="#0078d7"),
            stepMode=True,
            fillLevel=0,
            name="CH 1",
        )
        self.mca_item_ch2 = pg.PlotCurveItem(
            self.mca_x_axis,
            np.zeros(NUM_BINS),
            pen=pg.mkPen(color="#d9534f", width=2),
            brush=pg.mkBrush(color="#d9534f"),
            stepMode=True,
            fillLevel=0,
            name="CH 2",
        )

        self.line_item_ch1 = pg.PlotDataItem(
            pen=pg.mkPen("#0078d7", width=1),
            name="CH 1",
            symbolSize=4,
            connect="finite",
        )
        self.line_item_ch2 = pg.PlotDataItem(
            pen=pg.mkPen("#d9534f", width=1),
            name="CH 2",
            symbolSize=4,
            connect="finite",
        )

        self.start_line_item_ch1 = pg.PlotDataItem(
            pen=pg.mkPen(color=(0, 0, 139), width=1),
            name="Start CH1",
            symbolSize=4,
            connect="finite",
        )  # darker blue
        self.stop_line_item_ch1 = pg.PlotDataItem(
            pen=pg.mkPen(color=(135, 206, 250), width=1),
            name="Stop CH1",
            symbolSize=4,
            connect="finite",
        )  # cyan
        self.start_line_item_ch2 = pg.PlotDataItem(
            pen=pg.mkPen(color="g", width=1),
            name="Start CH2",
            symbolSize=4,
            connect="finite",
        )
        self.stop_line_item_ch2 = pg.PlotDataItem(
            pen=pg.mkPen(color="m", width=1),
            name="Stop CH2",
            symbolSize=4,
            connect="finite",
        )

        self.nan_marker_item_ch1 = None
        self.nan_marker_item_ch2 = None

        self.init_mc_chart()

    def setup_result_table(self):
        columns = [
            "",
            "Mean [Ch]",
            "Mean [pC]",
            "Var [Ch²]",
            "Var [pC²]",
            "StdDev [Ch]",
            "StdDev [pC]",
            "Covariance [Ch²]",
            "Count",
            "Errors",
        ]
        self.result_table.setColumnCount(len(columns))
        self.result_table.setRowCount(2)
        self.result_table.setHorizontalHeaderLabels(columns)
        self.result_table.verticalHeader().hide()

        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.result_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.result_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.result_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        self.result_table.setShowGrid(True)
        self.result_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.result_table.setStyleSheet("""
            QTableWidget {
                    background-color: palette(Base);
                    alternate-background-color: palette(AlternateBase);
                    gridline-color: palette(Midlight);
                    border: 1px solid palette(Mid);
                    border-radius: 9px;
                    font-size: 12pt;
                }
            QTableWidget::item {
                border-color: palette(Dark);
                padding-left: 5px;
                padding-right: 5px;
                padding-top: 2px;
                padding-bottom: 2px;
                
                /* Removes the dotted focus line */
                outline: 0;
            }

            QTableWidget::item:selected {
                background-color: palette(Highlight);
                color: palette(HighlightedText);
            }

            QHeaderView::section {
                background-color: palette(Button);
                color: palette(ButtonText);
                padding: 5px;
                font-weight: bold;
                border: none;
                
            }

            QTableCornerButton::section {
                background-color: palette(Button);
                border: none;
            }
        """)

        self.result_table.setItem(0, 0, QTableWidgetItem("CH 1"))
        self.result_table.setItem(1, 0, QTableWidgetItem("CH 2"))
        for c in range(1, len(columns)):
            item1 = QTableWidgetItem("0")
            item1.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item2 = QTableWidgetItem("0")
            item2.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.result_table.setItem(0, c, item1)
            self.result_table.setItem(1, c, item2)

        self.result_table.setFixedHeight(110)

    def setup_window(self):
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setLayout(main_layout)

        self.plot_central_widget = QWidget()
        plt_central_layout = QVBoxLayout(self.plot_central_widget)
        plt_central_layout.addWidget(self.plot_widget)

        self.result_central_widget = QWidget()
        self.result_central_widget.setVisible(False)

        self.setup_result_widget()

        # TODO: FARE IL CALCOLO SIA CON METODO VARIANZA SIA CON VARIANZA-COVARIANZA.
        # PER VARIANZA, SE COV=0 SIGNIFICA CHE I SAMPLE SONO INDIPENDENTI => PROPAGAZIONE
        # DELL'INCERTEZZA (ANALITICO). PER VARIANZA COVARIANZA USARE BLOCK BOOTSTRAP
        # CON POLITIS_WHITE (METTERE CHE SI PUO COMUNQUE SELEZIONARE IL VALORE DEL
        # BLOCK).
        # METTERE FFT, MOVING AVARAGE E MOVING VARIANCE (E COVARIANZA SE SERVE)
        # ACF E PACF (LAG METTERLO IN SECONDI).
        # MOVING AVG METTERE WINDOW SELEZIONABILE.

        main_layout.addWidget(self.menu_bar)
        main_layout.addWidget(self.toolbar)

        central_layout = QHBoxLayout()

        # --- left side (Controls + Plot + Table) ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 5, 10, 5)

        left_layout.addWidget(self.chart_controls)
        left_layout.addWidget(self.plot_central_widget, stretch=10)
        left_layout.addWidget(self.result_central_widget, stretch=10)
        # left_layout.addWidget(self.plot_widget, stretch=10)
        left_layout.addWidget(self.result_table, stretch=0)

        central_layout.addWidget(left_widget, stretch=10)

        # --- right side (Settings only) ---
        right_widget = QWidget()
        right_widget.setFixedWidth(315)
        self.right_widget = right_widget
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 5, 5, 5)

        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.addTab(self.setting_tab, "Settings")

        right_layout.addWidget(self.sidebar_tabs)

        central_layout.addWidget(right_widget)

        main_layout.addLayout(central_layout, stretch=10)

    # =======================================================
    # result UI
    # =======================================================

    def setup_result_widget(self):
        # --- Main Layout & Scroll Area ---
        main_layout = QHBoxLayout(self.result_central_widget)

        self.ui_labels = {}

        # Scroll Area Setup
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        # Force the contents to stay at the top and in the center
        container_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        content_wrapper = QWidget()
        # Prevents looking stretched on wide screens
        content_wrapper.setMaximumWidth(650)
        content_wrapper.setMinimumWidth(500)
        self.wrapper_layout = QVBoxLayout(content_wrapper)
        self.wrapper_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.wrapper_layout.setSpacing(20)

        bnt_row = QHBoxLayout()
        self.wrapper_layout.addLayout(bnt_row)

        bnt_row.addStretch()

        update_bnt = QPushButton("Update Results")
        update_bnt.setCursor(Qt.CursorShape.PointingHandCursor)
        update_bnt.setStyleSheet("""
            QPushButton { 
                background-color: #0078d7;
                color: white;
                border: 1px solid #005a9e;
                border-radius: 4px;
                padding: 6px 20px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #005a9e; }
        """)
        update_bnt.clicked.connect(self.update_results)
        bnt_row.addWidget(update_bnt)

        bnt_row.addStretch()

        self.var_cov_bootstrap_lbl = QLabel("Number of Bootstraps:")
        self.var_cov_n_bootstrap = QLabel("-")

        self.var_cov_bootstrap_layout = QHBoxLayout()
        self.var_cov_bootstrap_layout.addWidget(self.var_cov_bootstrap_lbl)
        self.var_cov_bootstrap_layout.addWidget(self.var_cov_n_bootstrap)
        self.var_cov_bootstrap_layout.addStretch()

        self.wrapper_layout.addLayout(self.var_cov_bootstrap_layout)

        # Dynamically Build the UI
        self.build_dynamic_ui()

        container_layout.addWidget(
            content_wrapper, alignment=Qt.AlignmentFlag.AlignHCenter
        )

        # --- Moving Average Charts ---
        self.ma_control_layout = QHBoxLayout()
        self.ma_control_layout.addWidget(QLabel("Moving Average Window [s]:"))
        self.ma_window_spinbox = QDoubleSpinBox()
        self.ma_window_spinbox.setRange(0.01, 10000)
        self.ma_window_spinbox.setValue(1.0)
        self.ma_window_spinbox.setDecimals(2)
        self.ma_control_layout.addWidget(self.ma_window_spinbox)
        self.ma_control_layout.addStretch()
        container_layout.addLayout(self.ma_control_layout)

        self.ma_container = QWidget()
        self.ma_layout = QHBoxLayout(self.ma_container)

        self.ma_chart_ch1 = QChart()
        self.ma_chart_ch1.setTitle("Moving Average & Variance Channel 1")
        self.ma_chart_ch1.legend().setVisible(True)
        self.ma_view_ch1 = InteractiveChartView(self.ma_chart_ch1)
        self.ma_view_ch1.setMinimumHeight(650)

        self.ma_chart_ch2 = QChart()
        self.ma_chart_ch2.setTitle("Moving Average & Variance Channel 2")
        self.ma_chart_ch2.legend().setVisible(True)
        self.ma_view_ch2 = InteractiveChartView(self.ma_chart_ch2)
        self.ma_view_ch2.setMinimumHeight(650)

        self.ma_layout.addWidget(self.ma_view_ch1)
        self.ma_layout.addWidget(self.ma_view_ch2)

        container_layout.addWidget(self.ma_container)

        # --- FFT Charts ---
        self.fft_control_layout = QHBoxLayout()

        self.fft_control_layout.addWidget(QLabel("FFT Mode:"))
        self.fft_mode_box = QComboBox()
        self.fft_mode_box.addItems(["PSD (dB)", "Magnitude (dB)"])
        self.fft_control_layout.addWidget(self.fft_mode_box)

        self.fft_control_layout.addWidget(QLabel("Max Points:"))
        self.fft_max_points_spinbox = QSpinBox()
        self.fft_max_points_spinbox.setRange(10, 1000000)
        self.fft_max_points_spinbox.setValue(4000)
        self.fft_max_points_spinbox.setSingleStep(100)
        self.fft_control_layout.addWidget(self.fft_max_points_spinbox)

        self.fft_control_layout.addWidget(QLabel("Downsample:"))
        self.fft_downsample_box = QComboBox()
        self.fft_downsample_box.addItems(["Mean Pooling", "Max Pooling"])
        self.fft_control_layout.addWidget(self.fft_downsample_box)

        self.fft_control_layout.addWidget(QLabel("Reference Function:"))
        self.fft_ref_func_box = QComboBox()
        self.fft_ref_func_box.addItems(["sinc²", "Weighting Function"])
        self.fft_control_layout.addWidget(self.fft_ref_func_box)

        # Connect to auto-update
        self.fft_mode_box.currentIndexChanged.connect(
            lambda _: self.update_results(plot_only=True)
        )
        self.fft_max_points_spinbox.valueChanged.connect(
            lambda _: self.update_results(plot_only=True)
        )
        self.fft_downsample_box.currentIndexChanged.connect(
            lambda _: self.update_results(plot_only=True)
        )
        self.fft_ref_func_box.currentIndexChanged.connect(
            lambda _: self.update_results(plot_only=True)
        )

        self.fft_control_layout.addStretch()
        container_layout.addLayout(self.fft_control_layout)

        self.fft_container = QWidget()
        self.fft_layout = QHBoxLayout(self.fft_container)

        self.fft_chart_ch1 = QChart()
        self.fft_chart_ch1.setTitle("FFT Channel 1")
        self.fft_chart_ch1.legend().setVisible(False)
        self.fft_view_ch1 = InteractiveChartView(self.fft_chart_ch1)
        self.fft_view_ch1.setMinimumHeight(650)

        self.fft_chart_ch2 = QChart()
        self.fft_chart_ch2.setTitle("FFT Channel 2")
        self.fft_chart_ch2.legend().setVisible(False)
        self.fft_view_ch2 = InteractiveChartView(self.fft_chart_ch2)
        self.fft_view_ch2.setMinimumHeight(650)

        self.fft_layout.addWidget(self.fft_view_ch1)
        self.fft_layout.addWidget(self.fft_view_ch2)

        container_layout.addWidget(self.fft_container)

        scroll_area.setWidget(container)
        main_layout.addWidget(scroll_area)

        self.apply_stylesheet()

        # Run an initial refresh to populate the 0s and a default unit
        self.refresh_result_ui()

    def build_dynamic_ui(self):
        """Loops through self.data_model.result and automatically builds the layout."""

        for macro_name, parameters in self.data_model.result.items():
            # Initialize the dictionary for this macro
            self.ui_labels[macro_name] = {}

            box = QGroupBox()
            layout = QVBoxLayout(box)

            if macro_name == "var":
                display_title = "Variance Method"

            elif macro_name == "var_cov":
                display_title = "Variance Covariance Method"

            elif macro_name == "var_doublecov":
                display_title = "Variance Double-Covariance Method"

            elif macro_name == "var_autocov":
                display_title = "Variance AutoCorrelation Method"

            box.setTitle(f"{display_title}")

            grid = QGridLayout()
            grid.setVerticalSpacing(12)

            # Headers
            ch1_header = QLabel("<b>Channel 1</b>")
            ch1_header.setMinimumHeight(25)
            ch2_header = QLabel("<b>Channel 2</b>")
            ch2_header.setMinimumHeight(25)
            ch1_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ch2_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(ch1_header, 0, 2)
            grid.addWidget(ch2_header, 0, 4)

            # Dividers
            total_rows = len(parameters) + 1
            div1 = QFrame()
            div1.setFrameShape(QFrame.Shape.VLine)
            grid.addWidget(div1, 0, 1, total_rows, 1)

            div2 = QFrame()
            div2.setFrameShape(QFrame.Shape.VLine)
            grid.addWidget(div2, 0, 3, total_rows, 1)

            # --- Dynamically create Rows ---
            for index, param_name in enumerate(parameters.keys()):
                row = index + 1

                # Create the labels
                name_lbl = QLabel(param_name)
                name_lbl.setMinimumHeight(20)
                ch1_lbl = QLabel("-")
                ch1_lbl.setMinimumHeight(20)
                ch2_lbl = QLabel("-")
                ch2_lbl.setMinimumHeight(20)

                ch1_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                ch2_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

                # Add to layout
                grid.addWidget(name_lbl, row, 0)
                grid.addWidget(ch1_lbl, row, 2)
                grid.addWidget(ch2_lbl, row, 4)

                # Store references to these specific labels in our dictionary
                self.ui_labels[macro_name][param_name] = {
                    "name": name_lbl,
                    "ch1": ch1_lbl,
                    "ch2": ch2_lbl,
                }

            # Formatting
            grid.setColumnStretch(0, 2)
            grid.setColumnStretch(2, 3)
            grid.setColumnStretch(4, 3)

            layout.addLayout(grid)
            self.wrapper_layout.addWidget(box)

    def refresh_result_ui(self):
        """
        Reads self.data_model.result and updates all labels on the screen.
        Adds the uncertainty with a ± symbol and updates the parameter unit.
        """
        import math

        try:
            n_val_ch1 = self.data_model.result["var_autocov"]["N"][0]
            n_val_ch2 = self.data_model.result["var_autocov"]["N"][2]
            n_max = max(n_val_ch1, n_val_ch2)
            n_boots = max(1, math.ceil(n_max / 10.0))

            n_boots = 10000
            self.var_cov_n_bootstrap.setText(str(n_boots))
        except:
            self.var_cov_n_bootstrap.setText("-")
        if self.setting_tab.show_type == self.setting_tab.CHARGE:
            is_charge = True
            unit = "pC"
        else:
            unit = "Ch"
            is_charge = False

        for macro_name, parameters in self.data_model.result.items():
            for param_name, values in parameters.items():
                ch1_val, ch1_unc, ch2_val, ch2_unc = values

                labels = self.ui_labels[macro_name][param_name]

                if param_name in ["Variance", "Covariance", "AutoCorrelation"]:
                    labels["name"].setText(f"{param_name} ({unit}²):")

                    if is_charge:
                        factor_ch1 = (
                            self.session.capacitance_pf_ch1
                            * self.setting_tab.adc_range
                            / NUM_BINS
                        ) ** 2
                        factor_ch2 = (
                            self.session.capacitance_pf_ch2
                            * self.setting_tab.adc_range
                            / NUM_BINS
                        ) ** 2

                        ch1_val = ch1_val * factor_ch1
                        ch1_unc = ch1_unc * factor_ch1

                        ch2_val = ch2_val * factor_ch2
                        ch2_unc = ch2_unc * factor_ch2

                elif param_name == "N":
                    labels["name"].setText(f"{param_name}")
                    ch1_unc = None
                    ch2_unc = None

                elif param_name in ["cov_mean_sq", "autocov_mean_sq"]:
                    if param_name == "cov_mean_sq":
                        disp_name = "Cov / Mean²"
                    else:
                        disp_name = "AutoCov / Mean²"
                    labels["name"].setText(f"{disp_name}")

                else:
                    if param_name == "y_bar_d":
                        param_name = '<span style="text-decoration: overline;">y</span><sub>D</sub>'
                    labels["name"].setText(f"{param_name} ({unit}):")
                    if is_charge:
                        factor_ch1 = (
                            self.session.capacitance_pf_ch1
                            * self.setting_tab.adc_range
                            / NUM_BINS
                        )
                        factor_ch2 = (
                            self.session.capacitance_pf_ch2
                            * self.setting_tab.adc_range
                            / NUM_BINS
                        )

                        ch1_val = ch1_val * factor_ch1
                        ch1_unc = ch1_unc * factor_ch1

                        ch2_val = ch2_val * factor_ch2
                        ch2_unc = ch2_unc * factor_ch2

                # Convert uncertainty to string to count leading zeros after decimal

                # CH1
                if ch1_unc is None:
                    labels["ch1"].setText(f"{ch1_val}")

                else:
                    unc_str = f"{ch1_unc:.10f}"
                    point = re.match(r"0\.(0*)", unc_str)
                    if point:
                        leading_zeros = len(point.group(1))
                        if leading_zeros == 10:
                            leading_zeros = 0
                    else:
                        leading_zeros = 0

                    decimal_places = leading_zeros + 2

                    formatted_val = f"{ch1_val:.{decimal_places}f}"
                    formatted_unc = f"{ch1_unc:.{decimal_places}f}"

                    labels["ch1"].setText(f"{formatted_val} ± {formatted_unc}")

                # CH2
                if ch2_unc is None:
                    labels["ch2"].setText(f"{ch2_val}")
                else:
                    unc_str = f"{ch2_unc:.10f}"
                    point = re.match(r"0\.(0*)", unc_str)
                    if point:
                        leading_zeros = len(point.group(1))
                        if leading_zeros == 10:
                            leading_zeros = 0
                    else:
                        leading_zeros = 0

                    decimal_places = leading_zeros + 2

                    formatted_val = f"{ch2_val:.{decimal_places}f}"
                    formatted_unc = f"{ch2_unc:.{decimal_places}f}"

                    labels["ch2"].setText(f"{formatted_val} ± {formatted_unc}")

    def update_results(self, plot_only=False):
        self.calculate_results(plot_only)

        if not plot_only:
            self.refresh_result_ui()

    def apply_stylesheet(self):
        style = """
            QGroupBox {
                font-weight: bold;
                border: 1px solid #ddd;
                border-radius: 6px;
                margin-top: 6px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QGroupBox::indicator {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 3px;
                top: 0px;
                width: 13px;
                height: 13px;
            }
            QDoubleSpinBox {
                padding-left: 0px;
                /* Reserve space on right for buttons so text doesn't overlap them */
                padding-right: 10px; 
            }
            QComboBox {
                padding: 3px 0px 3px 0px;
            }

            QLabel {
                color: #333333;
                font-size: 15px;
            }

            QFrame[frameShape="5"] {
                color: #e4e7ed; 
            }
        """
        self.setStyleSheet(style)

    # =======================================================
    # logic
    # =======================================================

    def on_start_stop_press(self):
        btn_text = self.start_bnt.text()
        if btn_text == "ARM":
            self.start_measurement_requested.emit(True)
        elif btn_text == "START":
            self.start_measurement_requested.emit(False)
        elif btn_text in ("SAVE", "SAVE AS", "UPDATE FILE"):
            self.save_data()
        else:
            self.stop_measurement_requested.emit()

    def on_stop_press(self):
        self.stop_measurement_requested.emit()

    def update_dashboard(self):
        new_data = self.data_model.data
        if new_data is None or len(new_data) == 0:
            return

        self.fill_result_table()

        size = (
            self.data_model.data.shape[0] * 8
            + 2 * self.data_model.start_data.shape[0] * 8
        ) * 2
        self.memory_label.setText(str(int(size / 1e6)))

        if self.chart_type == "mc":
            self.show_mc()
        elif self.chart_type == "time":
            self.show_time()

    # =======================================================
    # plot
    # =======================================================

    def on_time_roi_added(self, roi_widget):
        sf = self.session.sample_freq
        region = pg.LinearRegionItem(
            orientation="vertical",
            values=[roi_widget.start_value * sf, roi_widget.stop_value * sf],
        )
        region.setZValue(10)
        self.time_roi_regions[roi_widget] = region
        self.plot_widget.addItem(region)
        region._user_visible = True
        if self.chart_type != "time":
            region.setVisible(False)

        # Bi-directional update
        def update_widget():
            roi_widget.update_from_region(region)
            self.setting_tab.apply_time_roi_button.setEnabled(True)

        def update_region():
            region.sigRegionChanged.disconnect(update_widget)
            sf = self.session.sample_freq
            region.setRegion([roi_widget.start_value * sf, roi_widget.stop_value * sf])
            region.sigRegionChanged.connect(update_widget)
            region._user_visible = True
            if not region.isVisible() and self.chart_type == "time":
                region.setVisible(True)

        region.sigRegionChanged.connect(update_widget)
        roi_widget.update_signal.connect(update_region)

    def hide_time_roi_regions(self):
        for region in self.time_roi_regions.values():
            region._user_visible = False
            region.setVisible(False)

    def on_time_roi_removed(self, roi_widget):
        if roi_widget in self.time_roi_regions:
            region = self.time_roi_regions.pop(roi_widget)
            self.plot_widget.removeItem(region)

    def _get_channel_roi_factor(self, selected_channel):
        if self.setting_tab.show_type == self.setting_tab.CHANNEL:
            return 1.0
        if selected_channel == 1:
            return (
                self.session.capacitance_pf_ch1 * self.setting_tab.adc_range
            ) / NUM_BINS
        return (self.session.capacitance_pf_ch2 * self.setting_tab.adc_range) / NUM_BINS

    def on_channel_roi_added(self, roi_widget):
        factor = self._get_channel_roi_factor(roi_widget.selected_channel)

        mc_region = pg.LinearRegionItem(
            orientation="vertical",
            values=[roi_widget.start_value * factor, roi_widget.stop_value * factor],
        )
        mc_region.setZValue(10)
        self.plot_widget.addItem(mc_region)
        mc_region._user_visible = True

        time_region = pg.LinearRegionItem(
            orientation="horizontal",
            values=[roi_widget.start_value * factor, roi_widget.stop_value * factor],
        )
        time_region.setZValue(10)
        self.plot_widget.addItem(time_region)
        time_region._user_visible = True

        if self.chart_type != "mc":
            mc_region.setVisible(False)
        if self.chart_type != "time":
            time_region.setVisible(False)

        self.channel_roi_regions[roi_widget] = {"mc": mc_region, "time": time_region}

        def update_from_mc_region():
            update_widget(mc_region)

        def update_from_time_region():
            update_widget(time_region)

        def update_widget(source_region):
            min_val, max_val = source_region.getRegion()
            f = self._get_channel_roi_factor(roi_widget.selected_channel)

            roi_widget.start.blockSignals(True)
            roi_widget.stop.blockSignals(True)
            roi_widget.start.setValue(int(min_val / f))
            roi_widget.stop.setValue(int(max_val / f))
            roi_widget.start_value = int(min_val / f)
            roi_widget.stop_value = int(max_val / f)
            roi_widget.start.blockSignals(False)
            roi_widget.stop.blockSignals(False)

            self.setting_tab.apply_roi_button.setEnabled(True)

            other = time_region if source_region is mc_region else mc_region
            other_func = (
                update_from_time_region
                if source_region is mc_region
                else update_from_mc_region
            )
            other.sigRegionChanged.disconnect(other_func)
            other.setRegion([min_val, max_val])
            other.sigRegionChanged.connect(other_func)

        def update_regions():
            mc_region.sigRegionChanged.disconnect(update_from_mc_region)
            time_region.sigRegionChanged.disconnect(update_from_time_region)

            f = self._get_channel_roi_factor(roi_widget.selected_channel)
            bounds = [roi_widget.start_value * f, roi_widget.stop_value * f]
            mc_region.setRegion(bounds)
            time_region.setRegion(bounds)

            mc_region.sigRegionChanged.connect(update_from_mc_region)
            time_region.sigRegionChanged.connect(update_from_time_region)

            mc_region._user_visible = True
            time_region._user_visible = True

            if not mc_region.isVisible() and self.chart_type == "mc":
                mc_region.setVisible(True)
            if not time_region.isVisible() and self.chart_type == "time":
                time_region.setVisible(True)

        mc_region.sigRegionChanged.connect(update_from_mc_region)
        time_region.sigRegionChanged.connect(update_from_time_region)
        roi_widget.update_signal.connect(update_regions)

    def hide_channel_roi_regions(self):
        for regions in self.channel_roi_regions.values():
            regions["mc"]._user_visible = False
            regions["time"]._user_visible = False
            regions["mc"].setVisible(False)
            regions["time"].setVisible(False)

    def on_channel_roi_removed(self, roi_widget):
        if roi_widget in self.channel_roi_regions:
            regions = self.channel_roi_regions.pop(roi_widget)
            self.plot_widget.removeItem(regions["mc"])
            self.plot_widget.removeItem(regions["time"])

    def init_mc_chart(self, update: bool = False):
        """Initialize/Update the multichannel chart. If update is True,
        the chart is simply updated, therefore hidden lines will remain hidden."""
        if not update:
            self.safeAdd(self.mca_item_ch1)
            self.safeAdd(self.mca_item_ch2)
            self.safeRemove(self.line_item_ch1)
            self.safeRemove(self.line_item_ch2)
            self.safeRemove(self.start_line_item_ch1)
            self.safeRemove(self.stop_line_item_ch1)
            self.safeRemove(self.start_line_item_ch2)
            self.safeRemove(self.stop_line_item_ch2)

        self.chart_time_mode_box.setVisible(False)
        for region in self.time_roi_regions.values():
            region.setVisible(False)
        for regions in self.channel_roi_regions.values():
            regions["time"].setVisible(False)
            if getattr(regions["mc"], "_user_visible", True):
                regions["mc"].setVisible(True)
        self.moving_widget.setVisible(False)
        self.remove_overflow_marker()
        self.plot_widget.setLabel("left", "Counts")
        if self.setting_tab.show_type == self.setting_tab.CHANNEL:
            self.plot_widget.setXRange(0, NUM_BINS + 1)
            self.plot_widget.setLabel("bottom", "Channel")
        elif self.setting_tab.show_type == self.setting_tab.CHARGE:
            max_charge = (
                max(
                    [
                        self.session.capacitance_pf_ch1,
                        self.session.capacitance_pf_ch2,
                    ]
                )
                * self.setting_tab.adc_range
            )
            self.plot_widget.setXRange(0, max_charge)
            self.plot_widget.setLabel("bottom", "Charge [pC]")

        self.plot_widget.getAxis("bottom").setScale(1)
        self.plot_widget.setYRange(0, 10)
        self.plot_widget.enableAutoRange(pg.ViewBox.YAxis)

    def init_time_chart(self, update: bool = False):
        """Initialize/Update the time chart. If update is True, the chart is simply
        updated, therefore hidden lines will remain hidden."""
        is_plot = self.central_widget_type == "plot"
        self.chart_time_mode_box.setVisible(is_plot)
        for region in self.time_roi_regions.values():
            if getattr(region, "_user_visible", True):
                region.setVisible(is_plot)
        for regions in self.channel_roi_regions.values():
            regions["mc"].setVisible(False)
            if getattr(regions["time"], "_user_visible", True):
                regions["time"].setVisible(True)
        if not update:
            self.safeRemove(self.calib_line)
            self.safeRemove(self.calib_scatter)
            self.safeRemove(self.mca_item_ch1)
            self.safeRemove(self.mca_item_ch2)
            self.safeAdd(self.line_item_ch1)
            self.safeAdd(self.line_item_ch2)

            if self.session.debug_mode:
                self.safeAdd(self.start_line_item_ch1)
                self.safeAdd(self.stop_line_item_ch1)
                self.safeAdd(self.start_line_item_ch2)
                self.safeAdd(self.stop_line_item_ch2)

        if self.setting_tab.show_type == self.setting_tab.CHANNEL:
            self.plot_widget.setLabel("left", "Channel")
            self.plot_widget.setXRange(0, 1)
            self.plot_widget.setYRange(0, NUM_BINS)
        elif self.setting_tab.show_type == self.setting_tab.CHARGE:
            self.plot_widget.setLabel("left", "Charge [pC]")
            self.plot_widget.setXRange(0, 1)
            max_charge = max(
                [
                    self.session.capacitance_pf_ch1,
                    self.session.capacitance_pf_ch2,
                ]
            ) * (self.setting_tab.adc_range)
            self.plot_widget.setYRange(0, max_charge)

        self.plot_widget.getAxis("bottom").setScale(
            self.session.int_time + self.session.reset_time
        )
        self.plot_widget.setLabel("bottom", "Time [s]")

        is_moving = self.chart_time_mode_box.currentIndex() == 0
        self.moving_widget.setVisible(
            is_moving and (self.central_widget_type == "plot")
        )

        if not is_moving:
            self.plot_widget.enableAutoRange(pg.ViewBox.XAxis)
        else:
            self.plot_widget.setXRange(0, self.n_point_moving_win)
            self.plot_widget.enableAutoRange(pg.ViewBox.XAxis)

        self.line_item_ch1.setDownsampling(auto=False, method="peak")
        self.line_item_ch2.setDownsampling(auto=False, method="peak")

    def safeAdd(self, item):
        if item not in self.plot_widget.items():
            self.plot_widget.addItem(item)
        item.setVisible(True)
        existing_items = [sample.item for sample, label in self.legend.items]
        if hasattr(item, "name") and item.name():
            if item not in existing_items:
                self.legend.addItem(item, item.name())

    def on_calib_toggled(self, checked):
        if checked:
            self.setting_tab.calib_mode_btn.setText("Disable Calibration Mode")
            self.setting_tab.calib_extract_btn.setEnabled(True)

            if self.calib_line is None:
                self.calib_line = pg.InfiniteLine(
                    angle=0, movable=True, pen=pg.mkPen("b", width=2)
                )
                self.calib_line.setPos(100)  # Default height
                self.calib_line.sigPositionChanged.connect(self.update_calib_peaks)

            if self.calib_scatter is None:
                self.calib_scatter = pg.ScatterPlotItem(
                    size=12, pen=pg.mkPen(None), brush=pg.mkBrush(0, 255, 0, 150)
                )

            if self.chart_type == "mc":
                self.safeAdd(self.calib_line)
                self.safeAdd(self.calib_scatter)
                self.update_calib_peaks()
        else:
            self.setting_tab.calib_mode_btn.setText("Enable Calibration Mode")
            self.setting_tab.calib_extract_btn.setEnabled(False)
            self.safeRemove(self.calib_line)
            self.safeRemove(self.calib_scatter)
            self.detected_peaks = []

    def update_calib_peaks(self):
        if not self.setting_tab.calib_mode_btn.isChecked() or self.chart_type != "mc":
            return

        threshold = self.calib_line.value()

        try:
            import numpy as np
            from scipy.signal import find_peaks

            y_data = self.data_model.bar_data_ch1  # CH1
            peaks, _ = find_peaks(y_data, height=threshold, distance=20)

            self.detected_peaks = []
            scatter_points = []
            for p in peaks:
                start = max(0, p - 5)
                end = min(len(y_data), p + 6)

                weights = y_data[start:end]
                channels = np.arange(start, end)

                if np.sum(weights) > 0:
                    centroid = np.average(channels, weights=weights)
                    self.detected_peaks.append(centroid)
                    scatter_points.append({"pos": (centroid, y_data[p])})

            if self.calib_scatter:
                self.calib_scatter.setData(scatter_points)

        except ImportError:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self, "Dependency Error", "scipy is required for peak finding."
            )

    def on_calib_extract(self):
        if not self.detected_peaks:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.information(
                self, "No Peaks", "No peaks detected above the trigger line."
            )
            return

        from views.calibration_dialog import CalibrationDialog

        dialog = CalibrationDialog(self.detected_peaks, self)
        dialog.calibration_applied.connect(self.on_calibration_applied)
        dialog.exec()

    def on_calibration_applied(self, slope, intercept):
        self.session.calibration_slope = slope
        self.session.calibration_intercept = intercept
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.information(
            self,
            "Calibration Saved",
            f"Calibration applied successfully!\nSlope: {slope:.4f}\nIntercept: {intercept:.4f}",
        )
        self.setting_tab.calib_mode_btn.setChecked(False)

    def safeRemove(self, item):
        if item is None:
            return
        item.setVisible(False)
        if self.legend is not None:
            self.legend.removeItem(item)

    def show_mc(self):
        if self.show_plt_log_y.isChecked():
            ch1_log = self.data_model.bar_data_ch1.astype(float)
            ch2_log = self.data_model.bar_data_ch2.astype(float)

            # Use manual log10 scaling to avoid pyqtgraph's native LogMode bugs with stepMode
            ch1_log = np.log10(np.where(ch1_log > 0, ch1_log, 1))
            ch2_log = np.log10(np.where(ch2_log > 0, ch2_log, 1))

            self.plot_widget.setLogMode(False, False)
            if self.chart_type == "mc":
                self.plot_widget.setLabel("left", "Log10(Counts)")
            self.mca_item_ch1.setFillLevel(0)
            self.mca_item_ch2.setFillLevel(0)

        else:
            ch1_log = self.data_model.bar_data_ch1
            ch2_log = self.data_model.bar_data_ch2
            self.plot_widget.setLogMode(False, False)
            if self.chart_type == "mc":
                self.plot_widget.setLabel("left", "Counts")
            self.mca_item_ch1.setFillLevel(0)
            self.mca_item_ch2.setFillLevel(0)

        if self.setting_tab.show_type == self.setting_tab.CHANNEL:
            self.mca_item_ch1.setData(self.mca_x_axis, ch1_log)
            self.mca_item_ch2.setData(self.mca_x_axis, ch2_log)
        elif self.setting_tab.show_type == self.setting_tab.CHARGE:
            max_charge_ch1 = (
                self.session.capacitance_pf_ch1 * self.setting_tab.adc_range
            )
            max_charge_ch2 = (
                self.session.capacitance_pf_ch2 * self.setting_tab.adc_range
            )
            self.mca_item_ch1.setData(
                self.mca_x_axis / NUM_BINS * max_charge_ch1, ch1_log
            )
            self.mca_item_ch2.setData(
                self.mca_x_axis / NUM_BINS * max_charge_ch2, ch2_log
            )

    def show_time(self):
        if self.time_chart_type == "all":
            start = 0
        else:
            start = self.data_model.ptr - self.n_point_moving_win
            if start < 0:
                start = 0

        end = self.data_model.ptr

        if self.setting_tab.show_type == self.setting_tab.CHANNEL:
            factor_ch1 = 1
            factor_ch2 = 1
        elif self.setting_tab.show_type == self.setting_tab.CHARGE:
            max_charge_ch1 = (
                self.session.capacitance_pf_ch1 * self.setting_tab.adc_range
            )
            max_charge_ch2 = (
                self.session.capacitance_pf_ch2 * self.setting_tab.adc_range
            )
            factor_ch1 = max_charge_ch1 / NUM_BINS
            factor_ch2 = max_charge_ch2 / NUM_BINS

        x = np.arange(start=start, stop=end)
        self.line_item_ch1.setData(
            x=x, y=self.data_model.filtered_data[start:end, 0] * factor_ch1
        )
        self.line_item_ch2.setData(
            x=x, y=self.data_model.filtered_data[start:end, 1] * factor_ch2
        )

        self.show_overflow_marker(start=start, stop=end)

        if self.session.debug_mode:
            filtered_ch1 = self.data_model.filtered_data[start:end, 0]
            filtered_ch2 = self.data_model.filtered_data[start:end, 1]

            start_y1 = np.where(
                np.isnan(filtered_ch1),
                np.nan,
                self.data_model.start_data[start:end, 0] * factor_ch1,
            )
            stop_y1 = np.where(
                np.isnan(filtered_ch1),
                np.nan,
                self.data_model.stop_data[start:end, 0] * factor_ch1,
            )
            start_y2 = np.where(
                np.isnan(filtered_ch2),
                np.nan,
                self.data_model.start_data[start:end, 1] * factor_ch2,
            )
            stop_y2 = np.where(
                np.isnan(filtered_ch2),
                np.nan,
                self.data_model.stop_data[start:end, 1] * factor_ch2,
            )

            self.start_line_item_ch1.setData(x=x, y=start_y1)
            self.stop_line_item_ch1.setData(x=x, y=stop_y1)
            self.start_line_item_ch2.setData(x=x, y=start_y2)
            self.stop_line_item_ch2.setData(x=x, y=stop_y2)

    def show_overflow_marker(self, y_marker=NUM_BINS, start: int = 0, stop: int = 0):
        if self.setting_tab.show_type == self.setting_tab.CHARGE:
            max_charge = (
                max(
                    [
                        self.session.capacitance_pf_ch1,
                        self.session.capacitance_pf_ch2,
                    ]
                )
                * self.setting_tab.adc_range
            )
            y_marker = y_marker / NUM_BINS * max_charge

        x_nan_ch1 = start + np.where(np.isnan(self.data_model.data[start:stop, 0]))[0]
        y_nan_ch1 = np.full(x_nan_ch1.shape, y_marker, dtype=float)
        x_nan_ch2 = start + np.where(np.isnan(self.data_model.data[start:stop, 1]))[0]
        y_nan_ch2 = np.full(x_nan_ch2.shape, y_marker, dtype=float)

        if x_nan_ch1.size == 0 and self.nan_marker_item_ch1 is not None:
            self.plot_widget.removeItem(self.nan_marker_item_ch1)
            self.nan_marker_item_ch1 = None

        elif x_nan_ch1.size > 0:
            if self.nan_marker_item_ch1 is None:
                self.nan_marker_item_ch1 = self.plot_widget.plot(
                    x_nan_ch1,
                    y_nan_ch1,
                    pen=None,
                    symbol="o",
                    symbolPen="k",
                    symbolBrush="c",
                    symbolSize=10,
                    name="Errors CH 1",
                )
            else:
                self.nan_marker_item_ch1.setData(x_nan_ch1, y_nan_ch1)

        if x_nan_ch2.size == 0 and self.nan_marker_item_ch2 is not None:
            self.plot_widget.removeItem(self.nan_marker_item_ch2)
            self.nan_marker_item_ch2 = None

        elif x_nan_ch2.size > 0:
            if self.nan_marker_item_ch2 is None:
                self.nan_marker_item_ch2 = self.plot_widget.plot(
                    x_nan_ch2,
                    y_nan_ch2,
                    pen=None,
                    symbol="x",
                    symbolPen="k",
                    symbolBrush="r",
                    symbolSize=10,
                    name="Errors CH 2",
                )
            else:
                self.nan_marker_item_ch2.setData(x_nan_ch2, y_nan_ch2)

    def remove_overflow_marker(self):
        if self.nan_marker_item_ch1 is not None:
            self.plot_widget.removeItem(self.nan_marker_item_ch1)
            self.nan_marker_item_ch1 = None
        if self.nan_marker_item_ch2 is not None:
            self.plot_widget.removeItem(self.nan_marker_item_ch2)
            self.nan_marker_item_ch2 = None

    def show_points_time_plt(self):
        if self.view_plt_points.isChecked():
            self.line_item_ch1.setSymbol("o")
            self.line_item_ch2.setSymbol("o")
            self.start_line_item_ch1.setSymbol("o")
            self.start_line_item_ch2.setSymbol("o")
            self.stop_line_item_ch1.setSymbol("o")
            self.stop_line_item_ch2.setSymbol("o")

        else:
            self.line_item_ch1.setSymbol(None)
            self.line_item_ch2.setSymbol(None)
            self.start_line_item_ch1.setSymbol(None)
            self.start_line_item_ch2.setSymbol(None)
            self.stop_line_item_ch1.setSymbol(None)
            self.stop_line_item_ch2.setSymbol(None)

    # =======================================================
    # results
    # =======================================================

    def fill_result_table(self):
        def set_item(row, col, val):
            item = QTableWidgetItem(str(val))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.result_table.setItem(row, col, item)

        # Headers
        # "Integrator",
        # "Mean\n[Channel]",
        # "Mean\n[pC]",
        # "Variance\n[Channel]",
        # "Variance\n[pC]",
        # "Std. Dev.\n[Channel]",
        # "Std. Dev.\n[pC]",
        # "Cov. \n[Channel]"
        # "n. measures",
        # "Errors",

        if self.setting_tab.covariance_type == self.setting_tab.COV_DOUBLE:
            covariance_ch1 = self.data_model.covariance
            covariance_ch2 = self.data_model.covariance
        else:
            covariance_ch1 = self.data_model.cov_succ_ch1
            covariance_ch2 = self.data_model.cov_succ_ch2

        max_charge = self.session.capacitance_pf_ch1 * self.setting_tab.adc_range
        set_item(0, 1, round(self.data_model.mean_value_ch1, 2))
        set_item(0, 2, round(self.data_model.mean_value_ch1 / NUM_BINS * max_charge, 2))
        set_item(0, 3, round(self.data_model.variance_ch1, 4))
        set_item(
            0, 4, round(self.data_model.variance_ch1 * (max_charge / NUM_BINS) ** 2, 4)
        )
        set_item(0, 5, round(np.sqrt(self.data_model.variance_ch1), 4))
        set_item(
            0,
            6,
            round(
                np.sqrt(self.data_model.variance_ch1 * (max_charge / NUM_BINS) ** 2), 4
            ),
        )
        set_item(0, 7, round(covariance_ch1, 4))
        set_item(0, 8, self.data_model.count_ch1)
        set_item(0, 9, self.data_model.n_overflow_ch1)

        max_charge = self.session.capacitance_pf_ch2 * self.setting_tab.adc_range
        set_item(1, 1, round(self.data_model.mean_value_ch2, 2))
        set_item(1, 2, round(self.data_model.mean_value_ch2 / NUM_BINS * max_charge, 2))
        set_item(1, 3, round(self.data_model.variance_ch2, 4))
        set_item(
            1, 4, round(self.data_model.variance_ch2 * (max_charge / NUM_BINS) ** 2, 4)
        )
        set_item(1, 5, round(np.sqrt(self.data_model.variance_ch2), 4))
        set_item(
            1,
            6,
            round(
                np.sqrt(self.data_model.variance_ch2 * (max_charge / NUM_BINS) ** 2), 4
            ),
        )
        set_item(1, 7, round(covariance_ch2, 4))
        set_item(1, 8, self.data_model.count_ch2)
        set_item(1, 9, self.data_model.n_overflow_ch2)

    def calculate_results(self, plot_only=False):
        start = 0
        end = self.data_model.ptr
        cov_both_ch = self.setting_tab.covariance_type == self.setting_tab.COV_DOUBLE
        window_points = max(
            2, int(round(self.ma_window_spinbox.value() * self.session.sample_freq))
        )

        fft_mode = "psd" if self.fft_mode_box.currentIndex() == 0 else "magnitude"
        fft_downsample = (
            "mean" if self.fft_downsample_box.currentIndex() == 0 else "max"
        )
        fft_max_points = self.fft_max_points_spinbox.value()

        self.calculate_results_requested.emit(
            start,
            end,
            cov_both_ch,
            window_points,
            plot_only,
            fft_mode,
            fft_downsample,
            fft_max_points,
        )

    def render_analysis(
        self,
        ma_dict: dict,
        fft_dict: dict,
        ch2_active: bool,
        fft_mode: str = "psd",
    ):
        freq = self.session.sample_freq if self.session.sample_freq > 0 else 1.0
        time_step = 1.0 / freq
        for idx in [0, 1]:
            if not ch2_active and idx == 1:
                self.ma_chart_ch2.removeAllSeries()
                continue
            if not ma_dict[idx]:
                continue

            n = ma_dict[idx]["n"]

            # --- Moving Average ---
            ma = ma_dict[idx]["ma"]
            mvar = ma_dict[idx]["mvar"]

            time_axis_ma = np.arange(n - len(ma), n) * time_step
            chart_ma = self.ma_chart_ch1 if idx == 0 else self.ma_chart_ch2
            chart_ma.removeAllSeries()

            series_ma = QLineSeries()
            series_ma.setName("Mean")

            series_mvar = QLineSeries()
            series_mvar.setName("Variance")
            series_mvar.setPen(QPen(Qt.GlobalColor.red))

            max_points = 2000
            ma_step = max(1, len(ma) // max_points)

            points_ma = []
            points_mvar = []
            for k in range(0, len(ma), ma_step):
                points_ma.append(QPointF(float(time_axis_ma[k]), float(ma[k])))
                points_mvar.append(QPointF(float(time_axis_ma[k]), float(mvar[k])))

            series_ma.replace(points_ma)
            series_mvar.replace(points_mvar)

            chart_ma.addSeries(series_ma)
            chart_ma.addSeries(series_mvar)
            for axis in chart_ma.axes():
                chart_ma.removeAxis(axis)

            axis_x_ma = QValueAxis()
            axis_x_ma.setTitleText("Time [s]")
            if points_ma:
                axis_x_ma.setRange(time_axis_ma[0], time_axis_ma[-1])

            axis_y_ma = QValueAxis()
            axis_y_ma.setTitleText("Mean [cH]")
            if points_ma:
                y_vals_ma = [p.y() for p in points_ma]
                diff_ma = max(y_vals_ma) - min(y_vals_ma)
                diff_ma = diff_ma if diff_ma > 0 else 1.0
                axis_y_ma.setRange(
                    min(y_vals_ma) - diff_ma * 0.1, max(y_vals_ma) + diff_ma * 0.1
                )

            axis_y_mvar = QValueAxis()
            axis_y_mvar.setTitleText("Variance [cH²]")
            if points_mvar:
                axis_y_mvar.setRange(0, max(p.y() for p in points_mvar) * 1.1)

            chart_ma.addAxis(axis_x_ma, Qt.AlignmentFlag.AlignBottom)
            chart_ma.addAxis(axis_y_ma, Qt.AlignmentFlag.AlignLeft)
            chart_ma.addAxis(axis_y_mvar, Qt.AlignmentFlag.AlignRight)

            series_ma.attachAxis(axis_x_ma)
            series_mvar.attachAxis(axis_x_ma)
            series_ma.attachAxis(axis_y_ma)
            series_mvar.attachAxis(axis_y_mvar)

            # --- FFT ---
            if not fft_dict[idx]:
                continue

            freqs = np.array(fft_dict[idx]["freqs"])
            mag_db = fft_dict[idx]["mag_db"]

            ref_idx = self.fft_ref_func_box.currentIndex()
            is_sinc = ref_idx == 0

            if is_sinc:
                # Calculate sinc^2 series
                t_int = self.session.int_time
                ref_vals = np.sinc(freqs * t_int) ** 2
                # Avoid true zero for log axis
                ref_vals = np.clip(ref_vals, 1e-12, None)
            else:
                # Weighting function cos(2*pi*f/fs)
                ref_vals = np.cos(2 * np.pi * freqs / freq)

            chart_fft = self.fft_chart_ch1 if idx == 0 else self.fft_chart_ch2
            chart_fft.removeAllSeries()

            series_fft = QLineSeries()
            series_fft.setName("FFT")
            points_fft = [QPointF(float(f), float(m)) for f, m in zip(freqs, mag_db)]
            series_fft.replace(points_fft)
            chart_fft.addSeries(series_fft)

            series_ref = QLineSeries()
            series_ref.setName("sinc²(f * T_int)" if is_sinc else "2*cos(2π*l/k)")

            pen_ref = QPen(Qt.GlobalColor.red)
            pen_ref.setWidth(2)
            series_ref.setPen(pen_ref)

            points_ref = [QPointF(float(f), float(s)) for f, s in zip(freqs, ref_vals)]
            series_ref.replace(points_ref)
            chart_fft.addSeries(series_ref)

            for axis in chart_fft.axes():
                chart_fft.removeAxis(axis)

            axis_x_fft = QValueAxis()
            axis_x_fft.setTitleText("Frequency [Hz]")

            axis_y_fft = QValueAxis()
            axis_y_fft.setTitleText(
                "PSD [Ch²/Hz (dB)]" if fft_mode == "psd" else "Magnitude [dB]"
            )

            if is_sinc:
                axis_y_ref = QLogValueAxis()
                axis_y_ref.setTitleText("sinc²")
                axis_y_ref.setBase(10.0)
                axis_y_ref.setLabelFormat("%.1e")
                axis_y_ref.setMinorTickCount(9)
            else:
                axis_y_ref = QValueAxis()
                axis_y_ref.setTitleText("Weight")
                axis_y_ref.setLabelFormat("%.2f")

            chart_fft.addAxis(axis_x_fft, Qt.AlignmentFlag.AlignBottom)
            chart_fft.addAxis(axis_y_fft, Qt.AlignmentFlag.AlignLeft)
            chart_fft.addAxis(axis_y_ref, Qt.AlignmentFlag.AlignRight)

            series_fft.attachAxis(axis_x_fft)
            series_fft.attachAxis(axis_y_fft)

            series_ref.attachAxis(axis_x_fft)
            series_ref.attachAxis(axis_y_ref)

            # Set ranges
            if len(freqs) > 0:
                axis_x_fft.setRange(min(freqs), max(freqs))
                axis_y_fft.setRange(min(mag_db), max(mag_db))
                min_ref = float(np.min(ref_vals))
                max_ref = float(np.max(ref_vals))

                if is_sinc:
                    # Force minimum lower bound to at most 1e-1 to guarantee at least two major ticks (0.1 and 1.0)
                    y_min = min(min_ref, 1e-1)
                    axis_y_ref.setRange(max(1e-12, y_min), max(1.0, max_ref))
                else:
                    padding = (max_ref - min_ref) * 0.05 if max_ref > min_ref else 0.1
                    axis_y_ref.setRange(min_ref - padding, max_ref + padding)

        # logger.debug("Analysis rendered for %s: %s", self.session.file_path, self.data_model.result)

    # =======================================================
    # button logic mapping
    # =======================================================

    def on_show_results_press(self):
        self.plot_central_widget.setVisible(False)
        self.result_central_widget.setVisible(True)
        self.central_widget_type = "result"

        self.chart_time_mode_box.setVisible(False)
        for region in self.time_roi_regions.values():
            region.setVisible(False)
        self.moving_widget.setVisible(False)
        self.update_view_buttons()

        self.show_result_checkbox.setChecked(False)

    def on_show_hist(self):
        self.plot_central_widget.setVisible(True)
        self.result_central_widget.setVisible(False)

        self.chart_type = "mc"
        self.central_widget_type = "plot"
        self.update_view_buttons()
        self.init_mc_chart()
        self.show_mc()

    def on_show_time(self):
        self.plot_central_widget.setVisible(True)
        self.result_central_widget.setVisible(False)

        self.chart_type = "time"
        self.central_widget_type = "plot"
        self.update_view_buttons()
        self.init_time_chart()
        self.show_time()

    def on_change_time_chart_type(self):
        idx = self.chart_time_mode_box.currentIndex()
        if idx == 0:
            self.time_chart_type = "moving"
            self.moving_widget.setVisible(True)
        else:
            self.time_chart_type = "all"
            self.moving_widget.setVisible(False)
        self.init_time_chart()
        self.show_time()

    def on_mem_info_press(self):
        text = (
            "This application stores measurements in memory for fast access.\n\n"
            "Buffers automatically expand by doubling their size when full.\n"
            "Over time, this can lead to high memory consumption.\n\n"
            "Memory usage reference:\n"
            "• Debug mode: ~1 GB per 17 million measurements\n"
            "• Operational mode: ~1 GB per 67 million measurements\n\n"
            "Please monitor system memory during long-running sessions."
        )
        QMessageBox.information(self, "Help — Memory", text)

    def on_restart_press(self):
        if not self.is_running:
            if not self.already_saved and self.already_runned:
                confirm = QMessageBox.question(
                    self,
                    "Restart",
                    "Restart without saving?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if confirm == QMessageBox.StandardButton.No:
                    return

            self.init_variables()

            self.change_start_bnt("arm", hide=False, enable=True)
            self.measuring_time_label.setText("0 s")
            self.memory_label.setText(None)
            self.chart_type = "mc"
            self.update_view_buttons()
            self.init_time_chart()
            self.init_mc_chart()
            self.mca_item_ch1.clear()
            self.mca_item_ch2.clear()
            self.line_item_ch1.clear()
            self.line_item_ch2.clear()
            for r in range(2):
                for c in range(1, self.result_table.columnCount()):
                    self.result_table.setItem(r, c, QTableWidgetItem("0"))

            self.setting_tab.clear_filters()
            self.data_model.clear()
            self.session.file_path = None
            self.restart_signal.emit()
        else:
            QMessageBox.warning(
                self, "Warning", "Please stop the measurement before restarting."
            )

    def save_data(self, save_as=False):
        if not self.is_running:
            file_path = ""
            if self.already_saved and not save_as:
                # Use existing path instead of prompting
                file_path = getattr(self.session, "file_path", "")

            if not file_path or save_as:
                default_dir = getattr(self.session, "active_workspace_dir", "")
                file_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save Data to Zip",
                    default_dir,
                    "SYNC Measurement Files (*.zip);;All Files (*)",
                )
                if not file_path:
                    return
                if not file_path.endswith(".zip"):
                    file_path += ".zip"

            self.save_data_requested.emit(file_path)
        else:
            QMessageBox.warning(
                self, "Warning", "Please stop the measurement before saving."
            )

    def on_load_press(self):
        if self.is_running:
            QMessageBox.warning(
                self, "Warning", "Please stop the measurement before loading."
            )
            return

        elif not self.already_saved and self.already_runned:
            confirm = QMessageBox.question(
                self,
                "Load",
                "Load new file without saving current data?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm == QMessageBox.StandardButton.No:
                return

        dialog = QFileDialog(self)
        dialog.setWindowTitle("Load Measurement Data")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilters(
            [
                "SYNC Measurement Files (*.zip)",
                "CSV Files (*.csv)",
                "Data Files (*.dat)",
                "All Files (*)",
            ]
        )
        default_dir = getattr(
            self.session, "active_workspace_dir", None
        ) or os.path.expanduser("~")
        dialog.setDirectory(default_dir)

        if dialog.exec():
            file_path = dialog.selectedFiles()[0]
            if file_path:
                self.load_data_requested.emit(file_path)

    # =======================================================
    # misc
    # =======================================================

    def toggle_ch1_plots(self, state):

        if hasattr(self, "ma_view_ch1"):
            self.ma_view_ch1.setVisible(state)
        if hasattr(self, "fft_view_ch1"):
            self.fft_view_ch1.setVisible(state)

    def toggle_ch2_plots(self, state):

        if hasattr(self, "ma_view_ch2"):
            self.ma_view_ch2.setVisible(state)
        if hasattr(self, "fft_view_ch2"):
            self.fft_view_ch2.setVisible(state)

    def toggle_settings_panel(self, checked):
        self.right_widget.setVisible(checked)

    def change_start_bnt(
        self, name: str | None, hide: bool = False, enable: bool = True
    ):
        if hide:
            self.start_bnt.setVisible(False)
            if name is None:
                self.start_bnt.setText("")
                return
        else:
            self.start_bnt.setVisible(True)

        # Common big style
        base_style = """
            QPushButton {
                color: white; 
                font-weight: bold; 
                padding: 10px 30px; 
                border-radius: 5px; 
                font-size: 14pt;
                min-width: 150px;
            }
        """

        if name == "stop":
            self.start_bnt.setText("STOP")
            self.start_bnt.setIcon(self.icon_stop)
            self.start_bnt.setStyleSheet(
                base_style
                + """QPushButton { background-color: #dc3545; } 
                QPushButton:hover { background-color: #c82333; }"""
            )

        elif name == "arm":
            self.start_bnt.setText("ARM")
            self.start_bnt.setIcon(self.icon_arm)
            self.start_bnt.setStyleSheet(
                base_style
                + """QPushButton { background-color: #fd7e14; }
                QPushButton:hover { background-color: #e26b0c; }"""
            )

        elif name == "start":
            self.start_bnt.setText("START")
            self.start_bnt.setIcon(self.icon_start)
            self.start_bnt.setStyleSheet(
                base_style
                + """QPushButton { background-color: #28a745; }
                QPushButton:hover { background-color: #218838; }"""
            )

        elif name == "save":
            self.start_bnt.setText(
                "SAVE AS"
                if not getattr(self, "already_saved", False)
                else "UPDATE FILE"
            )
            self.start_bnt.setIcon(self.icon_save)
            self.start_bnt.setStyleSheet(
                base_style
                + """QPushButton { background-color: #0078d7; }
                QPushButton:hover { background-color: #0063b1; }"""
            )

        elif name == "update":
            self.start_bnt.setText("UPDATE FILE")
            self.start_bnt.setIcon(self.icon_save)
            self.start_bnt.setStyleSheet(
                base_style
                + """QPushButton { background-color: #17a2b8; }
                QPushButton:hover { background-color: #138496; }"""
            )

        self.start_bnt.setEnabled(enable)

    def on_edit_info(self):
        dialog = InfoDialog(self.session, self)
        dialog.exec()

    def update_after_load(self, elapsed_time: float):
        self.fill_result_table()
        self.on_frequency_change()
        self.already_saved = True
        self.change_start_bnt("update", hide=False, enable=True)
        self.restart_bnt.setEnabled(True)
        self.measuring_time_label.setText(f"{round(elapsed_time, 2)} s")

        self.already_saved = True

        # Update only the MA plot, keeping the imported JSON result table
        self.update_results(plot_only=True)

        self.plot_central_widget.setVisible(True)
        self.result_central_widget.setVisible(False)
        self.central_widget_type = "plot"
        self.chart_type = "mc"
        self.update_view_buttons()

        if self.chart_type == "mc":
            self.init_mc_chart()
            self.show_mc()
        elif self.chart_type == "time":
            self.init_time_chart()
            self.show_time()

        # clear_filters resets self.session.rois to None, so we must back it up
        saved_rois = getattr(self.session, "rois", None)
        saved_time_rois = getattr(self.session, "time_rois", None)
        self.setting_tab.clear_filters()
        self.session.rois = saved_rois
        self.session.time_rois = saved_time_rois

        # Safely attempt to restore the analysis UI
        try:
            if saved_rois:
                for roi_range in saved_rois:
                    if len(roi_range) == 2:
                        start, stop = roi_range
                        ch = 1
                    else:
                        ch, start, stop = roi_range

                    roi_widget = ROIWidget(self.session)
                    roi_widget.delete_signal.connect(
                        lambda r=roi_widget: self.setting_tab.remove_roi(r)
                    )
                    roi_widget.update_signal.connect(
                        lambda: self.setting_tab.apply_roi_button.setEnabled(True)
                    )

                    if ch == 2:
                        roi_widget.ch_select.setCurrentText("CH 2")
                    else:
                        roi_widget.ch_select.setCurrentText("CH 1")

                    roi_widget.start.setValue(int(start))
                    roi_widget.stop.setValue(int(stop))

                    self.setting_tab.roi_layout.addWidget(roi_widget)
                    self.setting_tab.roi_list.append(roi_widget)
                    self.setting_tab.roi_ranges.append((ch, start, stop))

                self.setting_tab.apply_roi_button.setEnabled(False)

            if saved_time_rois:
                for t_roi_range in saved_time_rois:
                    t_start, t_stop = t_roi_range
                    roi_widget = TimeROIWidget(self.session)
                    roi_widget.delete_signal.connect(
                        lambda r=roi_widget: self.setting_tab.remove_time_roi(r)
                    )
                    roi_widget.update_signal.connect(
                        lambda: self.setting_tab.apply_time_roi_button.setEnabled(True)
                    )

                    roi_widget.start.setValue(float(t_start))
                    roi_widget.stop.setValue(float(t_stop))

                    self.setting_tab.time_roi_layout.addWidget(roi_widget)
                    self.setting_tab.time_roi_list.append(roi_widget)
                    self.setting_tab.time_roi_ranges.append((t_start, t_stop))

                self.setting_tab.apply_time_roi_button.setEnabled(False)

        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Error restoring UI ROIs: {e}")
            self.session.rois = None
            self.session.time_rois = None
            self.setting_tab.clear_filters()

        try:
            self.refresh_result_ui()
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Error refreshing Result UI: {e}")

    def on_frequency_change(self):
        """Called when the time window changes the sample frequency. Called each time
        this window is shown"""
        freq = self.session.sample_freq if self.session.sample_freq > 0 else 1.0
        self.n_point_moving_line.setValue(self.n_point_moving_win / freq)

        def fmt_time(t):
            if t > 1:
                return f"{round(t, 3)} s"
            elif t > 1e-3:
                return f"{round(t * 1e3, 3)} ms"
            else:
                return f"{round(t * 1e6, 3)} μs"

        def fmt_hz(h):
            if h > 1e3:
                return f"{round(h / 1e3, 2)} kHz"
            return f"{round(h, 6)} Hz"

        self.int_time_label.setText(fmt_time(self.session.int_time))
        self.rst_time_label.setText(fmt_time(self.session.reset_time))
        self.sample_freq_label.setText(fmt_hz(self.session.sample_freq))

    def on_change_n_point_moving(self):
        """Called when changing the number of points in the moving window"""

        # The inserted value is in second

        n_point_s = self.n_point_moving_line.value()
        n_point = round(n_point_s * self.session.sample_freq)
        if n_point > 0:
            self.n_point_moving_win = n_point
            if self.chart_type == "time":
                self.init_time_chart()
                self.show_time()

    def on_update_setting(self):
        for roi_widget, regions in self.channel_roi_regions.items():
            f = self._get_channel_roi_factor(roi_widget.selected_channel)
            bounds = [roi_widget.start_value * f, roi_widget.stop_value * f]
            regions["mc"].blockSignals(True)
            regions["time"].blockSignals(True)
            regions["mc"].setRegion(bounds)
            regions["time"].setRegion(bounds)
            regions["mc"].blockSignals(False)
            regions["time"].blockSignals(False)

        if self.chart_type == "time":
            self.init_time_chart(update=True)
            self.show_time()
        elif self.chart_type == "mc":
            self.init_mc_chart(update=True)
            self.show_mc()

        self.recalculate_state_requested.emit()

    def close_event(self) -> bool:
        # closeEvent is called only on the main window, not the child widget

        # used when closing the app when the receiving is still working.
        if self.is_running and self.already_runned:
            confirm = QMessageBox.question(
                self,
                "Close",
                "Stop receiving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm == QMessageBox.StandardButton.No:
                return False
            self.on_stop_press()

        if not self.already_saved and self.already_runned:
            confirm = QMessageBox.question(
                self,
                "Close",
                "Close without saving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if confirm == QMessageBox.StandardButton.No:
                return False
        return True


# =====================================================================================
# The class below can be used fot the moving window in case a memeory safe application
# is required.
# =====================================================================================


# class RingBuffer:
#     def __init__(self, size=500_000):
#         """A circular buffer with fixed memory usage.
#         size: Max number of rows (e.g., 500k points).
#         """
#         self.capacity = size
#         self.buffer = np.zeros((size, 2), dtype=float)
#         self.data_model.ptr = 0  # Points to the next write position
#         self.is_full = False
#
#     def clear(self):
#         self.data_model.ptr = 0
#         self.is_full = False
#         self.buffer.fill(0)
#
#     def add(self, new_data):
#         """new_data: np.array of shape (k, 2)"""
#         n = new_data.shape[0]
#         if n == 0:
#             return
#
#         # If new data is larger than entire buffer, just take the last chunk
#         # This should never happen
#         if n > self.capacity:
#             new_data = new_data[-self.capacity :]  # take last rows
#             n = self.capacity
#
#         # Check if we need to wrap around
#         if self.data_model.ptr + n <= self.capacity:
#             # Simple case: fits at the end
#             self.buffer[self.data_model.ptr : self.data_model.ptr + n] = new_data
#             self.data_model.ptr += n
#         else:
#             # Wrapping case
#             # Fill until the end of the array
#             space_at_end = self.capacity - self.data_model.ptr
#             self.buffer[self.data_model.ptr :] = new_data[:space_at_end]
#
#             # Wrap around to the beginning
#             remainder = n - space_at_end
#             self.buffer[:remainder] = new_data[space_at_end:]
#
#             self.data_model.ptr = remainder
#             self.is_full = True
#
#         if self.data_model.ptr == self.capacity:
#             self.data_model.ptr = 0
#             self.is_full = True
#
#     def get_recent(self, n_points):
#         """Returns the last 'n_points' in chronological order (oldest -> newest).
#         This involves a copy, but it's fast for plotting windows (e.g. 10k points).
#         """
#         if n_points <= 0:
#             return np.empty((0, self.buffer.shape[1]))
#
#         # We can't return more than we have
#         available = self.capacity if self.is_full else self.data_model.ptr
#         n_points = min(n_points, available)
#
#         # Calculate indices
#         # The 'end' is usually self.data_model.ptr
#         # The 'start' is self.data_model.ptr - n_points (handling wrap)
#
#         idx_end = self.data_model.ptr
#         idx_start = self.data_model.ptr - n_points
#
#         if idx_start >= 0:
#             # No wrapping needed for retrieval
#             return self.buffer[idx_start:idx_end]
class TimeROIWidget(QGroupBox):
    delete_signal = pyqtSignal()
    update_signal = pyqtSignal()
    start_value: float = 0.0
    stop_value: float = 60.0

    def __init__(self, session_model) -> None:
        super().__init__()
        self.session = session_model
        self.init_ui()

    def init_ui(self):
        trash_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)

        master = QVBoxLayout()
        self.setLayout(master)

        row1 = QHBoxLayout()

        left_container = QWidget()
        left_layout = QHBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.min_btn = QPushButton("|<")
        self.min_btn.setToolTip("Set to 0")
        self.min_btn.setFixedWidth(30)
        self.min_btn.clicked.connect(self.set_to_min)

        self.max_btn = QPushButton("∞")
        self.max_btn.setToolTip("Track maximum time")
        self.max_btn.setFixedWidth(30)
        self.max_btn.setCheckable(True)
        self.max_btn.toggled.connect(self.set_to_max)

        left_layout.addWidget(self.min_btn)
        left_layout.addWidget(self.max_btn)
        left_layout.addStretch(1)

        # Match the width of QLabel("Channel:") + QComboBox(70) in ROIWidget
        left_container.setFixedWidth(135)

        row1.addWidget(left_container)

        delete_button = QPushButton()
        delete_button.setIcon(trash_icon)
        delete_button.setToolTip("Remove Time ROI")
        delete_button.clicked.connect(self.delete_signal.emit)

        row1.addStretch(1)
        row1.addWidget(delete_button)
        row1.addStretch(1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("From"))
        self.start = QDoubleSpinBox()
        self.start.setFixedWidth(85)
        self.start.setDecimals(3)
        self.start.setSingleStep(0.001)
        self.start.setRange(0, 1000000)
        self.start.setValue(self.start_value)
        self.start.valueChanged.connect(self.roi_changed)
        row2.addWidget(self.start)

        row2.addWidget(QLabel("To"))
        self.stop = QDoubleSpinBox()
        self.stop.setFixedWidth(85)
        self.stop.setDecimals(3)
        self.stop.setSingleStep(0.001)
        self.stop.setRange(0, 1000000)
        self.stop.setValue(self.stop_value)
        self.stop.valueChanged.connect(self.roi_changed)
        row2.addWidget(self.stop)

        master.addLayout(row1)
        master.addLayout(row2)

    def set_to_min(self):
        self.start.setValue(0)

    def set_to_max(self, checked):
        if checked:
            self.stop.setDisabled(True)
            self.stop.setValue(1000000)
        else:
            self.stop.setDisabled(False)

    def roi_changed(self):
        if self.start.value() >= self.stop.value() and not self.max_btn.isChecked():
            self.stop.setValue(self.start.value() + 0.001)

        if self.stop.value() <= self.start.value():
            self.start.setValue(self.stop.value() - 0.001)

        self.start_value = float(self.start.value())
        self.stop_value = float(self.stop.value())
        self.update_signal.emit()

    def get_range(self) -> tuple[float, float]:
        return (self.start_value, self.stop_value)

    def update_from_region(self, region):
        self.start.blockSignals(True)
        self.stop.blockSignals(True)

        min_x, max_x = region.getRegion()

        sf = self.session.sample_freq
        min_sec = min_x / sf
        max_sec = max_x / sf

        # If max is being dragged manually, uncheck max tracking
        if self.max_btn.isChecked() and max_sec < 999999:
            self.max_btn.blockSignals(True)
            self.max_btn.setChecked(False)
            self.stop.setDisabled(False)
            self.max_btn.blockSignals(False)

        self.start.setValue(min_sec)
        self.stop.setValue(max_sec)
        self.start_value = float(min_sec)
        self.stop_value = float(max_sec)

        self.start.blockSignals(False)
        self.stop.blockSignals(False)
        self.update_signal.emit()


class ROIWidget(QGroupBox):
    delete_signal = pyqtSignal()
    update_signal = pyqtSignal()
    selected_channel: int = 1
    start_value: int = int(NUM_BINS * 0.4)
    stop_value: int = int(NUM_BINS * 0.6)

    def __init__(self, session_model) -> None:
        super().__init__()
        self.session = session_model
        self.init_ui()

    def init_ui(self):
        trash_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)

        master = QVBoxLayout()
        self.setLayout(master)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Channel:"))
        self.ch_select = QComboBox()
        self.ch_select.addItem("CH 1")
        if self.session.multiple_channel:
            self.ch_select.addItem("CH 2")

        self.ch_select.currentIndexChanged.connect(self.on_ch_change)
        self.ch_select.setFixedWidth(70)
        row1.addWidget(self.ch_select)

        delete_button = QPushButton()
        delete_button.setIcon(trash_icon)
        delete_button.setToolTip("Remove ROI")
        delete_button.clicked.connect(self.delete_signal.emit)

        row1.addStretch(1)
        row1.addWidget(delete_button)
        row1.addStretch(1)

        row2 = QHBoxLayout()

        row2.addWidget(QLabel("From"))
        self.start = QSpinBox()
        self.start.setFixedWidth(85)
        self.start.setRange(0, NUM_BINS - 2)
        self.start.setValue(self.start_value)
        self.start.valueChanged.connect(self.roi_changed)
        row2.addWidget(self.start)

        row2.addWidget(QLabel("To"))
        self.stop = QSpinBox()
        self.stop.setFixedWidth(85)
        self.stop.setRange(1, NUM_BINS - 1)
        self.stop.setValue(self.stop_value)
        self.stop.valueChanged.connect(self.roi_changed)
        row2.addWidget(self.stop)

        master.addLayout(row1)
        master.addLayout(row2)

    def on_ch_change(self):
        if self.ch_select.currentText() == "CH 1":
            self.selected_channel = 1
        else:
            self.selected_channel = 2

        self.update_signal.emit()

    def roi_changed(self):
        if self.start.value() >= self.stop.value():
            self.stop.setValue(self.start.value() + 1)

        if self.stop.value() <= self.start.value():
            self.start.setValue(self.stop.value() - 1)

        self.start_value = self.start.value()
        self.stop_value = self.stop.value()
        self.update_signal.emit()

    def get_range(self) -> tuple[int, int, int]:
        """Return a tuple containing the roi information in the following order:

        (selected_channel, start, stop)
        """
        return (self.selected_channel, self.start_value, self.stop_value)


class RunSettingTab(QWidget):
    update_signal = pyqtSignal()
    time_roi_added = pyqtSignal(object)
    time_roi_removed = pyqtSignal(object)
    channel_roi_added = pyqtSignal(object)
    channel_roi_removed = pyqtSignal(object)
    CHARGE = "Charge"
    CHANNEL = "Channel"

    show_type = CHANNEL
    adc_range = 5
    COV_DOUBLE = "Double"
    COV_SUCCESSIVE = "Successive"
    covariance_type = COV_SUCCESSIVE

    def __init__(self, session_model, hardware_manager=None) -> None:
        super().__init__()
        self.session = session_model
        self.hardware_manager = hardware_manager
        self.roi_list: list[ROIWidget] = []
        self.roi_ranges: list[tuple[int, int, int]] = []
        self.time_roi_list: list[TimeROIWidget] = []
        self.time_roi_ranges: list[tuple[float, float]] = []
        self.init_ui()

    def init_ui(self):
        style_sheet = """
            QGroupBox {
                font-weight: bold;
                border: 1px solid #ddd;
                border-radius: 6px;
                margin-top: 6px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
             QGroupBox::indicator {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 3px;
                top: 0px;
                width: 13px;
                height: 13px;
            }
            QDoubleSpinBox {
                padding-left: 0px;
                /* Reserve space on right for buttons so text doesn't overlap them */
                padding-right: 10px; 
            }
            QComboBox {
                padding: 3px 0px 3px 0px;
            }
        """
        self.setStyleSheet(style_sheet)

        main_layout = QVBoxLayout()
        # main_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(main_layout)

        # --- ADC Section ---
        adc_group = QGroupBox("ADC Range [V]")
        adc_layout = QHBoxLayout()

        self.min_adc_line = QDoubleSpinBox()
        self.min_adc_line.setRange(float("-inf"), float("inf"))
        self.min_adc_line.setValue(0)
        self.min_adc_line.valueChanged.connect(self.on_adc_range_change)

        self.max_adc_line = QDoubleSpinBox()
        self.max_adc_line.setRange(float("-inf"), float("inf"))
        self.max_adc_line.setValue(5)
        self.max_adc_line.valueChanged.connect(self.on_adc_range_change)

        adc_layout.addWidget(self.min_adc_line)
        adc_layout.addStretch(1)
        adc_layout.addWidget(QLabel("to"))
        adc_layout.addStretch(1)
        adc_layout.addWidget(self.max_adc_line)
        adc_group.setLayout(adc_layout)

        # --- Calibration Section ---
        calib_group = QGroupBox("Calibration")
        calib_layout = QVBoxLayout()
        self.calib_mode_btn = QPushButton("Enable Calibration Mode")
        self.calib_mode_btn.setCheckable(True)
        self.calib_extract_btn = QPushButton("Extract Peaks")
        self.calib_extract_btn.setEnabled(False)
        calib_layout.addWidget(self.calib_mode_btn)
        calib_layout.addWidget(self.calib_extract_btn)
        calib_group.setLayout(calib_layout)

        main_layout.addWidget(calib_group)

        # --- View Mode ---
        view_group = QGroupBox("Units")
        view_layout = QVBoxLayout()
        self.type_box = QComboBox()
        self.type_box.addItems([self.CHANNEL, self.CHARGE])
        self.type_box.currentIndexChanged.connect(self.on_type_change)
        view_layout.addWidget(self.type_box)
        view_group.setLayout(view_layout)

        # --- Covariance ---

        cov_group = QGroupBox("Covariance")
        cov_layout = QVBoxLayout()
        self.cov_type_box = QComboBox()
        self.cov_type_box.addItems([self.COV_DOUBLE, self.COV_SUCCESSIVE])
        self.cov_type_box.setCurrentIndex(1)
        self.ch_cov_succ = QComboBox()
        self.cov_type_box.currentIndexChanged.connect(self.on_cov_type_change)
        cov_layout.addWidget(self.cov_type_box)
        cov_group.setLayout(cov_layout)

        # --- Time ROI ---
        time_roi_group = QGroupBox("Time ROI")
        self.time_roi_layout = QVBoxLayout()
        time_roi_group.setLayout(self.time_roi_layout)

        time_init_row = QHBoxLayout()
        self.time_roi_layout.addLayout(time_init_row)

        self.add_time_roi_button = QPushButton("Add")
        self.add_time_roi_button.clicked.connect(self.on_add_time_roi_press)
        time_init_row.addWidget(self.add_time_roi_button)

        self.apply_time_roi_button = QPushButton("Apply")
        self.apply_time_roi_button.clicked.connect(self.on_apply_time_roi_press)
        time_init_row.addWidget(self.apply_time_roi_button)
        self.apply_time_roi_button.setDisabled(True)

        # --- ROI ---

        roi_group = QGroupBox("ROI")
        self.roi_layout = QVBoxLayout()
        roi_group.setLayout(self.roi_layout)

        init_row = QHBoxLayout()
        self.roi_layout.addLayout(init_row)

        self.add_roi_button = QPushButton("Add")
        self.add_roi_button.clicked.connect(self.on_add_roi_press)
        init_row.addWidget(self.add_roi_button)

        self.apply_roi_button = QPushButton("Apply")
        self.apply_roi_button.clicked.connect(self.on_apply_roi_press)
        init_row.addWidget(self.apply_roi_button)
        self.apply_roi_button.setDisabled(True)

        # Add all to layout
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Show vertical scrollbar only when the content exceeds height
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 5, 0)

        content_layout.addWidget(adc_group)
        content_layout.addSpacing(20)
        content_layout.addWidget(view_group)
        content_layout.addWidget(cov_group)
        content_layout.addWidget(time_roi_group)
        content_layout.addWidget(roi_group)
        content_layout.addStretch(1)

        scroll_area.setWidget(content_widget)
        main_layout.addWidget(scroll_area)

    def on_type_change(self):
        self.show_type = self.type_box.currentText()
        self.update_signal.emit()

    def on_cov_type_change(self):
        if self.cov_type_box.currentText() == self.COV_DOUBLE:
            self.covariance_type = self.COV_DOUBLE

        else:
            self.covariance_type = self.COV_SUCCESSIVE

        self.update_signal.emit()

    def on_adc_range_change(self):
        mn, mx = self.min_adc_line.value(), self.max_adc_line.value()
        if mn > mx:
            self.min_adc_line.setValue(mx)
        diff = mx - mn
        if diff > 0:
            self.adc_range = diff
            self.update_signal.emit()

    def on_add_roi_press(self):
        roi = ROIWidget(self.session)
        roi.delete_signal.connect(lambda: self.remove_roi(roi))
        roi.update_signal.connect(lambda: self.apply_roi_button.setEnabled(True))
        self.roi_layout.addWidget(roi)

        self.roi_list.append(roi)
        self.apply_roi_button.setEnabled(True)
        self.channel_roi_added.emit(roi)

    def on_apply_roi_press(self):
        # ROIS are applied only when apply is pressed
        self._update_roi_range()
        self.apply_roi_button.setDisabled(True)
        self.update_signal.emit()

    def remove_roi(self, roi: ROIWidget):
        self.channel_roi_removed.emit(roi)
        self.roi_list.remove(roi)
        self.roi_layout.removeWidget(roi)
        roi.setParent(None)
        roi.deleteLater()
        self.apply_roi_button.setEnabled(True)

    def _update_roi_range(self):
        self.roi_ranges = []
        for roi in self.roi_list:
            self.roi_ranges.append(roi.get_range())
        self.session.rois = self.roi_ranges

    def clear_filters(self):
        for roi in list(self.roi_list):
            self.channel_roi_removed.emit(roi)
            self.roi_layout.removeWidget(roi)
            roi.deleteLater()
        self.roi_list = []
        self.roi_ranges = []
        self.session.rois = None
        self.apply_roi_button.setDisabled(True)

        for roi in list(self.time_roi_list):
            self.time_roi_layout.removeWidget(roi)
            self.time_roi_removed.emit(roi)
            roi.deleteLater()
        self.time_roi_list = []
        self.time_roi_ranges = []
        self.session.time_rois = None
        self.apply_time_roi_button.setDisabled(True)

    def get_roi_range(self) -> None | list[tuple[int, int, int]]:
        if len(self.roi_ranges) == 0:
            return None
        return self.roi_ranges

    def on_add_time_roi_press(self):
        roi = TimeROIWidget(self.session)
        roi.delete_signal.connect(lambda: self.remove_time_roi(roi))
        roi.update_signal.connect(lambda: self.apply_time_roi_button.setEnabled(True))
        self.time_roi_layout.addWidget(roi)
        self.time_roi_list.append(roi)
        self.apply_time_roi_button.setEnabled(True)
        self.time_roi_added.emit(roi)

    def on_apply_time_roi_press(self):
        self._update_time_roi_range()
        self.apply_time_roi_button.setDisabled(True)
        self.update_signal.emit()

    def remove_time_roi(self, roi):
        self.time_roi_list.remove(roi)
        self.time_roi_layout.removeWidget(roi)
        roi.setParent(None)
        self.time_roi_removed.emit(roi)
        roi.deleteLater()
        self.apply_time_roi_button.setEnabled(True)

    def _update_time_roi_range(self):
        self.time_roi_ranges = []
        for roi in self.time_roi_list:
            self.time_roi_ranges.append(roi.get_range())
        self.session.time_rois = self.time_roi_ranges
