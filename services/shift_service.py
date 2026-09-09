import json
import os
import logging
from PyQt6.QtWidgets import QMessageBox

logger = logging.getLogger(__name__)

class ShiftService:
    @staticmethod
    def save_setup(session_model, file_path: str) -> bool:
        """
        Saves the static setup parameters from the SessionModel into the given JSON file.
        """
        from core.constants import APP_VERSION
        try:
            setup_data = {
                "versions": {
                    "app_version": session_model.app_version,
                    "firmware_version": session_model.firmware_version or session_model.app_version,
                    "hardware_id_ch1": session_model.hardware_id_ch1,
                    "hardware_id_ch2": session_model.hardware_id_ch2
                },
                "experimental_setup": {
                    "facility": session_model.facility,
                    "beam_type": session_model.beam_type,
                    "shift_note": session_model.shift_note
                },
                "hardware_settings": {
                    "adc_num_bits": session_model.adc_num_bits,
                    "adc_vref_volts": session_model.adc_vref_volts,
                    "channels": {
                        "ch1": {
                            "capacitor_base_values_pF": session_model.capacitor_base_values_ch1
                        },
                        "ch2": {
                            "capacitor_base_values_pF": session_model.capacitor_base_values_ch2
                        }
                    }
                }
            }
            with open(file_path, 'w') as f:
                json.dump(setup_data, f, indent=4)
            
            session_model.active_workspace_dir = os.path.dirname(file_path)
            return True
        except Exception as e:
            logger.exception("Failed to save setup.json")
            return False

    @staticmethod
    def load_setup(session_model, file_path: str) -> bool:
        """
        Loads the static setup parameters from setup.json into the SessionModel.
        """
        try:
            with open(file_path, 'r') as f:
                setup_data = json.load(f)
                
            versions = setup_data.get("versions", {})
            
            # For backward compatibility, check hardware_id if ch1/ch2 don't exist
            session_model.app_version = versions.get("app_version", "")
            session_model.hardware_id_ch1 = versions.get("hardware_id_ch1", versions.get("hardware_id", ""))
            session_model.hardware_id_ch2 = versions.get("hardware_id_ch2", versions.get("hardware_id", ""))
            session_model.firmware_version = versions.get("firmware_version", "")
            
            exp_setup = setup_data.get("experimental_setup", {})
            session_model.facility = exp_setup.get("facility", "")
            session_model.beam_type = exp_setup.get("beam_type", "")
            session_model.shift_note = exp_setup.get("shift_note", "")
            
            from core.constants import NUM_BIT
            hw = setup_data.get("hardware_settings", {})
            session_model.adc_num_bits = hw.get("adc_num_bits", NUM_BIT)
            session_model.adc_vref_volts = hw.get("adc_vref_volts", 5.0)
            
            hw_settings = hw.get("channels", {})
            session_model.capacitor_base_values_ch1 = hw_settings.get("ch1", {}).get("capacitor_base_values_pF", [22.0, 100.0, 560.0, 3000.0])
            session_model.capacitor_base_values_ch2 = hw_settings.get("ch2", {}).get("capacitor_base_values_pF", [22.0, 100.0, 560.0, 3000.0])
            
            # Update the workspace path
            folder_path = os.path.dirname(file_path)
            session_model.active_workspace_dir = folder_path
            return True
        except Exception as e:
            logger.exception("Failed to load setup.json")
            return False

    @staticmethod
    def check_for_recovery(folder_path: str) -> bool:
        """Checks if there are orphaned temp files in the given workspace directory."""
        import os
        csv_tmp = os.path.join(folder_path, ".active_run.csv.tmp")
        json_tmp = os.path.join(folder_path, ".active_run.json.tmp")
        return os.path.exists(csv_tmp) and os.path.exists(json_tmp)

    @staticmethod
    def recover_data(folder_path: str, parent_widget) -> bool:
        """Packages orphaned temp files into a ZIP archive and deletes the temp files."""
        import os
        import json
        import zipfile
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        
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
            with open(json_tmp, "r") as jf:
                settings_dict = json.load(jf)
                
            with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('settings.json', json.dumps(settings_dict, indent=4))
                zf.write(csv_tmp, 'data.csv')
                
            os.remove(csv_tmp)
            os.remove(json_tmp)
            QMessageBox.information(parent_widget, "Success", "Measurement data recovered and saved successfully!")
            return True
        except Exception as e:
            QMessageBox.critical(parent_widget, "Error", f"Failed to recover data: {e}")
            return False
