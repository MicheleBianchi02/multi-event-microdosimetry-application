import logging
import time
from datetime import datetime

import numpy as np
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QMessageBox

from core.calc_stat import calculate_results as calc_stat_results
from core.constants import UPDATE_INTERVAL_MS
from core.load_file import load_file
from models.data_model import DataModel
from models.session_model import SessionModel
from services.analysis_service import AnalysisService
from services.hardware_service import HardwareService

logger = logging.getLogger(__name__)


class MeasurementController(QObject):
    """
    Manages the logic during the active measurement phase.
    """

    def __init__(
        self,
        session_model: SessionModel,
        data_model: DataModel,
        hardware_service: HardwareService,
        main_window,
        simulate: bool,
    ):
        super().__init__()
        self.session = session_model
        self.data_model = data_model
        self.hardware = hardware_service
        self.main_window = main_window
        self.running_view = self.main_window.running_view
        self.simulate = simulate
        self.timestamp = 0

        self.is_armed = False
        self.is_running = False
        self.already_runned = False
        self.arming_istant = None
        self.starting_time = 0
        self.rate = None
        self._current_file = None

        self.timer = QTimer()
        self.timer.timeout.connect(self._update_loop)

        # Connect view signals
        self.running_view.start_measurement_requested.connect(
            self.handle_start_measurement
        )
        self.running_view.stop_measurement_requested.connect(
            self.handle_stop_measurement
        )
        self.running_view.calculate_results_requested.connect(self.calculate_results)
        self.running_view.recalculate_state_requested.connect(
            self.handle_recalculate_state
        )
        self.running_view.save_data_requested.connect(self.handle_save_data)
        self.running_view.load_data_requested.connect(self.handle_load_data)

    def handle_start_measurement(self, arm_only: bool):
        if arm_only:
            self.arm_device()
        else:
            self.start_device()

    def handle_recalculate_state(self):
        self.data_model.rebuild_accumulators(
            self.session.rois,
            self.session.sample_freq,
            self.session.time_rois,
        )
        self.running_view.update_dashboard()

    def handle_save_data(self, file_path: str):
        """
        Packages the current measurement data and settings into a ZIP archive.

        Parameters
        ----------
        file_path : str
            The absolute path where the ZIP should be saved.
        """
        from services.data_exporter import DataExporter
        import tempfile
        import os

        previous_timestamp = self.timestamp
        self.timestamp = 0

        # If online saving was active, flush it
        if self._current_file is not None:
            if not self._current_file.closed:
                self._current_file.flush()
            temp_csv = self.session.file_path
        else:
            # We didn't save online (no workspace), so write current memory to a quick temp file
            temp_fd, temp_csv = tempfile.mkstemp(suffix=".csv")
            with os.fdopen(temp_fd, "w") as f:
                if self.session.debug_mode:
                    first_line = "timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop"
                else:
                    first_line = "timestamp,ch1,ch2"
                self._write_data_to_file_object(f, first_line)
                
                if self.session.debug_mode:
                    start_data = self.data_model.start_data[: self.data_model.ptr, :]
                    stop_data = self.data_model.stop_data[: self.data_model.ptr, :]
                    self._write_data_to_file_object(f, (start_data, stop_data))
                else:
                    data = self.data_model.filtered_data[: self.data_model.ptr, :]
                    self._write_data_to_file_object(f, data)
                    
        import datetime
        try:
            start_dt = datetime.datetime.fromtimestamp(self.starting_time).astimezone()
            self.session.timestamp_start = start_dt.isoformat()
            
            if self.data_model.ptr > 0:
                freq = self.session.sample_freq if self.session.sample_freq > 0 else 1.0
                last_time_s = self.data_model.ptr / freq
                end_dt = start_dt + datetime.timedelta(seconds=float(last_time_s))
                self.session.timestamp_end = end_dt.isoformat()
            else:
                self.session.timestamp_end = self.session.timestamp_start
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error calculating timestamps: {e}")

        try:
            import math
            import datetime
            
            n_boots = 1
            if "var_autocov" in self.data_model.result:
                n_boots = max(1, math.ceil(max(self.data_model.result["var_autocov"]["N"][0], self.data_model.result["var_autocov"]["N"][2]) / 10.0))

            analysis_meta = {
                "analysis_timestamp": datetime.datetime.now().astimezone().isoformat(),
                "n_bootstraps": n_boots,
                "rois_time_s": getattr(self.session, "time_rois", []),
                "rois_channel": getattr(self.session, "rois", [])
            }

            success = DataExporter.export_to_zip(
                session_model=self.session,
                temp_csv_path=temp_csv,
                zip_filepath=file_path,
                results_dict=self.data_model.result,
                analysis_meta=analysis_meta
            )
            
            if success:
                self.running_view.already_saved = True
                self.session.file_path = file_path  # Remember the ZIP path
                self.running_view.change_start_bnt("update", hide=False, enable=True)
                
                # Cleanup temp files if we saved online
                if getattr(self.session, 'active_workspace_dir', None):
                    import os
                    json_tmp = os.path.join(self.session.active_workspace_dir, ".active_run.json.tmp")
                    csv_tmp = os.path.join(self.session.active_workspace_dir, ".active_run.csv.tmp")
                    if os.path.exists(json_tmp): os.remove(json_tmp)
                    if os.path.exists(csv_tmp): os.remove(csv_tmp)
                    
                QMessageBox.information(self.main_window, "Success", "Measurement saved successfully.")
            else:
                QMessageBox.critical(self.main_window, "Error", "Failed to package and save measurement.")
        finally:
            self.timestamp = previous_timestamp
            # Clean up temporary file created if there was no active workspace
            if self._current_file is None and os.path.exists(temp_csv):
                os.remove(temp_csv)

    def handle_load_data(self, file_path: str):
        """
        Loads measurement data from a CSV file into the application state.

        Parameters
        ----------
        file_path : str
            The absolute path to the CSV file to load.
        """
        try:
            result = load_file(file_path)
        except Exception as e:
            logger.exception("Failed to load data")
            QMessageBox.warning(self.main_window, "Error", f"Failed to load: {str(e)}")
            return

        try:
            self.running_view.init_variables()
    
            self.session.int_time = result["int_time"]
            self.session.reset_time = result["reset_time"]
            self.session.sample_freq = result["sample_freq"]
            self.session.debug_mode = result["debug_mode"]
    
            elapsed_time = result["elapsed_time"]
            data = result["data"]
    
            # self.session.multiple_channel is determined below
    
            self.data_model.clear()
            
            # Zero out the existing result values BEFORE attempting to load new ones,
            # otherwise old values will bleed into the newly loaded view.
            for macro in self.data_model.result.values():
                for param in macro:
                    macro[param] = [0, 0, 0, 0]
        except Exception as e:
            logger.exception("Failed to initialize load data")
            QMessageBox.warning(self.main_window, "Error", f"Failed to initialize load data: {str(e)}")
            return
        
        try:
            settings = result.get("settings")
            if settings:
                # Extract timestamp from JSON
                timestamp_iso = settings.get("measurement_info", {}).get("timestamp_start")
                if timestamp_iso:
                    from datetime import datetime
                    try:
                        self.starting_time = datetime.fromisoformat(timestamp_iso).timestamp()
                    except Exception:
                        import time
                        self.starting_time = time.time()
                else:
                    import time
                    self.starting_time = time.time()
                    
                # Restore timing settings from JSON (overrides dummy 0.0 values from CSV parser)
                timing = settings.get("timing_settings", {})
                self.session.int_time = timing.get("integration_time_s", self.session.int_time)
                self.session.reset_time = timing.get("reset_time_s", self.session.reset_time)
                self.session.sample_freq = timing.get("sample_frequency_hz", self.session.sample_freq)
                
                # Restore measurement info
                meas_info = settings.get("measurement_info", {})
                self.session.is_problematic = meas_info.get("is_problematic", False)
                self.session.timestamp_start = meas_info.get("timestamp_start", "")
                self.session.timestamp_end = meas_info.get("timestamp_end", "")
                
                # Restore experimental setup
                exp_setup = settings.get("experimental_setup", {})
                self.session.facility = exp_setup.get("facility", getattr(self.session, "facility", ""))
                self.session.beam_type = exp_setup.get("beam_type", getattr(self.session, "beam_type", ""))
                self.session.beam_energy_mev = exp_setup.get("beam_energy_MeV", getattr(self.session, "beam_energy_mev", 0.0))
                self.session.beam_current_na = exp_setup.get("beam_current_nA", getattr(self.session, "beam_current_na", 0.0))
                self.session.depth_cm = exp_setup.get("depth_cm", getattr(self.session, "depth_cm", 0.0))
                self.session.detector_voltage_v = exp_setup.get("detector_voltage_V", getattr(self.session, "detector_voltage_v", 0.0))
                self.session.calibration_slope = exp_setup.get("calibration_slope", getattr(self.session, "calibration_slope", 1.0))
                self.session.calibration_intercept = exp_setup.get("calibration_intercept", getattr(self.session, "calibration_intercept", 0.0))
                self.session.shift_note = exp_setup.get("shift_note", getattr(self.session, "shift_note", ""))
                self.session.measurement_note = exp_setup.get("measurement_note", getattr(self.session, "measurement_note", ""))
                
                hw_settings = settings.get("hardware_settings", {})
                versions = settings.get("versions", {})
                self.session.hardware_id_ch1 = versions.get("hardware_id_ch1", versions.get("hardware_id", getattr(self.session, "hardware_id_ch1", "")))
                self.session.hardware_id_ch2 = versions.get("hardware_id_ch2", versions.get("hardware_id", getattr(self.session, "hardware_id_ch2", "")))
                self.session.app_version = versions.get("app_version", getattr(self.session, "app_version", ""))
                self.session.firmware_version = versions.get("firmware_version", getattr(self.session, "firmware_version", ""))
                
                self.session.adc_num_bits = hw_settings.get("adc_num_bits", getattr(self.session, "adc_num_bits", 12))
                self.session.adc_vref_volts = hw_settings.get("adc_vref_volts", getattr(self.session, "adc_vref_volts", 5.0))
                # single_event is not loaded from json
                
                ch1_settings = hw_settings.get("channels", {}).get("ch1", {})
                ch2_settings = hw_settings.get("channels", {}).get("ch2", {})
                self.session.capacitor_base_values_ch1 = ch1_settings.get("capacitor_base_values_pF", getattr(self.session, "capacitor_base_values_ch1", [22.0, 100.0, 560.0, 3000.0]))
                self.session.capacitor_base_values_ch2 = ch2_settings.get("capacitor_base_values_pF", getattr(self.session, "capacitor_base_values_ch2", [22.0, 100.0, 560.0, 3000.0]))
                self.session.capacitor_code_ch1 = ch1_settings.get("capacitor_code", getattr(self.session, "capacitor_code_ch1", "1000"))
                self.session.capacitor_code_ch2 = ch2_settings.get("capacitor_code", getattr(self.session, "capacitor_code_ch2", "1000"))
    
                analysis = settings.get("analysis", {})
                self.session.analysis_timestamp = analysis.get("analysis_timestamp", "")
                self.session.rois = analysis.get("rois_channel") or []
                self.session.time_rois = analysis.get("rois_time_s") or []
            else:
                self.starting_time = self._read_starting_time(file_path)
                self.session.rois = None
                self.session.time_rois = []
                
            # Determine single_channel_mode
            sc_mode = None
            if settings:
                hw_settings = settings.get("hardware_settings", {})
                if "single_channel_mode" in hw_settings:
                    sc_mode = hw_settings["single_channel_mode"]
                    
            if sc_mode is None:
                reply = QMessageBox.question(
                    self.main_window,
                    "Mode Selection",
                    "Was this measurement recorded in Single Channel Mode? (Check Yes if only CH1 was active).",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                sc_mode = (reply == QMessageBox.StandardButton.Yes)
                
            self.session.multiple_channel = not sc_mode
    
            if self.session.debug_mode:
                self.data_model.append_debug_measurements(
                    result["start_value"], result["stop_value"]
                )
    
            self.session.file_path = file_path
    
            self.data_model.append_measurements(data)
            self.data_model.rebuild_accumulators(
                self.session.rois,
                self.session.sample_freq,
                self.session.time_rois,
            )
            
            if settings:
                analysis = settings.get("analysis", {})
                # Safely merge results matching current schema AFTER accumulators are rebuilt
                loaded_res = analysis.get("results", {})
                if loaded_res:
                    try:
                        for macro, params in loaded_res.items():
                            if macro in self.data_model.result:
                                for p_name, values in params.items():
                                    if p_name in self.data_model.result[macro]:
                                        self.data_model.result[macro][p_name] = values
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error(f"Failed to parse results from JSON: {e}")
    
            self.running_view.update_after_load(elapsed_time)
            self.main_window.change_to_running_window()
            
        except Exception as e:
            logger.exception("Failed to process loaded data")
            QMessageBox.warning(self.main_window, "Error", f"Failed to process data: {str(e)}")

    def arm_device(self):
        """
        Sends the arm command to the hardware device to prepare for measurement.
        """
        if not self.running_view.simulate:
            acq = self.hardware.hardware_manager.acquisition
            if not acq:
                QMessageBox.critical(self.running_view, "Error", "No acquisition hardware available.")
                return
                
            response = acq.arm()
            if not response:
                QMessageBox.critical(
                    self.running_view, "Error", "Arming command failed."
                )
                return

        self.running_view.change_start_bnt("start", hide=False, enable=True)
        self.arming_istant = time.time()
        self.is_armed = True

    def start_device(self):
        """
        Sends the start command to the hardware device and begins the measurement loop.
        """
        if self.arming_istant is None or time.time() - self.arming_istant < 3:
            QMessageBox.critical(
                self.running_view,
                "Error",
                "You must wait at least 3 seconds after arming.",
            )
            return

        simulate = self.simulate
        if simulate:
            period = self.session.int_time + self.session.reset_time
            if period <= 0:
                QMessageBox.critical(
                    self.running_view,
                    "Error",
                    "Invalid timing configuration for simulation.",
                )
                return
            self.rate = 10**-3 / period
            logger.info(
                "Simulating a rate of %s measures/ms for each channel", self.rate
            )
        else:
            pass # Hardware device will be started in start_receiving

        self.data_model.clear()
        self.timestamp = 0
        self.starting_time = time.time()
        self.session.timestamp_start = datetime.now().astimezone().isoformat()
        self.session.timestamp_end = None
        self.session.is_problematic = False

        if getattr(self.session, 'active_workspace_dir', None) is None:
            self.running_view.already_saved = False
            self.session.file_path = None
        else:
            import os
            self.session.file_path = os.path.join(self.session.active_workspace_dir, ".active_run.csv.tmp")
            if self.session.debug_mode:
                first_line = "timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop"
            else:
                first_line = "timestamp,ch1,ch2"
                
            try:
                self._current_file = open(self.session.file_path, "w")
                self._write_data_to_file_object(self._current_file, first_line)
                self.running_view.already_saved = False # Not fully saved yet
                
                # Save a temporary JSON copy of settings for crash recovery
                from services.data_exporter import DataExporter
                import json
                json_path = os.path.join(self.session.active_workspace_dir, ".active_run.json.tmp")
                with open(json_path, "w") as jf:
                    json.dump(DataExporter.build_settings_dict(self.session), jf, indent=4)
                    
            except PermissionError:
                QMessageBox.critical(self.main_window, "Error", "Permission denied: Cannot write to workspace location.")
                self.running_view.already_saved = False
                self.session.file_path = None
            except OSError:
                QMessageBox.critical(self.main_window, "Error", "System error: Cannot write temporary file.")
                self.running_view.already_saved = False
                self.session.file_path = None

        success = self.hardware.start_receiving(simulate=simulate, rate=self.rate)
        if not success:
            QMessageBox.critical(
                self.running_view, "Error", "Start command failed."
            )
            return

        self.is_running = True
        self.already_runned = True

        self.running_view.change_start_bnt("stop", hide=False, enable=True)
        self.running_view.restart_bnt.setEnabled(False)

        if self.running_view.chart_type == "mc":
            self.running_view.init_mc_chart(update=True)
        elif self.running_view.chart_type == "time":
            self.running_view.init_time_chart(update=True)

        self._update_loop(update_time=True)
        self.timer.start(UPDATE_INTERVAL_MS)

    def handle_stop_measurement(self):
        """
        Stops the hardware device from acquiring data and halts the UI update loop.
        """
        simulate = self.simulate
        response = self.hardware.stop_receiving(not simulate)

        if self.already_runned:
            self._update_loop(update_time=True)
            
            # Explicitly close the file to guarantee OS-level sync
            if hasattr(self, "_current_file") and self._current_file is not None:
                if not self._current_file.closed:
                    self._current_file.close()
                    
            self.is_running = False
            self.timer.stop()
            self.running_view.restart_bnt.setEnabled(True)

        self.running_view.change_start_bnt("save", hide=False, enable=True)
        self.is_running = False
        self.is_armed = False
        if self.session.timestamp_end is None:
            self.session.timestamp_end = datetime.now().astimezone().isoformat()

        if not response:
            QMessageBox.critical(self.running_view, "Error", "Stop command failed.")

    def _update_loop(self, update_time: bool = True):
        if self.hardware.has_error():
            logger.error("Hardware service reported an error during receiving data.")
            self.hardware.clear_error()
            self.is_running = False
            self.running_view.change_start_bnt(None, hide=True, enable=False)
            self.timer.stop()
            raw_data = []
        else:
            raw_data = self.data_model.get_latest_batch()

        measure_time = round((time.time() - self.starting_time), 1)
        if update_time:
            self.running_view.measuring_time_label.setText(f"{measure_time} s")

        if not raw_data or len(raw_data) == 0:
            return

        result = self.data_model.ingest_raw_batch(
            raw_batch=raw_data,
            debug_mode=self.session.debug_mode,
            rois=self.session.rois,
            sample_frequency=self.session.sample_freq,
            time_rois=self.session.time_rois,
        )

        # Notify view to update plots/table
        self.running_view.update_dashboard()

        if self.session.file_path is not None and self._current_file is not None:
            try:
                if self.session.debug_mode:
                    start_value, stop_value, new_data = result
                    self._write_data_to_file_object(self._current_file, (start_value, stop_value))
                else:
                    self._write_data_to_file_object(self._current_file, result)
                self._current_file.flush()
            except Exception as e:
                logger.error(f"Error writing to file: {e}")
                self.running_view.already_saved = False
                self.session.file_path = None
                try:
                    self._current_file.close()
                except Exception:
                    pass
                self._current_file = None

    def _write_data_to_file_object(self, f, data) -> None:
        """Write the given data directly to an open file object."""
        freq = self.session.sample_freq if self.session.sample_freq > 0 else 1.0
        if isinstance(data, (list, np.ndarray)):
            lines = []
            for val in data:
                self.timestamp += 1
                ts = round(self.timestamp / freq, 7)
                ch1 = int(val[0]) if not np.isnan(val[0]) else val[0]
                ch2 = int(val[1]) if not np.isnan(val[1]) else val[1]
                lines.append(f"{ts},{ch1},{ch2}\n")
            f.write("".join(lines))
        elif isinstance(data, str):
            f.write(f"{data}\n")
        elif isinstance(data, tuple):
            start_val, stop_val = data
            lines = []
            for i in range(len(start_val)):
                self.timestamp += 1
                ts = round(self.timestamp / freq, 7)
                s1 = (
                    int(start_val[i][0])
                    if not np.isnan(start_val[i][0])
                    else start_val[i][0]
                )
                s2 = (
                    int(start_val[i][1])
                    if not np.isnan(start_val[i][1])
                    else start_val[i][1]
                )
                e1 = (
                    int(stop_val[i][0])
                    if not np.isnan(stop_val[i][0])
                    else stop_val[i][0]
                )
                e2 = (
                    int(stop_val[i][1])
                    if not np.isnan(stop_val[i][1])
                    else stop_val[i][1]
                )
                lines.append(f"{ts},{s1},{e1},{s2},{e2}\n")
            f.write("".join(lines))

    def _format_starting_time(self) -> str:
        return datetime.fromtimestamp(self.starting_time).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]

    def _read_starting_time(self, file_path: str) -> float:
        try:
            with open(file_path, "r") as f:
                header_line = f.readline().strip()
        except OSError:
            return time.time()

        marker = "starting time :"
        if marker not in header_line:
            return time.time()

        value = header_line.split(marker, 1)[1].strip()
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except ValueError:
            return time.time()

    def calculate_results(
        self,
        start: int,
        end: int,
        cov_both_ch: bool,
        window_points: int,
        plot_only: bool = False,
        fft_mode: str = "psd",
        fft_downsample: str = "mean",
        fft_max_points: int = 4000,
    ):
        """
        Calculates and renders comprehensive statistics and time-series analyses.

        Parameters
        ----------
        start : int
            The starting index of the data slice to analyze.
        end : int
            The ending index of the data slice.
        cov_both_ch : bool
            Whether to calculate the covariance between the two channels.
        window_points : int
            The number of points for the moving average window.
        plot_only : bool
            If True, skips the table UI update.
        fft_mode : str
            FFT mode: "psd" or "magnitude"
        fft_downsample : str
            Downsampling method for FFT: "mean" or "max"
        fft_max_points : int
            Maximum points in the FFT output array
        """
        if not self.data_model or self.data_model.count == 0:
            return

        data = self.data_model.filtered_data[start:end, :]
        
        if not plot_only:
            new_result = calc_stat_results(
                data=data,
                ch1=True,
                ch2=self.session.multiple_channel,
                cov_both_ch=cov_both_ch,
            )
            self.data_model.result["var"] = new_result["var"]
            self.data_model.result["var_cov"] = new_result["var_cov"]
            self.data_model.result["var_doublecov"] = new_result["var_doublecov"]
            self.data_model.result["var_autocov"] = new_result["var_autocov"]

        ma_dict = {0: {}, 1: {}}
        fft_dict = {0: {}, 1: {}}

        ch2_active = self.session.multiple_channel
        for idx in [0, 1]:
            if not ch2_active and idx == 1:
                continue
            data_matrix = data[:, idx]
            data_matrix = data_matrix[~np.isnan(data_matrix)]
            n = len(data_matrix)
            if n > 2:
                # MA & Variance
                ma, mvar = AnalysisService.calculate_moving_average_variance(
                    data_matrix, window_points
                )
                ma_dict[idx] = {"ma": ma, "mvar": mvar, "n": n}
                
                # FFT
                freqs, mag_db = AnalysisService.calculate_fft(
                    data_matrix, self.session.sample_freq, fft_mode, fft_downsample, fft_max_points
                )
                fft_dict[idx] = {"freqs": freqs, "mag_db": mag_db}


        self.running_view.render_analysis(
            ma_dict, fft_dict, ch2_active, fft_mode
        )

        # Auto-update the ZIP file with analysis results if already saved
        if False:
            from services.data_exporter import DataExporter
            from datetime import datetime
            analysis_meta = {
                "analysis_timestamp": datetime.now().astimezone().isoformat(),
                "n_bootstraps": max(1, __import__("math").ceil(max(self.data_model.result["var_autocov"]["N"][0], self.data_model.result["var_autocov"]["N"][2]) / 10.0)),
                "rois_time_s": getattr(self.session, 'time_rois', []),
                "rois_channel": getattr(self.session, 'rois', [])
            }
            DataExporter.update_analysis_in_zip(self.session.file_path, self.data_model.result, analysis_meta)
