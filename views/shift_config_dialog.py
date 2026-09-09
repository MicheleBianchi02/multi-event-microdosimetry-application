from PyQt6.QtWidgets import QDialog, QVBoxLayout
from views.shift_config_widget import ShiftConfigWidget

class ShiftConfigDialog(QDialog):
    def __init__(self, session_model, parent=None):
        super().__init__(parent)
        self.session = session_model
        self.setWindowTitle("Workspace & Shift Configuration")
        self.setMinimumWidth(500)
        
        layout = QVBoxLayout(self)
        self.widget = ShiftConfigWidget(session_model, self)
        layout.addWidget(self.widget)
