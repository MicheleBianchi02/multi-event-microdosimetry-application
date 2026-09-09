import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.constants import APP_VERSION
from views.import_data_widget import ImportDataWidget
from views.mode_window import SingleEventWaitWindow
from views.run_config_widget import RunConfigWidget
from views.running_window import RunningWindow
from views.shift_config_dialog import ShiftConfigDialog
from views.start_config_widget import StartConfigWidget
from views.welcome_widget import WelcomeWidget

logger = logging.getLogger(__name__)


class View(QWidget):
    def __init__(self, session_model, data_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.data_model = data_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager

        self.setup_view = SetupWindow(self.session, self.simulate, self.hardware_manager)
        self.running_view = RunningWindow(self.session, self.data_model, self.simulate, self.hardware_manager)

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle(f"ATOMIQA {APP_VERSION}")
        self.resize(1600, 900)

        master = QVBoxLayout()
        self.setLayout(master)

        self.windows_stack = QStackedWidget()
        master.addWidget(self.windows_stack)

        self.setup_view.change_to_running_window.connect(self.change_to_running_window)
        self.setup_view.load_data_requested.connect(self.load_data)

        self.running_view.restart_signal.connect(self.restart_window)

        self.windows_stack.addWidget(self.setup_view)
        self.windows_stack.addWidget(self.running_view)

        self.windows_stack.setCurrentIndex(0)

    def change_to_running_window(self):
        self.windows_stack.setCurrentIndex(1)
        self.running_view.on_frequency_change()

    def refresh_shift_dialog(self):
        """Let the open workspace dialog re-read the hardware link state."""
        self.setup_view.refresh_shift_dialog()

    def restart_window(self):
        # Clear the plot from the previous run
        self.running_view.clear_plot()

        # If we were just doing offline analysis (stack_view index 3 is Import Data)
        if self.setup_view.stack_view.currentIndex() == 3:
            self.windows_stack.setCurrentIndex(0)
            return

        # If we didn't connect hardware, go back to the welcome screen
        if not self.setup_view.is_hardware_connected:
            self.windows_stack.setCurrentIndex(0)
            self.setup_view.restart_from_beginning()
            return

        # On restart for a real measurement, seamlessly slide back to Step 2
        self.windows_stack.setCurrentIndex(0)
        self.setup_view.stack_view.setCurrentIndex(2)

        # Show headers and footers
        self.setup_view.header_frame.show()
        self.setup_view.footer_frame.show()

        # Update buttons
        self.setup_view.back_bnt.setEnabled(True)
        self.setup_view.next_bnt.setText("Start Measurement")
        self.setup_view.next_bnt.show()

        if self.setup_view.run_config.time_window.already_sent:
            self.setup_view.next_bnt.setEnabled(True)
        else:
            self.setup_view.next_bnt.setEnabled(False)

        self.setup_view.run_config.time_window.update_label()
        self.setup_view.run_config.time_window.populate_meta_fields()

    def load_data(self, file_path: str):
        self.running_view.load_data_requested.emit(file_path)

    def closeEvent(self, a0) -> None:
        if self.windows_stack.currentIndex() == 1:  # running_window
            close = self.running_view.close_event()

            if close:
                return super().closeEvent(a0)

            return a0.ignore()

        else:
            return super().closeEvent(a0)


class SetupWindow(QWidget):
    change_to_running_window = pyqtSignal()
    load_data_requested = pyqtSignal(str)
    mode_switched = pyqtSignal()
    # Hardware-config requests from the workspace dialog (handled by the app
    # controller, which owns the hardware manager)
    hardware_import_requested = pyqtSignal()
    hardware_edit_requested = pyqtSignal()
    hardware_new_requested = pyqtSignal()
    hardware_remove_requested = pyqtSignal()

    def __init__(self, session_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager

        # Windows
        self.welcome_widget = WelcomeWidget()
        self.import_data = ImportDataWidget(self.session)
        self.start_config = StartConfigWidget(self.session, self.simulate, self.hardware_manager)
        self.run_config = RunConfigWidget(self.session, self.simulate, self.hardware_manager)
        self.single_event_wait = SingleEventWaitWindow()

        # Connections for Welcome Screen
        self.welcome_widget.start_measurement_clicked.connect(self.go_to_start_config)
        self.welcome_widget.analyze_offline_clicked.connect(self.go_to_import_data)

        # Connections for Single Event Wait
        self.single_event_wait.change_to_multi.connect(self.on_switch_to_multi)

        # File load signals
        self.import_data.load_signal.connect(self.on_file_loaded)

        self.start_config.setting_window.connection_successfull.connect(
            self.connection_successfull
        )
        self.start_config.mode_window.mode_selected.connect(
            self.on_mode_selected
        )
        self.run_config.time_window.sent_successfull.connect(self.sent_successfull)
        self.run_config.time_window.editing_time.connect(self.disable_next)

        self.is_hardware_connected = False

        self.init_ui()

    @property
    def setting_window(self):
        return self.start_config.setting_window

    @property
    def time_window(self):
        return self.run_config.time_window

    @property
    def mode_window(self):
        return self.start_config.mode_window

    def init_ui(self):
        # header section
        self.header_frame = QFrame()
        self.header_frame.setStyleSheet(
            "/* background-color: white */ ; border-bottom: 1px solid #ddd;"
        )
        header_layout = QVBoxLayout(self.header_frame)

        top_header_layout = QHBoxLayout()

        self.header_title = QLabel("Step 1: Operation Mode")
        self.header_title.setStyleSheet(
            "font-size: 22pt; font-weight: bold; color: palette(text); border: none;"
        )

        self.workspace_btn = QPushButton("⚙️ Workspace Settings")
        self.workspace_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.workspace_btn.setStyleSheet("""
            QPushButton { 
                background-color: #f8f9fa; border: 1px solid #ccc; 
                border-radius: 4px; padding: 5px 15px; font-weight: bold; color: #333;
            }
            QPushButton:hover { background-color: #e2e6ea; }
        """)
        self.workspace_btn.clicked.connect(self.open_workspace_settings)

        top_header_layout.addWidget(self.header_title)
        top_header_layout.addStretch()
        top_header_layout.addWidget(self.workspace_btn)

        self.header_subtitle = QLabel("Choose the operational mode.")
        self.header_subtitle.setStyleSheet("font-size: 15pt; color: #666; border: none")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 4)
        self.progress_bar.setValue(1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: none; background: #eee; border-radius: 3px; }
            QProgressBar::chunk { background: #0078d7; border-radius: 3px; }
        """)

        header_layout.addLayout(top_header_layout)
        header_layout.addWidget(self.header_subtitle)
        header_layout.addSpacing(10)
        header_layout.addWidget(self.progress_bar)
        header_layout.setContentsMargins(30, 20, 30, 20)

        self.stack_view = DynamicStackedWidget()

        self.stack_view.addWidget(self.welcome_widget)
        self.stack_view.addWidget(self.start_config)
        self.stack_view.addWidget(self.run_config)
        self.stack_view.addWidget(self.import_data)
        self.stack_view.addWidget(self.single_event_wait)

        # footer section
        self.footer_frame = QFrame()
        self.footer_frame.setStyleSheet(
            "/* background-color: #f5f5f5 */; border-top: 1px solid #ddd;"
        )
        footer_layout = QHBoxLayout(self.footer_frame)
        footer_layout.setContentsMargins(30, 15, 30, 15)

        self.back_bnt = QPushButton("Back")
        self.back_bnt.setEnabled(False)
        self.back_bnt.setStyleSheet("""
            QPushButton { 
                font-size: 14pt;
                font-weight: bold; 
                padding: 8px 30px; 
                color: palette(text);
                background-color: rgba(127, 127, 127, 0.3); 
                border: none; 
                border-radius: 4px; 
            }
            QPushButton:hover { background: #e0e0e0; color: #000; }
            QPushButton:disabled { 
                background-color: rgba(127, 127, 127, 0.3); 
                color: rgba(127, 127, 127, 0.8);
            }
        """)
        self.back_bnt.clicked.connect(self.on_back_press)

        self.next_bnt = QPushButton("Next Step")
        self.next_bnt.setEnabled(False)
        self.next_bnt.setStyleSheet("""
            QPushButton { 
                font-size: 14pt; 
                font-weight: bold; 
                padding: 8px 30px; 
                background-color: #0078d7; 
                color: white; 
                border-radius: 4px; 
                border: none;
            }
            QPushButton:hover { background-color: #0063b1; }
            QPushButton:disabled { 
                background-color: rgba(127, 127, 127, 0.3); 
                color: rgba(127, 127, 127, 0.8);
            }
        """)
        self.next_bnt.clicked.connect(self.on_next_press)

        self.workspace_status_lbl = QLabel("Workspace: None")
        self.workspace_status_lbl.setStyleSheet(
            "color: #666; font-style: italic; font-size: 11pt;"
        )
        self.session.workspace_changed.connect(
            lambda w: self.workspace_status_lbl.setText(
                f"Workspace: {w}" if w else "Workspace: None"
            )
        )

        footer_layout.addWidget(self.back_bnt)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.workspace_status_lbl)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.next_bnt)

        # main layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        layout.addWidget(self.header_frame)
        self.header_frame.hide()  # Hidden on Welcome screen
        self.footer_frame.hide()  # Hidden on Welcome screen

        # Wrapper for content to give it margin
        content_wrapper = QWidget()
        content_layout = QVBoxLayout(content_wrapper)
        content_layout.setContentsMargins(30, 20, 30, 20)

        center_layout = QHBoxLayout()
        center_layout.addStretch(1)

        # self.stack_view.setMaximumWidth(700)
        center_layout.addWidget(self.stack_view)

        center_layout.addStretch(1)

        content_layout.addLayout(center_layout)

        layout.addWidget(content_wrapper, stretch=1)

        layout.addWidget(self.footer_frame)

        self.stack_view.currentChanged.connect(self.updateGeometry)
        self.stack_view.currentChanged.connect(self.adjustSize)

    def open_workspace_settings(self):
        dialog = ShiftConfigDialog(self.session, self.hardware_manager, self)
        widget = dialog.widget
        widget.hardware_import_requested.connect(self.hardware_import_requested.emit)
        widget.hardware_edit_requested.connect(self.hardware_edit_requested.emit)
        widget.hardware_new_requested.connect(self.hardware_new_requested.emit)
        widget.hardware_remove_requested.connect(self.hardware_remove_requested.emit)
        self._shift_dialog = dialog
        dialog.exec()
        self._shift_dialog = None

    def refresh_shift_dialog(self):
        """Let the open workspace dialog re-read the hardware link state."""
        dialog = getattr(self, "_shift_dialog", None)
        if dialog is not None:
            dialog.widget.refresh_hardware_status()

    def go_to_start_config(self):
        self.update_header(
            1, "Step 1: Hardware Setup", "Choose mode and connect to device."
        )
        self.stack_view.setCurrentIndex(1)
        self.header_frame.show()
        self.footer_frame.show()
        self.back_bnt.setEnabled(True)
        self.next_bnt.setText("Next Step")
        self.check_start_config_ready()

    def go_to_import_data(self):
        self.stack_view.setCurrentIndex(3)
        self.header_frame.hide()
        self.footer_frame.show()
        self.back_bnt.setEnabled(True)
        self.next_bnt.hide()

    def on_file_loaded(self, path):
        self.load_data_requested.emit(path)
        self.change_to_running_window.emit()

    def on_switch_to_multi(self):
        self.start_config.mode_window.select_mode(False)
        self.update_header(
            2,
            "Step 2: Measurement Setup",
            "Configure time, facility, and metadata.",
        )
        self.stack_view.setCurrentIndex(2)
        self.back_bnt.setEnabled(True)
        self.next_bnt.setText("Start Measurement")
        self.next_bnt.show()

        if self.run_config.time_window.already_sent:
            self.next_bnt.setEnabled(True)
        else:
            self.next_bnt.setEnabled(False)

        self.run_config.time_window.update_label()
        self.mode_switched.emit()

    def on_next_press(self):
        index = self.stack_view.currentIndex()

        if index == 1:  # Start Config -> Run Config OR SingleEventWait
            if self.session.single_event:
                self.update_header(
                    2,
                    "Step 2: Single Event Mode",
                    "Device is actively waiting for events.",
                )
                self.stack_view.setCurrentIndex(4)
                self.back_bnt.setEnabled(True)
                self.next_bnt.hide()
                self.mode_switched.emit()
            else:
                self.update_header(
                    2,
                    "Step 2: Measurement Setup",
                    "Configure time, facility, and metadata.",
                )
                self.stack_view.setCurrentIndex(2)
                self.back_bnt.setEnabled(True)
                self.next_bnt.setText("Start Measurement")
                self.next_bnt.show()

                if self.run_config.time_window.already_sent:
                    self.next_bnt.setEnabled(True)
                else:
                    self.next_bnt.setEnabled(False)

                self.run_config.time_window.update_label()
                self.mode_switched.emit()

        elif index == 2:  # Run Config -> Run
            self.run_config.save_configuration()
            self.change_to_running_window.emit()

    def on_back_press(self):
        index = self.stack_view.currentIndex()

        if index == 1:  # Start Config -> Welcome Screen
            self.stack_view.setCurrentIndex(0)
            self.header_frame.hide()
            self.footer_frame.hide()
            self.session.single_event = None

        elif index == 2:  # Run Config -> Start Config
            self.update_header(
                1, "Step 1: Hardware Setup", "Choose mode and connect to device."
            )
            self.next_bnt.setText("Next Step")
            self.next_bnt.setEnabled(True)
            self.stack_view.setCurrentIndex(1)
            self.check_start_config_ready()

        elif index == 3:  # Import Data -> Welcome Screen
            self.stack_view.setCurrentIndex(0)
            self.header_frame.hide()
            self.footer_frame.hide()
            self.next_bnt.show()

        elif index == 4:  # Single Event Wait -> Start Config
            self.update_header(
                1, "Step 1: Hardware Setup", "Choose mode and connect to device."
            )
            self.next_bnt.setText("Next Step")
            self.next_bnt.setEnabled(True)
            self.next_bnt.show()
            self.stack_view.setCurrentIndex(1)
            self.check_start_config_ready()

    def update_header(self, step_num, title, subtitle):
        self.header_title.setText(title)
        self.header_subtitle.setText(subtitle)
        self.progress_bar.setValue(step_num)

    def connection_successfull(self):
        self.is_hardware_connected = True
        self.check_start_config_ready()

    def on_mode_selected(self, is_single_event: bool):
        """Mode chosen on Step 1: stop the user here if no measurement setup
        (workspace with a linked, loaded hardware config) is available."""
        if not self.simulate and not (
            self.hardware_manager and self.hardware_manager.is_loaded
        ):
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "No Measurement Setup",
                "No hardware configuration is loaded.\n\n"
                "Create a measurement setup first: open ⚙️ Workspace Settings, "
                "create a workspace and import or assemble the hardware "
                "configuration of your setup.\n\n"
                "(For software tests, use the Simulate button instead.)",
            )
        self.check_start_config_ready()

    def check_start_config_ready(self):
        if self.stack_view.currentIndex() == 1:
            hardware_ok = self.simulate or (
                self.hardware_manager and self.hardware_manager.is_loaded
            )
            if (
                hardware_ok
                and self.is_hardware_connected
                and self.session.single_event is not None
            ):
                self.next_bnt.setEnabled(True)
            else:
                self.next_bnt.setEnabled(False)

    def sent_successfull(self):
        self.next_bnt.setEnabled(True)

    def disable_next(self):
        self.next_bnt.setEnabled(False)

    def restart_from_beginning(self):
        self.stack_view.setCurrentIndex(0)
        self.header_frame.hide()
        self.footer_frame.hide()
        self.start_config.setting_window.restart_event()
        self.session.single_event = None
        self.is_hardware_connected = False

        self.back_bnt.setVisible(True)
        self.next_bnt.setVisible(True)

        self.update_header(
            1, "Step 1: Hardware Setup", "Choose mode and connect to device."
        )
        self.disable_next()


# QStackedWidget set the width of all the widgets inside it to the
# the width of the biggest widget
class DynamicStackedWidget(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

    def sizeHint(self):
        # Only return the size hint of the current widget
        if self.currentWidget():
            return self.currentWidget().sizeHint()
        return super().sizeHint()

    def minimumSizeHint(self):
        if self.currentWidget():
            return self.currentWidget().minimumSizeHint()
        return super().minimumSizeHint()
