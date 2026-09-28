import logging

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from models.session_model import SessionModel
from services.analysis_service import AnalysisService
from services.hardware_service import HardwareService
from views.manual_connect_dialog import ManualConnectDialog

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
        self.connection_widget = self.setup_view.connection_widget
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

    # ------------------------------------------------------------------
    # Connection handling (button-driven: auto via the configured udev
    # ports, manual fallback via the per-device dialog — never automatic
    # on config load)
    # ------------------------------------------------------------------

    def on_auto_connect_requested(self):
        if self.setup_view.simulate:
            return

        manager = self.hardware.hardware_manager
        if not manager.is_loaded:
            self.connection_widget.on_connect_result(
                False,
                "No hardware configuration loaded — load a workspace or use Simulate.",
            )
            return

        failed = manager.connect_all()
        if not failed:
            note = (
                " (acquisition is virtual: no data will be collected)"
                if manager.acquisition_is_virtual
                else ""
            )
            self.connection_widget.on_connect_result(
                True, f"All devices connected.{note}"
            )
            return

        self.connection_widget.on_connect_result(
            False,
            "Auto-connect failed — check the cabling and the udev rules "
            "(setup_udev_rules.sh), or map the ports manually.",
        )
        # Auto-connect failure (missing udev rules, device unplugged,
        # Windows, ...) opens the manual-connect dialog directly.
        self._open_manual_connect_dialog(initial_errors=failed)

    def on_manual_connect_requested(self):
        manager = self.hardware.hardware_manager
        if not manager.is_loaded:
            self.connection_widget.on_connect_result(
                False,
                "No hardware configuration loaded — load a workspace or use Simulate.",
            )
            return
        self._open_manual_connect_dialog()

    def _open_manual_connect_dialog(self, initial_errors: dict = None):
        manager = self.hardware.hardware_manager
        dialog = ManualConnectDialog(manager, self.connection_widget, initial_errors=initial_errors)
        dialog.exec()
        self.connection_widget.update_devices()
        if manager.all_role_devices_connected():
            self.connection_widget.on_connect_result(True, "All devices connected.")

    def _bind_signals(self):
        self.connection_widget.auto_connect_requested.connect(self.on_auto_connect_requested)
        self.connection_widget.manual_connect_requested.connect(self.on_manual_connect_requested)
        self.connection_widget.simulate_requested.connect(self.simulate_requested.emit)
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

    def on_apply_configuration_requested(self, config: dict):
        hw_mgr = self.hardware.hardware_manager
        # Session flags are derived from the hardware config, never from the
        # (removed) UI toggles — also on the simulation path, so simulated
        # data format matches the configured channel count.
        self.session.debug_mode = True
        num_active = len(hw_mgr.active_channels) if hw_mgr.is_loaded else 1
        self.session.multiple_channel = (num_active > 1)

        if hw_mgr and hw_mgr.is_loaded:
            g1_label = config.get("gain_ch1_label")
            if g1_label:
                self.session.capacitance_pf_ch1 = hw_mgr.get_capacitance(1, g1_label)
                self.session.gpio_mask_ch1 = hw_mgr.get_gain_mask(1, g1_label)
            g2_label = config.get("gain_ch2_label")
            if g2_label:
                self.session.capacitance_pf_ch2 = hw_mgr.get_capacitance(2, g2_label)
                self.session.gpio_mask_ch2 = hw_mgr.get_gain_mask(2, g2_label)

        # Simulation: there is no hardware to talk to, but the
        # configuration must still be REALIZABLE — a request the timing
        # mirror rejects must not report "Applied Successfully" (B34).
        if self.setup_view.simulate:
            try:
                hw_mgr.validate_timing(
                    sample_ramp=bool(config.get("sample_ramp", False)),
                    ramp_dt_us=config.get("ramp_dt_us"),
                    int_time_us=config["int_time_us"],
                    reset_time_us=config["reset_time_us"],
                )
            except ValueError as exc:
                self.time_window.on_configuration_result(False, str(exc))
                return
            self.time_window.on_configuration_result(True)
            return

        try:
            success = self.send_timing_config(config)
            self.time_window.on_configuration_result(success)
        except ValueError as exc:
            # The timing mirror rejected the configuration (e.g. markers
            # mode with T_int beyond the deployed firmware's dt cap):
            # show the reason, not a generic failure.
            self.time_window.on_configuration_result(False, str(exc))
        except Exception:
            self.time_window.on_configuration_result(False)
            logger.exception("Communication failed")

    def _refresh_session_timing(self, actual, mode: str):
        """Store the driver-mirror timing (the values the hardware will
        actually apply) in the session — including the ramp window geometry
        (samples_per_window / applied dt) the ramp path consumes verbatim:
        re-deriving N = int/dt downstream is lossy and misaligns windows."""
        self.session.timing_mode = mode
        self.session.int_time = actual.int_time_us / 1e6
        self.session.reset_time = actual.reset_time_us / 1e6
        self.session.period_s = actual.period_us / 1e6
        self.session.sample_freq = actual.sample_frequency_hz
        if mode == "ramp":
            self.session.ramp_dt_s = (actual.ramp_dt_eff_us or 0.0) / 1e6
            self.session.samples_per_window = int(actual.samples_per_window or 0)
        elif mode == "markers":
            self.session.ramp_dt_s = 0.0
            self.session.samples_per_window = int(actual.samples_per_window or 2)
        else:
            self.session.ramp_dt_s = 0.0
            self.session.samples_per_window = 0

    def on_timing_calculation_requested(
        self, int_time_s: float, reset_time_s: float, period_s: float = None,
        ramp_dt_us: float = None,
    ):
        hw_mgr = self.hardware.hardware_manager
        if not hw_mgr.timing:
            logger.error("No timing controller assigned")
            return

        period_us = period_s * 1e6 if period_s is not None else None
        # Routed preview: ramp sampling -> the driver's exact mirror; split
        # hardware without ramp sampling -> window markers (dt = int, the
        # degenerate ramp feeding the external digitizer); otherwise plain
        # timing (PIC quantization depends on the channel count, injected
        # by the manager).
        try:
            actual, mode = hw_mgr.preview_timing(
                sample_ramp=ramp_dt_us is not None,
                ramp_dt_us=ramp_dt_us,
                int_time_us=int_time_s * 1e6,
                reset_time_us=reset_time_s * 1e6,
                period_us=period_us,
            )
        except ValueError as exc:
            # The driver's mirror rejected the request (e.g. markers mode
            # with T_int beyond the firmware's dt cap): say why instead of
            # silently showing no-ramp numbers that can never be applied.
            self.time_window.on_configuration_result(False, str(exc))
            return
        if actual is None:
            self.time_window.on_configuration_result(
                False,
                "The timing device cannot realize this configuration "
                "(see the log for the reason).",
            )
            return
        self._refresh_session_timing(actual, mode)
        self.time_window.update_actual_timing(actual)

    def on_mode_switched(self):
        # The SE/ME line belongs to the gain controllers: the command goes to
        # all of them so every circuit receives it (B5).
        if not self.setup_view.simulate:
            manager = self.hardware.hardware_manager
            if manager.is_loaded:
                manager.set_event_mode(self.session.single_event)

    def send_timing_config(self, timer_params: dict) -> bool:
        """Called when user presses 'Send' in the Time window.

        Ordered pipeline; every command's result is honoured. Mode and channel
        count go first: the PIC's timer quantization (TX delay) depends on both,
        so the device must know them before it receives the timer registers.
        """
        hw_mgr = self.hardware.hardware_manager
        timing = hw_mgr.timing
        if not timing:
            return False

        # Data mode: the app always acquires in debug mode (start/stop stored).
        if not timing.set_mode(True):
            logger.error("set_mode(debug) failed on the timing device")
            return False
        self.session.debug_mode = True

        # Channel count: derived from the active channels of the hardware config.
        num_active = len(hw_mgr.active_channels) if hw_mgr.is_loaded else 1
        if not timing.set_channels(num_active > 1):
            logger.error("set_channels failed on the timing device")
            return False
        self.session.multiple_channel = (num_active > 1)

        # Timing registers, routed by the manager: ramp sampling as
        # requested, window markers on split hardware without ramp
        # sampling (dt = int, 2 pulses per window for the digitizer),
        # classic no-ramp otherwise.
        period_us = timer_params.get("period_us")
        sample_ramp = bool(timer_params.get("sample_ramp", False))
        ramp_dt_us = timer_params.get("ramp_dt_us", 10.0)
        if sample_ramp and not hw_mgr.can_ramp_while_acquiring:
            logger.error(
                "Ramp sampling rejected: the timing device cannot generate "
                "ramp triggers while acquiring."
            )
            return False
        # The acquisition device must be able to swallow the trigger rate:
        # 1/dt <= the driver's per-channel stream limit (e.g. the USB-6451
        # samples 1 MS/s per channel).
        max_rate = hw_mgr.acquisition_max_rate_hz
        if sample_ramp and max_rate is not None:
            rate_hz = 1e6 / ramp_dt_us
            if rate_hz > max_rate * (1.0 + 1e-9):
                logger.error(
                    "Ramp sampling rejected: dt = %.3g us implies %.6g S/s per "
                    "channel, above the acquisition device limit of %.6g S/s",
                    ramp_dt_us, rate_hz, max_rate,
                )
                return False
        self.session.timing_mode = hw_mgr.timing_mode_for(sample_ramp)
        self.session.ramp_dt_s = (ramp_dt_us * 1e-6) if sample_ramp else 0.0
        success = hw_mgr.configure_timing(
            sample_ramp,
            ramp_dt_us,
            timer_params["int_time_us"],
            timer_params["reset_time_us"],
            period_us=period_us,
        )
        if not success:
            logger.error("Timing configuration rejected by the timing device")
            return False

        # Refresh the session from the same mirror the hardware was just
        # programmed through (identical inputs -> identical quantized
        # values): the session, the wizard card and the ramp geometry the
        # measurement will chunk on are guaranteed to describe the applied
        # timing even if the preview never ran for these values.
        try:
            actual, mode = hw_mgr.preview_timing(
                sample_ramp, ramp_dt_us,
                timer_params["int_time_us"], timer_params["reset_time_us"],
                period_us=period_us,
            )
            if actual is not None:
                self._refresh_session_timing(actual, mode)
        except ValueError:
            logger.exception("Post-apply timing refresh failed (hardware already configured)")

        # Gains, atomically (B19). CH2 must have a selection whenever it is active.
        g1_label = timer_params.get("gain_ch1_label")
        g2_label = timer_params.get("gain_ch2_label") if num_active > 1 else None
        if num_active > 1 and not g2_label:
            logger.error("CH2 is active in the hardware config but no CH2 gain was selected")
            return False
        if g1_label:
            if not hw_mgr.apply_gains(g1_label, g2_label):
                return False
            self.session.capacitance_pf_ch1 = hw_mgr.get_capacitance(1, g1_label)
            self.session.gpio_mask_ch1 = hw_mgr.get_gain_mask(1, g1_label)
            if g2_label:
                self.session.capacitance_pf_ch2 = hw_mgr.get_capacitance(2, g2_label)
                self.session.gpio_mask_ch2 = hw_mgr.get_gain_mask(2, g2_label)

        # ADC input range.
        if "input_range_min" in timer_params and "input_range_max" in timer_params:
            if not hw_mgr.set_input_range(timer_params["input_range_min"], timer_params["input_range_max"]):
                logger.error("set_input_range failed on the acquisition device")
                return False

        # ADC resolution of this setup (B6): bits from the loaded config
        # drive the histogram bins and the overflow sentinel app-wide.
        adc = hw_mgr.get_adc_settings()
        if adc.get("bits"):
            self.session.adc_bits = int(adc["bits"])

        return True

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
