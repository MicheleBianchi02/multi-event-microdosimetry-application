import logging

import numpy as np
from numpy.typing import NDArray

from core.bragg_peak import (
    calc_g_parameter,
    calc_integration_times,
    export_plot_data,
    load_fluka_data,
    load_sobp_data,
)
from core.data_utils import filter_data_by_rois as _filter_data_by_rois

logger = logging.getLogger(__name__)


try:
    from statsmodels.tsa.stattools import acf, pacf
except ImportError:
    acf = None
    pacf = None


class AnalysisService:
    """
    Handles heavy mathematical calculations (Moving Averages, Variance, FFT, ACF, PACF)
    extracted from the View to prevent blocking the UI and breaking MVC.
    """

    @staticmethod
    def filter_data_by_rois(data: NDArray, rois: list | None) -> NDArray:
        """
        Filters data based on a list of Regions of Interest (ROIs).

        Parameters
        ----------
        data : NDArray
            The input data array.
        rois : list of tuple or None
            The ROIs to apply.

        Returns
        -------
        NDArray
            The filtered data.
        """
        return _filter_data_by_rois(data, rois)

    @staticmethod
    def calculate_moving_average_variance(data: NDArray, window_points: int):
        """
        Calculates simple moving average and variance using valid convolution.

        Parameters
        ----------
        data : NDArray
            The input 1D data array.
        window_points : int
            The size of the moving average window.

        Returns
        -------
        tuple of (NDArray, NDArray)
            A tuple containing the moving average array and the moving variance array.
        """
        if window_points <= len(data):
            ma = np.convolve(data, np.ones(window_points) / window_points, mode="valid")
            ma_sq = np.convolve(
                data**2, np.ones(window_points) / window_points, mode="valid"
            )
            mvar = np.maximum(0, ma_sq - ma**2)
            return ma, mvar
        return np.array([]), np.array([])

    @staticmethod
    def calculate_acf(data: NDArray, n: int):
        """
        Calculates the Autocorrelation Function (ACF) using FFT.

        Parameters
        ----------
        data : NDArray
            The input 1D data array.
        n : int
            The number of lags to compute (typically the length of the data).

        Returns
        -------
        NDArray
            The autocorrelation values.
        """
        if n == 0:
            return np.array([])
        if acf is None:
            acf_values = np.zeros(n)
            acf_values[0] = 1.0
            return acf_values
        if np.ptp(data) > 0:
            acf_values = acf(data, nlags=n - 1, fft=True)
        else:
            acf_values = np.zeros(n)
            acf_values[0] = 1.0
        return acf_values

    @staticmethod
    def calculate_pacf(data: NDArray, pacf_nlags: int):
        """
        Calculates the Partial Autocorrelation Function (PACF).

        Parameters
        ----------
        data : NDArray
            The input 1D data array.
        pacf_nlags : int
            The number of lags to compute for the PACF.

        Returns
        -------
        NDArray
            The partial autocorrelation values.
        """
        if pacf is None:
            pacf_values = np.zeros(max(2, pacf_nlags + 1))
            pacf_values[0] = 1.0
            return pacf_values
        if pacf_nlags > 0 and np.ptp(data) > 0:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pacf_values = pacf(data, nlags=pacf_nlags, method="yw")
        else:
            pacf_values = np.zeros(max(2, pacf_nlags + 1))
            pacf_values[0] = 1.0
        return pacf_values

    @staticmethod
    def calculate_fft(
        data: NDArray,
        sample_freq: float,
        mode: str = "psd",
        downsample_method: str = "mean",
        max_points: int = 4000,
    ):
        """
        Calculates the Fast Fourier Transform in dB.

        Parameters:
            mode: "psd" for Power Spectral Density (dB/Hz), "magnitude" for Peak Amplitude (dBV).
            downsample_method: "mean" or "max" pooling.
        """
        n = len(data)
        if n < 2 or sample_freq <= 0:
            return np.array([]), np.array([])

        freqs = np.fft.rfftfreq(n, d=1.0 / sample_freq)

        if mode == "magnitude":
            # Forward norm divides by n, giving mean linear amplitude per bin
            fft_values = np.fft.rfft(data, norm="forward")
            vals = np.abs(fft_values)
            db_multiplier = 20.0
        elif mode == "psd":
            fft_values = np.fft.rfft(data)
            vals = (np.abs(fft_values) ** 2) / (sample_freq * n)
            db_multiplier = 10.0
        else:
            raise ValueError(f"Unknown mode '{mode}'. Choose 'psd' or 'magnitude'.")

        # Single-sided scaling: fold negative frequency power into AC bins
        if n % 2 == 0:
            vals[1:-1] *= 2.0
        else:
            vals[1:] *= 2.0

        # Downsample if resolution exceeds max_points
        if len(freqs) > max_points:
            bin_edges = np.round(np.linspace(0, len(freqs), max_points + 1)).astype(int)
            starts = bin_edges[:-1]
            bin_sizes = np.diff(bin_edges)

            if downsample_method == "max":
                vals = np.maximum.reduceat(vals, starts)
            elif downsample_method == "mean":
                vals = np.add.reduceat(vals, starts) / bin_sizes
            else:
                raise ValueError(
                    f"Unknown downsample_method '{downsample_method}'. Choose 'mean' or 'max'."
                )

            freqs = np.add.reduceat(freqs, starts) / bin_sizes

        vals_db = db_multiplier * np.log10(vals + 1e-18)

        return freqs, vals_db

    @staticmethod
    def load_bragg_data(path="data/pristine"):
        try:
            return load_fluka_data(path)
        except Exception as e:
            logger.warning("Could not load Bragg data: %s", e)
            return {}

    @staticmethod
    def load_sobp_data(path="data/SOBP"):
        try:
            return load_sobp_data(path)
        except Exception as e:
            logger.warning("Could not load SOBP data: %s", e)
            return {}

    @staticmethod
    def calculate_g_parameter(let_0, l, i_leakage, rate, E_0, E_z_max):
        return calc_g_parameter(let_0, l, i_leakage, rate, E_0, E_z_max)

    @staticmethod
    def calculate_integration_times(
        z_array,
        e_array,
        let_array_eV_um,
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
    ):
        return calc_integration_times(
            z_array,
            e_array,
            let_array_eV_um,
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

    @staticmethod
    def export_data(folder_path, data_dict):
        export_plot_data(folder_path, data_dict)

    @staticmethod
    def import_custom_setups(file_path: str) -> list:
        import csv

        setups = []
        with open(file_path, "r", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers:
                return setups
            for row in reader:
                if len(row) < 18:
                    continue
                try:
                    data = {
                        "energy": float(row[0]),
                        "beam_curr": float(row[1]),
                        "z": float(row[2]),
                        "yf": float(row[3]),
                        "yd": float(row[4]),
                        "g": float(row[5]),
                        "rate": float(row[6]),
                        "current_pa": float(row[7]),
                        "l": float(row[8]),
                        "n_min": float(row[9]),
                        "cap": float(row[10]),
                        "vmin": float(row[11]),
                        "vmax": float(row[12]),
                        "t_int_min": float(row[13]),
                        "t_int_max": float(row[14]),
                        "t_m_min": float(row[15]),
                        "t_m_max": float(row[15]),
                        "mean_v": float(row[16]),
                        "std_v": float(row[17]),
                    }
                    setups.append(data)
                except ValueError:
                    continue
        return setups

    @staticmethod
    def calculate_custom_setup(
        energy,
        beam_curr,
        z,
        yf,
        yd,
        g,
        val,
        is_rate,
        l,
        n_min,
        cap,
        vmin,
        vmax,
        tm_max,
        eps,
        c_tint,
        use_custom_tint,
    ):
        import numpy as np

        W = 30.0
        e_const = 1.6e-19
        yf_eV_um = yf * 1000.0
        yd_eV_um = yd * 1000.0

        q_0 = yf_eV_um * l / W * e_const

        if is_rate:
            rate = val
            current_pa = (rate * g * q_0) * 1e12
        else:
            current_pa = val
            rate = (current_pa * 1e-12) / (g * q_0) if q_0 > 0 and g > 0 else 0.0

        Q_min_C = (cap * 1e-12) * vmin
        Q_max_C = (cap * 1e-12) * vmax

        z_array = np.array([z])
        e_array = np.array([1.0])
        let_array = np.array([yf_eV_um])

        from core.bragg_peak import calc_integration_times

        res_int = calc_integration_times(
            z_array,
            e_array,
            let_array,
            rate,
            q_0,
            g,
            1.0,
            Q_min_C,
            Q_max_C,
            yd_eV_um,
            yd_eV_um,
            n_min,
            l,
        )

        T_int_min_z = res_int["T_int_min_z"]
        T_int_max_qmax = res_int["T_int_max_comp1"]
        I_z = res_int["I_z"]

        if use_custom_tint:
            T_int_selected = np.full_like(z_array, c_tint / 1000.0)
        else:
            T_int_selected = np.minimum(T_int_min_z, T_int_max_qmax)

        y_bar_D_charge = yd_eV_um / W * e_const * l * g
        with np.errstate(divide="ignore", invalid="ignore"):
            T_exp = y_bar_D_charge / I_z
            T_exp = np.nan_to_num(T_exp, posinf=0.0, neginf=0.0)
            Tm_z = (2 * T_int_selected + T_exp) / (eps**2)

        Tm_final = np.minimum(Tm_z, tm_max)
        Tm_final = np.clip(Tm_final, 1e-15, None)

        c_tot_F = cap * 1e-12
        mean_V = I_z * T_int_selected / c_tot_F

        term = np.clip(I_z * T_int_selected * y_bar_D_charge, 0, None)
        std_V = np.sqrt(term) / c_tot_F

        return {
            "energy": energy,
            "beam_curr": beam_curr,
            "z": z,
            "yf": yf,
            "yd": yd,
            "g": g,
            "rate": rate,
            "current_pa": current_pa,
            "l": l,
            "n_min": n_min,
            "cap": cap,
            "vmin": vmin,
            "vmax": vmax,
            "t_int_min": res_int["T_int_min_z"][0],
            "t_int_max": res_int["T_int_max_comp1"][0],
            "t_m_min": Tm_final[0],
            "t_m_max": Tm_final[0],
            "mean_v": mean_V[0],
            "std_v": std_V[0],
            "t_exp": res_int["T_exp_min"][0],
        }

    @staticmethod
    def export_custom_setups_to_csv(file_path: str, setups: list):
        import csv

        headers = [
            "Energy (MeV)",
            "Beam current (nA)",
            "z (cm)",
            "y_bar_F (keV/um)",
            "y_bar_D (keV/um)",
            "g",
            "Rate (p/s)",
            "Current (pA)",
            "Chord (um)",
            "n_min",
            "C (pF)",
            "V_min (V)",
            "V_max (V)",
            "T_int_min (s)",
            "T_int_max (s)",
            "T_m (s)",
            "Mean V (V)",
            "std V (V)",
        ]
        with open(file_path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)
            for data in setups:
                row_data = [
                    f"{data['energy']:.2f}",
                    f"{data['beam_curr']:.2f}",
                    f"{data['z']:.3f}",
                    f"{data['yf']:.3f}",
                    f"{data['yd']:.3f}",
                    f"{data['g']:.1f}",
                    f"{data['rate']:.2e}",
                    f"{data['current_pa']:.2f}",
                    f"{data['l']:.2f}",
                    f"{data['n_min']:.0f}",
                    f"{data['cap']:.1f}",
                    f"{data['vmin']:.3f}",
                    f"{data['vmax']:.3f}",
                    f"{data['t_int_min']:.4e}",
                    f"{data['t_int_max']:.4e}",
                    f"{data['t_m_min']:.4e}",
                    f"{data['mean_v']:.3f}",
                    f"{data['std_v']:.3f}",
                ]
                writer.writerow(row_data)
