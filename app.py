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
        # The app always acquires in debug mode (start/stop stored); the
        # operational mode of the electronics is never used.
        self.session_model.debug_mode = True
        self.data_model = DataModel()

        # Initialize Hardware
        from hardware.core.hardware_manager import HardwareManager
        self.hardware_manager = HardwareManager()
        self.hardware_service = HardwareService(
            self.hardware_manager, self.data_model, self.session_model
        )

        # Initialize View
        self.main_window = MainWindow(self.session_model, self.data_model, simulate, self.hardware_manager)

        # Views refresh their hardware-dependent parts when a config is loaded
        self.hardware_manager.add_config_listener(
            lambda _m: self.main_window.setup_view.setting_window.reload_devices()
        )

        # Load the hardware config linked to the active workspace, if any
        self.session_model.workspace_changed.connect(self._on_workspace_changed)

        # Hardware-config operations from the workspace dialog
        setup_view = self.main_window.setup_view
        setup_view.hardware_import_requested.connect(self._on_hardware_import)
        setup_view.hardware_edit_requested.connect(self._on_hardware_edit)
        setup_view.hardware_new_requested.connect(self._on_hardware_new)
        setup_view.hardware_remove_requested.connect(self._on_hardware_remove)

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

    def _load_hardware_config(self, path: str):
        from hardware.config import ConfigValidationError

        try:
            self.hardware_manager.load_config(path)
        except ConfigValidationError as e:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self.main_window,
                "Invalid Hardware Configuration",
                "The hardware configuration file was not accepted "
                "(nothing was connected):\n\n" + "\n".join(f"• {err}" for err in e.errors),
            )
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox

            logger.exception("Failed to load hardware config")
            QMessageBox.critical(
                self.main_window,
                "Hardware Configuration",
                f"Could not read {path}:\n{e}",
            )

    def _on_workspace_changed(self, workspace_dir: str):
        """Load the hardware config linked in the workspace's setup.json."""
        import json
        import os

        from PyQt6.QtWidgets import QMessageBox

        if not workspace_dir:
            return
        from PyQt6.QtWidgets import QMessageBox

        setup_path = os.path.join(workspace_dir, self.session_model.setup_file_name)
        if not os.path.exists(setup_path):
            return
        try:
            with open(setup_path, "r", encoding="utf-8") as f:
                setup = json.load(f)
        except Exception:
            logger.exception("Failed to read setup.json")
            return

        hardware = setup.get("hardware", {})
        config_file = hardware.get("config_file")
        if not config_file:
            QMessageBox.warning(
                self.main_window,
                "No Hardware Config",
                "This workspace has no hardware configuration linked.\n"
                "Real-hardware features stay disabled until one is imported\n"
                "(Workspace Settings > Hardware Configuration).",
            )
            return

        config_path = os.path.join(workspace_dir, config_file)
        if not os.path.exists(config_path):
            QMessageBox.warning(
                self.main_window,
                "Hardware Config Missing",
                f"The linked hardware config {config_file!r} was not found\n"
                f"in the workspace folder.",
            )
            return

        # Load first (validation errors must block), then warn on edits
        # made outside the app (hash mismatch)
        expected_sha = hardware.get("config_sha256")
        self._load_hardware_config(config_path)
        if (
            self.hardware_manager.is_loaded
            and expected_sha
            and self.hardware_manager.config_sha256 != expected_sha
        ):
            QMessageBox.warning(
                self.main_window,
                "Hardware Config Changed",
                f"{config_file!r} differs from the version recorded when the\n"
                f"workspace was created (it was edited outside the app).\n"
                f"Measurements remain usable, but are not directly comparable\n"
                f"with earlier ones of this campaign.",
            )

    def _hardware_library_dirs(self) -> list:
        import os

        return [os.path.join(os.path.dirname(os.path.abspath(__file__)), "hardware_library")]

    def _require_workspace(self) -> bool:
        from PyQt6.QtWidgets import QMessageBox

        if not self.session_model.active_workspace_dir:
            QMessageBox.warning(
                self.main_window,
                "No Workspace",
                "Create or load a workspace first (button 'Create/Save "
                "Workspace' in this dialog): the hardware configuration file "
                "must live in the same folder as its setup.json.",
            )
            return False
        return True

    def _on_hardware_import(self):
        import json
        import os

        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        from hardware.config import ConfigValidationError, hardware_config_from_dict
        from hardware.registry import registry_descriptors
        from services.shift_service import ShiftService

        if not self._require_workspace():
            return
        path, _ = QFileDialog.getOpenFileName(
            self.main_window, "Import Hardware Config", "", "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            hardware_config_from_dict(data, registry_descriptors())
        except ConfigValidationError as e:
            QMessageBox.critical(
                self.main_window,
                "Invalid Hardware Configuration",
                "This file is not a valid hardware configuration "
                "(nothing was changed):\n\n" + "\n".join(f"• {err}" for err in e.errors),
            )
            return
        except Exception as e:
            QMessageBox.critical(self.main_window, "Hardware Config", f"Could not read {path}:\n{e}")
            return

        workspace = self.session_model.active_workspace_dir
        dest = os.path.join(workspace, os.path.basename(path))
        if os.path.abspath(dest) == os.path.abspath(
            os.path.join(workspace, self.session_model.setup_file_name)
        ):
            QMessageBox.critical(
                self.main_window,
                "Reserved File Name",
                f"{os.path.basename(path)} is the name of this workspace's setup "
                f"file. The hardware configuration must use a different name — "
                f"rename the file and import it again.",
            )
            return
        if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(path):
            try:
                with open(dest, "r", encoding="utf-8") as f:
                    same = json.load(f) == data
            except Exception:
                same = False
            if not same:
                reply = QMessageBox.question(
                    self.main_window,
                    "Overwrite Configuration?",
                    f"{os.path.basename(dest)} already exists in this workspace "
                    f"with different content.\nOverwrite it and relink?",
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        copied = ShiftService.link_hardware_config(
            workspace, path, setup_file=self.session_model.setup_file_name
        )
        self._load_hardware_config(copied)
        self.main_window.refresh_shift_dialog()

    def _on_hardware_edit(self):
        import os

        workspace = self.session_model.active_workspace_dir
        link = {}
        if workspace:
            from services.shift_service import ShiftService

            link = ShiftService.read_hardware_link(
                workspace, setup_file=self.session_model.setup_file_name
            )
        config_path = (
            os.path.join(workspace, link["config_file"])
            if workspace and link.get("config_file")
            else None
        )
        from PyQt6.QtWidgets import QMessageBox

        if not config_path or not os.path.exists(config_path):
            QMessageBox.information(
                self.main_window,
                "Hardware Config",
                "No hardware configuration is linked to this workspace.\n"
                "Import one or assemble a new one from components.",
            )
            return

        import json

        with open(config_path, "r", encoding="utf-8") as f:
            initial = json.load(f)
        if self._run_config_editor(initial):
            self.main_window.refresh_shift_dialog()

    def _on_hardware_new(self):
        from hardware.config import editor_model as em

        if not self._require_workspace():
            return
        if self._run_config_editor(em.new_config()):
            self.main_window.refresh_shift_dialog()

    def _run_config_editor(self, initial_config: dict) -> bool:
        """Open the editor; on accept, write and load the configuration.
        Returns True if a config was saved and loaded."""
        import json
        import os

        from PyQt6.QtWidgets import QMessageBox

        from hardware.config import editor_model as em
        from hardware.registry import registry_descriptors
        from services.shift_service import DEFAULT_HARDWARE_CONFIG_NAME, ShiftService
        from views.hardware_config_editor import HardwareConfigEditorDialog

        dialog = HardwareConfigEditorDialog(
            initial_config, self._hardware_library_dirs(), self.main_window
        )
        if not dialog.exec():
            return False

        config = dialog.get_config()
        errors, warnings = em.validate(config, registry_descriptors())
        if errors:
            QMessageBox.critical(self.main_window, "Hardware Config", "\n".join(errors))
            return False

        workspace = self.session_model.active_workspace_dir
        link = ShiftService.read_hardware_link(
            workspace, setup_file=self.session_model.setup_file_name
        )
        config_name = link.get("config_file") or DEFAULT_HARDWARE_CONFIG_NAME
        dest = os.path.join(workspace, config_name)
        if os.path.exists(dest) and not initial_config.get("devices"):
            # creating a NEW configuration over an existing file: confirm.
            # (When editing, dest is the file being edited — overwriting it
            # is the intent.)
            reply = QMessageBox.question(
                self.main_window,
                "Overwrite Configuration?",
                f"{config_name} already exists in this workspace.\nOverwrite it?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

        try:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
        except OSError as e:
            QMessageBox.critical(self.main_window, "Hardware Config", f"Cannot write {dest}:\n{e}")
            return False

        ShiftService.link_hardware_config(
            workspace, dest, setup_file=self.session_model.setup_file_name
        )
        self._load_hardware_config(dest)

        if warnings:
            QMessageBox.warning(self.main_window, "Hardware Config Warnings", "\n".join(warnings))
        return True

    def _on_hardware_remove(self):
        from PyQt6.QtWidgets import QMessageBox

        from services.shift_service import ShiftService

        workspace = self.session_model.active_workspace_dir
        if not workspace:
            return
        reply = QMessageBox.question(
            self.main_window,
            "Remove Hardware Link",
            "Unlink the hardware configuration from this workspace?\n"
            "The file stays on disk; real-hardware features will be disabled.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ShiftService.clear_hardware_link(
            workspace, setup_file=self.session_model.setup_file_name
        )
        self.hardware_manager.unload_config()
        self.main_window.refresh_shift_dialog()

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
