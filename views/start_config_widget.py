from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from views.mode_window import OperationModeWindow
from views.setting_window import SettingWindow

class StartConfigWidget(QWidget):
    def __init__(self, session_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        
        self.mode_window = OperationModeWindow(self.session)
        self.setting_window = SettingWindow(self.session, self.simulate, self.hardware_manager)
        
        self.init_ui()
        
    def init_ui(self):
        layout = QHBoxLayout(self)
        
        # Left side: Mode
        left_layout = QVBoxLayout()
        left_lbl = QLabel("1. Select Mode")
        left_lbl.setStyleSheet("font-weight: bold; font-size: 16pt;")
        left_layout.addWidget(left_lbl)
        left_layout.addWidget(self.mode_window)
        
        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        
        # Right side: Hardware
        right_layout = QVBoxLayout()
        right_lbl = QLabel("2. Connect Hardware")
        right_lbl.setStyleSheet("font-weight: bold; font-size: 16pt;")
        right_layout.addWidget(right_lbl)
        right_layout.addWidget(self.setting_window)
        
        layout.addLayout(left_layout)
        layout.addWidget(line)
        layout.addLayout(right_layout)
