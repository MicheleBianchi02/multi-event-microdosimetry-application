import json
import os
import logging
import shutil
from PyQt6.QtWidgets import QMessageBox

logger = logging.getLogger(__name__)

DEFAULT_HARDWARE_CONFIG_NAME = "hardware_config.json"


class ShiftService:
    """Reads/writes the campaign setup.json and its hardware-config link.

    setup.json carries only campaign metadata plus a link to the hardware
    configuration file living in the same folder; everything hardware
    (ADC settings, capacitors, wiring) lives in the hardware config.
    """

    @staticmethod
    def save_setup(session_model, file_path: str, hardware_config_source: str = None) -> bool:
        """
        Saves the campaign setup; optionally copies the given hardware config
        file into the workspace folder and links it (name + sha256).
        """
        from core.constants import APP_VERSION

        try:
            folder = os.path.dirname(file_path)
            hardware_block = {}

            # preserve an existing link if the user does not provide a new one
            existing = {}
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            hardware_block = existing.get("hardware", {})

            if hardware_config_source:
                dest = os.path.join(folder, DEFAULT_HARDWARE_CONFIG_NAME)
                shutil.copy2(hardware_config_source, dest)
                from hardware.config import file_sha256
                hardware_block = {
                    "config_file": DEFAULT_HARDWARE_CONFIG_NAME,
                    "config_sha256": file_sha256(dest),
                }
            elif hardware_block.get("config_file"):
                linked = os.path.join(folder, hardware_block["config_file"])
                if not os.path.exists(linked):
                    hardware_block = {}

            setup_data = {
                "versions": {"app_version": session_model.app_version or APP_VERSION},
                "experimental_setup": {
                    "facility": session_model.facility,
                    "beam_type": session_model.beam_type,
                    "shift_note": session_model.shift_note,
                },
                "hardware": hardware_block,
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(setup_data, f, indent=4)

            session_model.active_workspace_dir = folder
            return True
        except Exception:
            logger.exception("Failed to save setup.json")
            return False

    @staticmethod
    def load_setup(session_model, file_path: str) -> bool:
        """
        Loads campaign metadata from setup.json into the SessionModel.
        (The hardware config itself is loaded by the app controller through
        the hardware manager.) Old-format files are accepted; their hardware
        fields are ignored.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                setup_data = json.load(f)

            versions = setup_data.get("versions", {})
            session_model.app_version = versions.get("app_version", session_model.app_version)

            exp_setup = setup_data.get("experimental_setup", {})
            session_model.facility = exp_setup.get("facility", session_model.facility)
            session_model.beam_type = exp_setup.get("beam_type", session_model.beam_type)
            session_model.shift_note = exp_setup.get("shift_note", session_model.shift_note)

            session_model.active_workspace_dir = os.path.dirname(file_path)
            return True
        except Exception:
            logger.exception("Failed to load setup.json")
            return False

    @staticmethod
    def check_for_recovery(folder_path: str) -> bool:
        """Checks if there are orphaned temp files in the given workspace directory."""
        csv_tmp = os.path.join(folder_path, ".active_run.csv.tmp")
        json_tmp = os.path.join(folder_path, ".active_run.json.tmp")
        return os.path.exists(csv_tmp) and os.path.exists(json_tmp)

    @staticmethod
    def recover_data(folder_path: str, parent_widget) -> bool:
        """Packages orphaned temp files into a ZIP archive and deletes the temp files."""
        import zipfile
        from PyQt6.QtWidgets import QFileDialog

        csv_tmp = os.path.join(folder_path, ".active_run.csv.tmp")
        json_tmp = os.path.join(folder_path, ".active_run.json.tmp")

        save_path, _ = QFileDialog.getSaveFileName(
            parent_widget,
            "Save Recovered Data",
            folder_path,
            "SYNC Measurement Files (*.zip)"
        )
        if not save_path:
            return False

        if not save_path.endswith(".zip"):
            save_path += ".zip"

        try:
            with zipfile.ZipFile(save_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(csv_tmp, "data.csv")
                zf.write(json_tmp, "settings.json")
                # provenance copies written at measurement start, if present
                for tmp_name, arc_name in (
                    (".active_run.hardware_config.json.tmp", "hardware_config.json"),
                    (".active_run.setup.json.tmp", "setup.json"),
                ):
                    tmp = os.path.join(folder_path, tmp_name)
                    if os.path.exists(tmp):
                        zf.write(tmp, arc_name)
            os.remove(csv_tmp)
            os.remove(json_tmp)
            QMessageBox.information(
                parent_widget, "Success", "Measurement data recovered and saved successfully!"
            )
            return True
        except Exception as e:
            logger.exception("Failed to recover data")
            QMessageBox.critical(parent_widget, "Error", f"Failed to recover data: {e}")
            return False
