import logging
import sys
import warnings

from PyQt6.QtCore import QLocale, QObject
from PyQt6.QtWidgets import QApplication

from controllers.measurement_controller import MeasurementController
from controllers.setup_controller import SetupController
from models.data_model import DataModel
from models.session_model import SessionModel
from services.hardware_service import HardwareService
from views.main_window import View as MainWindow

warnings.filterwarnings("ignore", message="All-NaN slice encountered")

logger = logging.getLogger(__name__)


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


class AppController(QObject):
    def __init__(self, simulate: bool, baud_rate: int, single_channel: bool):
        super().__init__()

        # Initialize Models
        self.session_model = SessionModel()
        self.session_model.baud_rate = baud_rate
        self.session_model.multiple_channel = not single_channel
        self.session_model.debug_mode = False
        self.data_model = DataModel()

        # Initialize Hardware
        from hardware.core.hardware_manager import HardwareManager
        self.hardware_manager = HardwareManager()
        # TODO: handle missing hardware_config.json elegantly, for now load if exists
        import os
        config_path = "hardware_config.json"
        if os.path.exists(config_path):
            self.hardware_manager.load_config(config_path)
            
        self.hardware_service = HardwareService(
            self.hardware_manager, self.data_model, self.session_model
        )

        # Initialize View
        self.main_window = MainWindow(self.session_model, self.data_model, simulate, self.hardware_manager)

        # Initialize Controllers
        self.setup_controller = SetupController(
            self.session_model, self.hardware_service, self.main_window
        )
        self.measurement_controller = MeasurementController(
            self.session_model,
            self.data_model,
            self.hardware_service,
            self.main_window,
            simulate,
        )

        # Connect Main Window events
        self.setup_controller.simulate_requested.connect(self._on_simulate_requested)

    def _on_simulate_requested(self):
        logger.info("Simulation requested via UI.")
        # Enable simulation across controllers and views
        self.measurement_controller.simulate = True
        self.main_window.simulate = True
        self.main_window.setup_view.simulate = True
        self.main_window.setup_view.setting_window.simulate = True
        self.main_window.setup_view.time_window.simulate = True
        self.main_window.running_view.simulate = True

        # Refresh the port table to show Simulated Port if not already present
        self.setup_controller.refresh_ports()

        # Select the first row (Simulated Port)
        self.main_window.setup_view.setting_window.port_table.selectRow(0)

        # Automatically connect to simulation
        self.main_window.setup_view.setting_window.selected_port = "Simulated Port"
        self.main_window.setup_view.setting_window.on_connection_result(
            True, "Simulation connection established."
        )

    def _on_stop_measurement(self):
        self.measurement_controller.handle_stop_measurement()

    def run(self):
        self.main_window.show()


def main(simulate: bool, baud_rate: int, single_channel: bool, debug: bool = False):
    configure_logging(debug)
    logger.debug(
        "Starting application: simulate=%s baud_rate=%s single_channel=%s",
        simulate,
        baud_rate,
        single_channel,
    )

    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))

    controller = AppController(simulate, baud_rate, single_channel)
    controller.run()

    sys.exit(app.exec())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--simulate", action="store_true", default=False)
    parser.add_argument("-b", "--baud-rate", action="store", default=115200, type=int)
    parser.add_argument("-c", "--single-channel", action="store_true", default=False)
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug logging to stderr",
    )
    args = parser.parse_args()

    main(args.simulate, args.baud_rate, args.single_channel, args.debug)
