from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class OperationModeWindow(QWidget):
    mode_selected = pyqtSignal(bool)  # Emits True if Single Event, False if Multi Event

    def __init__(self, session_model):
        super().__init__()
        self.session = session_model
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.addStretch(1)

        title = QLabel("Select Operation Mode")
        title.setStyleSheet("font-size: 24pt; font-weight: bold; color: #333;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Choose the operational mode of the device before proceeding."
        )
        subtitle.setStyleSheet("font-size: 14pt; color: #666; margin-bottom: 20px;")
        layout.addWidget(subtitle)

        btn_layout = QHBoxLayout()

        self.btn_single = QPushButton("Single Event Mode")
        self.btn_single.setFixedSize(300, 150)
        self.btn_single.setCheckable(True)
        self.btn_single.setCheckable(True)
        self.btn_single.setStyleSheet("""
            QPushButton {
                background-color: #28a745; 
                color: white; 
                font-size: 18pt; 
                font-weight: bold; 
                border-radius: 10px;
                border: 2px solid transparent;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:checked { border: 4px solid #333; }
        """)
        self.btn_single.clicked.connect(lambda: self.select_mode(True))

        self.btn_multi = QPushButton("Multi Event Mode\n(Integration)")
        self.btn_multi.setFixedSize(300, 150)
        self.btn_multi.setCheckable(True)
        self.btn_multi.setCheckable(True)
        self.btn_multi.setStyleSheet("""
            QPushButton {
                background-color: #007bff; 
                color: white; 
                font-size: 18pt; 
                font-weight: bold; 
                border-radius: 10px;
                border: 2px solid transparent;
            }
            QPushButton:hover { background-color: #0069d9; }
            QPushButton:checked { border: 4px solid #333; }
        """)
        self.btn_multi.clicked.connect(lambda: self.select_mode(False))

        btn_layout.addWidget(self.btn_single)
        btn_layout.addSpacing(20)
        btn_layout.addWidget(self.btn_multi)

        layout.addLayout(btn_layout)
        layout.addStretch(1)

    def select_mode(self, is_single_event: bool):
        self.session.single_event = is_single_event
        if is_single_event:
            self.btn_single.setChecked(True)
            self.btn_multi.setChecked(False)
        else:
            self.btn_multi.setChecked(True)
            self.btn_single.setChecked(False)
        self.mode_selected.emit(is_single_event)


class SingleEventWaitWindow(QWidget):
    change_to_multi = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.addStretch(1)

        title = QLabel("Single Event Mode Active")
        title.setStyleSheet("font-size: 24pt; font-weight: bold; color: #28a745;")
        layout.addWidget(title)

        subtitle = QLabel(
            "The device is connected and operating in Single Event Mode.\nNo data is collected by this application in this mode."
        )
        subtitle.setStyleSheet("font-size: 14pt; color: #666; margin-bottom: 20px;")
        layout.addWidget(subtitle)

        self.btn_switch = QPushButton("Switch to Multi Event Mode")
        self.btn_switch.setFixedSize(400, 60)
        self.btn_switch.setStyleSheet("""
            QPushButton {
                background-color: #ffc107; 
                color: #333; 
                font-size: 16pt; 
                font-weight: bold; 
                border-radius: 8px;
            }
            QPushButton:hover { background-color: #e0a800; }
        """)
        self.btn_switch.clicked.connect(self.change_to_multi.emit)

        layout.addWidget(self.btn_switch)
        layout.addStretch(1)
