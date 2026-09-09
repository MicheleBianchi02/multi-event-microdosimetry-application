from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea, QPushButton
from PyQt6.QtCore import Qt
from views.time_window import TimeWindow

class RunConfigWidget(QWidget):
    def __init__(self, session_model, simulate: bool, hardware_manager=None):
        super().__init__()
        self.session = session_model
        self.simulate = simulate
        self.hardware_manager = hardware_manager
        
        self.time_window = TimeWindow(self.session, self.simulate, self.hardware_manager)
        
        self.init_ui()
        
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.time_window)
        
    def save_configuration(self):
        """Called right before switching to the running window"""
        pass
