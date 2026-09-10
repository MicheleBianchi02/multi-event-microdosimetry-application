import json
import zipfile
import os
import shutil
from datetime import datetime

from core.constants import APP_VERSION
import logging

logger = logging.getLogger(__name__)

class DataExporter:
    
    @staticmethod
    def build_settings_dict(session_model, results_dict=None, analysis_meta=None, hardware_manager=None):
        """
        Builds the complete JSON dictionary based on the session model.
        The hardware section is a snapshot of the loaded hardware config and
        the values actually applied (gpio masks, ADC range) — read-only for
        the offline analysis (B4/B6).
        """
        if results_dict is None:
            results_dict = {}

        if analysis_meta is None:
            analysis_meta = {
                "analysis_timestamp": None,
                "n_bootstraps": None,
                "rois_time_s": getattr(session_model, 'time_rois', []),
                "rois_channel": getattr(session_model, 'rois', [])
            }

        manager = hardware_manager
        loaded = manager is not None and manager.is_loaded

        # Optional: extract timestamps if they are available in session, else use current
        timestamp = datetime.now().astimezone().isoformat()
        t_start = getattr(session_model, 'timestamp_start', None) or timestamp
        t_end = getattr(session_model, 'timestamp_end', None) or timestamp

        if loaded:
            adc = manager.get_adc_settings()
            hardware_settings = {
                "adc": {
                    "bits": adc.get("bits"),
                    "input_range_volts": adc.get("input_range_volts"),
                },
                "hardware_config_sha256": manager.config_sha256,
                "hardware": manager.devices_summary(),
            }
        else:
            hardware_settings = {}

        channels_out = {}
        for ch in ("ch1", "ch2"):
            mask = getattr(session_model, f'gpio_mask_{ch}', None)
            capacitance = getattr(session_model, f'capacitance_pf_{ch}', None)
            if mask is not None or capacitance:
                channels_out[ch] = {
                    "gpio_mask": list(mask) if mask is not None else None,
                    "capacitance_pF": capacitance,
                }
        hardware_settings["channels"] = channels_out
        hardware_settings["single_channel_mode"] = not getattr(session_model, 'multiple_channel', True)

        settings = {
            "file_format_version": "3.0",
            "versions": {
                "app_version": getattr(session_model, 'app_version', APP_VERSION),
            },
            "measurement_info": {
                "timestamp_start": t_start,
                "timestamp_end": t_end,
                "acquisition_mode": "debug" if getattr(session_model, 'debug_mode', False) else "operational",
                "is_problematic": getattr(session_model, 'is_problematic', False)
            },
            "experimental_setup": {
                "facility": getattr(session_model, 'facility', ""),
                "beam_type": getattr(session_model, 'beam_type', ""),
                "particle": getattr(session_model, 'particle', ""),
                "beam_energy_MeV": getattr(session_model, 'beam_energy_mev', 0.0),
                "beam_current_nA": getattr(session_model, 'beam_current_na', 0.0),
                "depth_cm": getattr(session_model, 'depth_cm', 0.0),
                "detector_voltage_V": getattr(session_model, 'detector_voltage_v', 0.0),
                "calibration_slope": getattr(session_model, 'calibration_slope', 1.0),
                "calibration_intercept": getattr(session_model, 'calibration_intercept', 0.0),
                "shift_note": getattr(session_model, 'shift_note', ""),
                "measurement_note": getattr(session_model, 'measurement_note', "")
            },
            "hardware_settings": hardware_settings,
            "timing_settings": {
                "integration_time_s": getattr(session_model, 'int_time', 0.0),
                "reset_time_s": getattr(session_model, 'reset_time', 0.0),
                "period_s": getattr(session_model, 'int_time', 0.0) + getattr(session_model, 'reset_time', 0.0),
                "sample_frequency_hz": getattr(session_model, 'sample_freq', 0.0)
            },
            "analysis": {
                "analysis_timestamp": analysis_meta.get("analysis_timestamp"),
                "n_bootstraps": analysis_meta.get("n_bootstraps"),
                "rois_time_s": analysis_meta.get("rois_time_s", []),
                "rois_channel": analysis_meta.get("rois_channel", []),
                "results": results_dict
            }
        }
        
        return settings

    @staticmethod
    def export_to_zip(session_model, temp_csv_path: str, zip_filepath: str, results_dict=None, analysis_meta=None, hardware_manager=None, hardware_config_text=None, setup_json_text=None) -> bool:
        """
        Bundles the CSV data, settings and (v3) verbatim copies of the
        hardware config and campaign setup into a self-contained zip.
        """
        try:
            settings_dict = DataExporter.build_settings_dict(
                session_model, results_dict, analysis_meta, hardware_manager
            )
            settings_json = json.dumps(settings_dict, indent=4)

            with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
                # 1. Write the settings JSON
                zf.writestr('settings.json', settings_json)

                # 2. Add the CSV file
                if temp_csv_path and os.path.exists(temp_csv_path):
                    zf.write(temp_csv_path, 'data.csv')
                else:
                    # If there's no temp file (e.g. testing), write an empty placeholder
                    zf.writestr('data.csv', 'timestamp,ch1,ch2\n')

                # 3. Embed provenance: the exact hardware config and campaign
                #    setup used for this run (absent in pre-3.0 archives)
                if hardware_config_text:
                    zf.writestr('hardware_config.json', hardware_config_text)
                if setup_json_text:
                    zf.writestr('setup.json', setup_json_text)

            return True
        except Exception as e:
            logger.exception("Failed to export to zip")
            return False

    @staticmethod
    def update_analysis_in_zip(zip_filepath: str, results_dict: dict, analysis_meta: dict) -> bool:
        """
        Updates the settings.json file inside an existing zip archive with new analysis results.
        """
        temp_zip_path = zip_filepath + ".tmp"
        try:
            with zipfile.ZipFile(zip_filepath, 'r') as zf_in:
                settings_path = None
                for name in zf_in.namelist():
                    if name.endswith("settings.json"):
                        settings_path = name
                        break
                        
                if not settings_path:
                    raise KeyError("There is no settings.json in the archive")
                
                # Parse existing settings
                with zf_in.open(settings_path) as f:
                    settings_dict = json.load(f)
                
                # Update analysis block
                settings_dict.setdefault("analysis", {})
                settings_dict["analysis"]["analysis_timestamp"] = analysis_meta.get("analysis_timestamp")
                settings_dict["analysis"]["n_bootstraps"] = analysis_meta.get("n_bootstraps")
                settings_dict["analysis"]["rois_time_s"] = analysis_meta.get("rois_time_s", [])
                settings_dict["analysis"]["rois_channel"] = analysis_meta.get("rois_channel", [])
                settings_dict["analysis"]["results"] = results_dict
                
                settings_json = json.dumps(settings_dict, indent=4)
                
                # Write to new zip
                with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf_out:
                    zf_out.writestr(settings_path, settings_json)
                    
                    # Copy all other files
                    for item in zf_in.infolist():
                        if item.filename != settings_path:
                            zf_out.writestr(item, zf_in.read(item.filename))
                            
            # Replace old zip with new one
            os.replace(temp_zip_path, zip_filepath)
            return True
        except Exception as e:
            logger.exception("Failed to update analysis in zip")
            if os.path.exists(temp_zip_path):
                os.remove(temp_zip_path)
            return False
