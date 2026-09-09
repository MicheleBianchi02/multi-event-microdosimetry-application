from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal, Qt

class WelcomeWidget(QWidget):
    start_measurement_clicked = pyqtSignal()
    analyze_offline_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("Welcome to ATOMIQA")
        title.setStyleSheet("font-size: 32pt; font-weight: bold; color: #333; margin-bottom: 10px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("What would you like to do?")
        subtitle.setStyleSheet("font-size: 16pt; color: #666; margin-bottom: 40px;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(40)

        self.btn_start = QPushButton("Start New\nMeasurement")
        self.btn_start.setFixedSize(300, 200)
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #0078d7; 
                color: white; 
                font-size: 18pt; 
                font-weight: bold; 
                border-radius: 10px;
            }
            QPushButton:hover { background-color: #005a9e; }
        """)
        self.btn_start.clicked.connect(self.start_measurement_clicked.emit)

        self.btn_analyze = QPushButton("Analyze\nOffline Data")
        self.btn_analyze.setFixedSize(300, 200)
        self.btn_analyze.setStyleSheet("""
            QPushButton {
                background-color: #28a745; 
                color: white; 
                font-size: 18pt; 
                font-weight: bold; 
                border-radius: 10px;
            }
            QPushButton:hover { background-color: #218838; }
        """)
        self.btn_analyze.clicked.connect(self.analyze_offline_clicked.emit)

        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_analyze)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)
