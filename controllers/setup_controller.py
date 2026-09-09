import logging

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from models.session_model import SessionModel
from services.analysis_service import AnalysisService
from services.hardware_service import HardwareService
from services.serial_device import get_port

logger = logging.getLogger(__name__)


class SetupController(QObject):
    """
    Handles logic for the setup wizard, separating business logic from the UI.
    """

    simulate_requested = pyqtSignal()

    def __init__(
        self,
        session_model: SessionModel,
        hardware_service: HardwareService,
        main_window,
    ):
        super().__init__()
        self.session = session_model
        self.hardware = hardware_service
        self.main_window = main_window
        # Get references to the specific setup views
        self.setup_view = self.main_window.setup_view
        self.setting_window = self.setup_view.setting_window
        self.time_window = self.setup_view.time_window
        self.analysis_view = self.time_window.analysis_widget

        # Setup analysis data
        self.plot_data_bragg = AnalysisService.load_bragg_data("data/pristine")
        self.plot_data_sobp = AnalysisService.load_sobp_data("data/SOBP")
        self.plot_data = self.plot_data_bragg
        self.unique_energies = (
            sorted(list(set(d["beam_energy"] for d in self.plot_data.values())))
            if self.plot_data
            else []
        )
        self.analysis_view.set_initial_data(self.unique_energies)

        self._bind_signals()
        self.auto_connect()

    def auto_connect(self):
        if self.setup_view.simulate:
            return

        if not self.hardware.hardware_manager.is_loaded:
            self.setting_window.on_connection_result(
                False,
                "No hardware configuration loaded — use 'Load Hardware Config' or Simulate.",
            )
            return

        failed = self.check_connection()
        if not failed:
            note = (
                " (acquisition is virtual: no data will be collected)"
                if self.hardware.hardware_manager.acquisition_is_virtual
                else ""
            )
            self.setting_window.on_connection_result(
                True, f"All devices auto-connected successfully.{note}"
            )
        else:
            QMessageBox.warning(
                self.setting_window, 
                "Hardware Connection Failed", 
                f"Could not auto-connect to devices: {', '.join(failed)}\n\nPlease connect them manually using the panel."
            )

    def _bind_signals(self):
        self.setting_window.connect_requested.connect(self.on_connect_requested)
        self.setting_window.ports_refresh_requested.connect(self.refresh_ports)
        self.setting_window.simulate_requested.connect(self.simulate_requested.emit)
        self.time_window.apply_configuration_requested.connect(
            self.on_apply_configuration_requested
        )
        self.time_window.timing_calculation_requested.connect(
            self.on_timing_calculation_requested
        )
        self.setup_view.mode_switched.connect(self.on_mode_switched)

        # Analysis signals
        self.analysis_view.mode_changed_signal.connect(self.on_analysis_mode_changed)
        self.analysis_view.plot_base_requested.connect(self.on_analysis_plot_base)
        self.analysis_view.calc_g_requested.connect(self.on_analysis_calc_g)
        self.analysis_view.plot_int_requested.connect(self.on_analysis_plot_int)
        self.analysis_view.plot_unc_requested.connect(self.on_analysis_plot_unc)
        self.analysis_view.export_requested.connect(self.on_analysis_export)
        self.analysis_view.custom_import_requested.connect(self.on_custom_import)

        self.refresh_ports()

    def refresh_ports(self):
        self.setting_window.fill_port_table(get_port())

    def on_connect_requested(self, dev_id: str, port: str):
        try:
            self.hardware.hardware_manager.set_device_port(dev_id, port)
            failed = self.check_connection()
            if not failed:
                self.setting_window.on_connection_result(
                    True, "All devices connected successfully."
                )
            else:
                self.setting_window.on_connection_result(
                    False, f"Devices failed: {', '.join(failed)}"
                )
        except Exception as e:
            self.setting_window.on_connection_result(False, f"Serial Error: {str(e)}")

    def on_apply_configuration_requested(self, config: dict):
        hw_mgr = self.hardware.hardware_manager
        if hw_mgr and hw_mgr.is_loaded:
            g1_label = config.get("gain_ch1_label")
            if g1_label:
                self.session.capacitance_pf_ch1 = hw_mgr.get_capacitance(1, g1_label)
                self.session.gpio_mask_ch1 = hw_mgr.get_gain_mask(1, g1_label)
            g2_label = config.get("gain_ch2_label")
            if g2_label:
                self.session.capacitance_pf_ch2 = hw_mgr.get_capacitance(2, g2_label)
                self.session.gpio_mask_ch2 = hw_mgr.get_gain_mask(2, g2_label)

        # We check if simulate is true. If so, return success immediately.
        if self.setup_view.simulate:
            self.time_window.on_configuration_result(True)
            return

        try:
            success = self.send_timing_config(config)
            self.time_window.on_configuration_result(success)
        except Exception:
            self.time_window.on_configuration_result(False)
            logger.exception("Communication failed")

    def on_timing_calculation_requested(self, int_time_s: float, reset_time_s: float):
        timing_controller = self.hardware.hardware_manager.timing
        if not timing_controller:
            logger.error("No timing controller assigned")
            return
            
        act_int_us, act_rst_us = timing_controller.calculate_actual_timing_us(
            int_time_s * 1e6,
            reset_time_s * 1e6,
            self.session.debug_mode,
            self.session.multiple_channel,
        )
        self.session.int_time = act_int_us / 1e6
        self.session.reset_time = act_rst_us / 1e6
        period_s = (act_int_us + act_rst_us) / 1e6
        self.session.sample_freq = 1 / period_s if period_s > 0 else 0
        self.time_window.update_label()

    def check_connection(self) -> list[str]:
        """Called when user presses 'Connect' or during auto-connect. Returns list of failed device IDs."""
        return self.hardware.hardware_manager.connect_all()

    def on_mode_switched(self):
        # The SE/ME line belongs to the gain controllers: the command goes to
        # all of them so every circuit receives it (B5).
        if not self.setup_view.simulate:
            manager = self.hardware.hardware_manager
            if manager.is_loaded:
                manager.set_event_mode(self.session.single_event)

    def send_timing_config(self, timer_params: dict) -> bool:
        """Called when user presses 'Send' in the Time window."""
        timing = self.hardware.hardware_manager.timing
        if not timing:
            return False

        hw_mgr = self.hardware.hardware_manager

        if timer_params.get("sample_ramp", False):
            if not hw_mgr.can_ramp_while_acquiring:
                logger.error(
                    "Ramp sampling rejected: the timing device cannot generate "
                    "ramp triggers while acquiring."
                )
                self.time_window.on_configuration_result(False)
                return False
            success = timing.configure_ramp(
                timer_params.get("ramp_dt_us", 10.0),
                timer_params["int_time_us"],
                timer_params["reset_time_us"]
            )
        else:
            success = timing.configure_noramp(
                timer_params["int_time_us"],
                timer_params["reset_time_us"]
            )
            
        # Legacy PIC DAQ might still have these. For now we call them if they exist
        # on the timing device, otherwise we ignore. 
        if success and hasattr(timing, "set_mode"):
            success = timing.set_mode(timer_params.get("debug_mode", False))
            
        if success and hasattr(timing, "set_channels"):
            success = timing.set_channels(timer_params.get("multiple_channel", False))

        # Handle Gains
        g1_label = timer_params.get("gain_ch1_label")
        if success and g1_label:
            success = hw_mgr.apply_gain(1, g1_label)
            if success:
                self.session.capacitance_pf_ch1 = hw_mgr.get_capacitance(1, g1_label)
                self.session.gpio_mask_ch1 = hw_mgr.get_gain_mask(1, g1_label)

        g2_label = timer_params.get("gain_ch2_label")
        if success and g2_label:
            success = hw_mgr.apply_gain(2, g2_label)
            if success:
                self.session.capacitance_pf_ch2 = hw_mgr.get_capacitance(2, g2_label)
                self.session.gpio_mask_ch2 = hw_mgr.get_gain_mask(2, g2_label)

        return success

    # --- Analysis Handlers ---
    def on_analysis_mode_changed(self, mode: str):
        if mode == "bragg":
            self.plot_data = self.plot_data_bragg
        else:
            self.plot_data = self.plot_data_sobp
        self.unique_energies = (
            sorted(list(set(d["beam_energy"] for d in self.plot_data.values())))
            if self.plot_data
            else []
        )
        self.analysis_view.set_initial_data(self.unique_energies)

    def on_analysis_plot_base(self, energy_str: str, mode: str):
        if not self.plot_data:
            QMessageBox.warning(self.analysis_view, "Data Error", "No data loaded.")
            return

        if mode == "bragg":
            try:
                beam_energy = float(energy_str)
            except ValueError:
                QMessageBox.warning(
                    self.analysis_view, "Input Error", "Invalid beam energy."
                )
                return
        else:
            beam_energy = energy_str

        selected_keys = [
            k for k, v in self.plot_data.items() if v["beam_energy"] == beam_energy
        ]
        if not selected_keys:
            QMessageBox.warning(
                self.analysis_view, "Data Error", "No data found for selected energy."
            )
            return

        max_z_overall = 0
        for k in selected_keys:
            max_z_k = self.plot_data[k]["z"][-1]
            if max_z_k > max_z_overall:
                max_z_overall = max_z_k

        self.analysis_view.update_base_plots(
            selected_keys, self.plot_data, max_z_overall
        )

    def on_analysis_calc_g(self, params: dict):
        s_param = params["s_param"]
        selected_keys = params["selected_keys"]
        mode = params["mode"]
        input_val = params["rate"]
        l = params["l"]
        i_leakage = params["i_leakage"]
        current_max_z = params["current_max_z"]

        target_key = None
        for k in selected_keys:
            if self.plot_data[k]["step"] == s_param:
                target_key = k
                break

        if target_key is None:
            QMessageBox.warning(
                self.analysis_view, "Data Error", "Could not find matching step data."
            )
            return

        target_data = self.plot_data[target_key]
        z_array = target_data["z"]
        let_array = target_data["let"]
        e_array = target_data["e"]

        if mode == "sobp":
            fluence_array = target_data["f"]
            fluence_0 = np.interp(0.0, z_array, fluence_array)
            rate = input_val * fluence_0
        else:
            rate = input_val

        let_0 = np.interp(0.0, z_array, let_array) * 1000.0
        E_0 = np.interp(0.0, z_array, e_array)
        E_z_max = np.interp(current_max_z, z_array, e_array)

        if E_z_max == 0:
            QMessageBox.warning(
                self.analysis_view,
                "Calculation Error",
                "Energy at selected z_max is 0. Cannot divide by 0.",
            )
            return

        calculated_g, q_0 = AnalysisService.calculate_g_parameter(
            let_0, l, i_leakage, rate, E_0, E_z_max
        )
        self.analysis_view.update_g_result(
            target_key,
            target_data,
            current_max_z,
            E_0,
            E_z_max,
            calculated_g,
            q_0,
            let_0,
            rate,
        )

    def on_analysis_plot_int(self, params: dict):
        target_data = params["target_data"]
        mode = params["mode"]
        g_use = params["g_use"]
        Q_min_C = params["Q_min_C"]
        Q_max_C = params["Q_max_C"]
        y_bar_D_min_keV = params["y_bar_D_min_keV"]
        y_bar_D_max_keV = params["y_bar_D_max_keV"]
        rate_input = params["rate"]
        n_min = params["n_min"]
        l = params["l"]
        q_0 = params["q_0"]
        E_0 = params["E_0"]
        c_tot = params["c_tot"]

        z_array = target_data["z"]
        e_array = target_data["e"]
        let_array = target_data["let"]

        if mode == "bragg":
            y_bar_D_min_eV_um = y_bar_D_min_keV * 1000.0
            y_bar_D_max_eV_um = y_bar_D_max_keV * 1000.0
            rate = rate_input
        else:
            y_bar_D_eV_um_array = target_data["y_bar_D_eV_um"]
            y_bar_D_min_eV_um = y_bar_D_eV_um_array
            y_bar_D_max_eV_um = y_bar_D_eV_um_array
            i_beam_na = rate_input
            fluence_array = target_data["f"]
            fluence_0 = np.interp(0.0, z_array, fluence_array)
            rate = i_beam_na * fluence_0

        results = AnalysisService.calculate_integration_times(
            z_array,
            e_array,
            let_array * 1000.0,
            rate,
            q_0,
            g_use,
            E_0,
            Q_min_C,
            Q_max_C,
            y_bar_D_min_eV_um,
            y_bar_D_max_eV_um,
            n_min,
            l,
        )

        clip_val = 1e-15
        for k in [
            "T_int_min_z",
            "T_int_min_comp1",
            "T_int_min_comp2",
            "T_int_max_min_z",
            "T_int_max_max_z",
            "T_int_max_comp1",
            "T_int_max_comp2_min",
            "T_int_max_comp2_max",
            "T_exp_min",
            "T_exp_max",
        ]:
            results[k] = np.clip(results[k], clip_val, None)

        phase3_results = results.copy()
        phase3_results.update(
            {
                "z_array": z_array,
                "y_bar_D_min_eV_um": y_bar_D_min_eV_um,
                "y_bar_D_max_eV_um": y_bar_D_max_eV_um,
                "g_use": g_use,
                "l": l,
                "c_tot": c_tot,
            }
        )
        self.analysis_view.update_phase3_plots(phase3_results)

    def on_analysis_plot_unc(self, params: dict):
        phase3_results = params["phase3_results"]
        tm_max = params["tm_max"]
        epsilon = params["epsilon"]
        custom_tint_ms = params["custom_tint_ms"]
        chk_custom_tint = params["chk_custom_tint"]

        z_array = phase3_results["z_array"]
        T_int_min_z = phase3_results["T_int_min_z"]
        T_int_max_qmax = phase3_results["T_int_max_comp1"]
        y_bar_D_min_eV_um = phase3_results["y_bar_D_min_eV_um"]
        y_bar_D_max_eV_um = phase3_results["y_bar_D_max_eV_um"]
        I_z = phase3_results["I_z"]
        g_use = phase3_results["g_use"]
        l = phase3_results["l"]

        W = 30.0
        e_const = 1.6e-19

        if chk_custom_tint:
            T_int_selected = np.full_like(z_array, custom_tint_ms / 1000.0)
        else:
            T_int_selected = np.minimum(T_int_min_z, T_int_max_qmax)

        y_bar_D_charge_min = y_bar_D_min_eV_um / W * e_const * l * g_use
        y_bar_D_charge_max = y_bar_D_max_eV_um / W * e_const * l * g_use
        with np.errstate(divide="ignore", invalid="ignore"):
            T_exp_min = y_bar_D_charge_min / I_z
            T_exp_min = np.nan_to_num(T_exp_min, posinf=0.0, neginf=0.0)

            T_exp_max = y_bar_D_charge_max / I_z
            T_exp_max = np.nan_to_num(T_exp_max, posinf=0.0, neginf=0.0)

            Tm_min_z = (2 * T_int_selected + T_exp_min) / (epsilon**2)
            Tm_max_z = (2 * T_int_selected + T_exp_max) / (epsilon**2)

        Tm_min_final = np.minimum(Tm_min_z, tm_max)
        Tm_max_final = np.minimum(Tm_max_z, tm_max)

        clip_val = 1e-15
        Tm_min_final = np.clip(Tm_min_final, clip_val, None)
        Tm_max_final = np.clip(Tm_max_final, clip_val, None)

        c_tot_F = phase3_results["c_tot"] * 1e-12
        mean_V = I_z * T_int_selected / c_tot_F

        term_min = np.clip(I_z * T_int_selected * y_bar_D_charge_min, 0, None)
        term_max = np.clip(I_z * T_int_selected * y_bar_D_charge_max, 0, None)
        std_V_min = np.sqrt(term_min) / c_tot_F
        std_V_max = np.sqrt(term_max) / c_tot_F

        phase4_results = {
            "z_array": z_array,
            "Tm_min": Tm_min_final,
            "Tm_max": Tm_max_final,
            "mean_V": mean_V,
            "std_V_min": std_V_min,
            "std_V_max": std_V_max,
        }
        self.analysis_view.update_phase4_plots(phase4_results)

    def on_analysis_export(self, params: dict):
        folder = params["folder"]
        data_dict = params["data_dict"]
        try:
            AnalysisService.export_data(folder, data_dict)
            QMessageBox.information(
                self.analysis_view, "Success", "Data exported successfully."
            )
        except Exception as e:
            QMessageBox.critical(
                self.analysis_view, "Export Error", f"Failed to export data:\n{e}"
            )

    def on_custom_import(self, file_path: str):
        try:
            from services.analysis_service import AnalysisService

            setups = AnalysisService.import_custom_setups(file_path)
            if setups:
                self.analysis_view.add_imported_setups(setups)
                from PyQt6.QtWidgets import QMessageBox

                QMessageBox.information(
                    self.analysis_view,
                    "Import Successful",
                    f"Successfully loaded {len(setups)} setups.",
                )
            else:
                from PyQt6.QtWidgets import QMessageBox

                QMessageBox.warning(
                    self.analysis_view,
                    "Import Empty",
                    "No valid setups found in the CSV file.",
                )
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.critical(
                self.analysis_view, "Import Error", f"Failed to import data:\\n{e}"
            )
