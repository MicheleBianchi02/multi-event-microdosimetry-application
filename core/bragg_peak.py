import logging
import os

import numpy as np

logger = logging.getLogger(__name__)


def extract_num(name):
    try:
        return float("".join(c for c in name if c.isdigit() or c == "."))
    except ValueError:
        return 0.0


# =============================================================================
# RANGES.TXT TEMPLATE
# If ranges.txt is missing, you can create it in the results_dir (data/pristine).
# The format should be:
# # Enter the range for each beam energy & step after the colon.
# # E.g., 70Mev/10um: 4.2
# 70Mev/10um: 4.2
# 70Mev/20um: 4.2
# =============================================================================
def load_fluka_data(results_dir="data/pristine"):
    range_file = os.path.join(results_dir, "ranges.txt")

    if not os.path.exists(results_dir):
        raise FileNotFoundError(f"Directory '{results_dir}' not found.")

    subdirs = []
    for energy_dir in os.listdir(results_dir):
        energy_path = os.path.join(results_dir, energy_dir)
        if os.path.isdir(energy_path):
            has_subdirs = False
            for step_dir in os.listdir(energy_path):
                step_path = os.path.join(energy_path, step_dir)
                if os.path.isdir(step_path):
                    subdirs.append((energy_dir, step_dir))
                    has_subdirs = True

            if not has_subdirs and os.path.exists(
                os.path.join(energy_path, "energy.dat")
            ):
                subdirs.append((energy_dir, ""))

    subdirs.sort(key=lambda x: (extract_num(x[0]), extract_num(x[1])))

    if not subdirs:
        raise ValueError(
            f"No valid energy/step subdirectories found in '{results_dir}'."
        )

    ranges = {}
    if os.path.exists(range_file):
        with open(range_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) >= 2:
                    folder = parts[0].strip()
                    val_str = parts[1].strip()
                    if val_str:
                        try:
                            ranges[folder] = float(val_str)
                        except ValueError:
                            pass

    for edir, sdir in subdirs:
        key = f"{edir}/{sdir}" if sdir else edir
        if key not in ranges:
            if edir in ranges:
                ranges[key] = ranges[edir]
            else:
                ranges[key] = None
                logger.warning(
                    "Range for '%s' not found in %s. Will default to maximum simulated depth.",
                    key, range_file
                )

    plot_data = {}

    for edir, sdir in subdirs:
        key = f"{edir}/{sdir}" if sdir else edir
        folder_path = (
            os.path.join(results_dir, edir, sdir)
            if sdir
            else os.path.join(results_dir, edir)
        )
        e_file = os.path.join(folder_path, "energy.dat")
        f_file = os.path.join(folder_path, "fluence.dat")

        if not os.path.exists(e_file) or not os.path.exists(f_file):
            continue

        try:
            e_data = np.loadtxt(e_file, comments="#")
            f_data = np.loadtxt(f_file, comments="#")
        except Exception:
            continue

        if len(e_data) == 0 or len(f_data) == 0:
            continue

        z_e = (e_data[:, 0] + e_data[:, 1]) / 2.0
        z_f = (f_data[:, 0] + f_data[:, 1]) / 2.0

        val_e = e_data[:, 2]
        val_f_raw = f_data[:, 2]

        val_f = np.interp(z_e, z_f, val_f_raw)

        with np.errstate(divide="ignore", invalid="ignore"):
            let = np.nan_to_num((val_e / val_f) * 100, posinf=0.0, neginf=0.0)

        beam_range = ranges.get(key)
        if beam_range is not None:
            mask = z_e <= beam_range
            z_e = z_e[mask]
            val_e = val_e[mask]
            let = let[mask]
            val_f = val_f[mask]
        else:
            beam_range = z_e[-1] if len(z_e) > 0 else 1.0

        if len(z_e) > 0:
            plot_data[key] = {
                "z": z_e,
                "e": val_e,
                "f": val_f,
                "let": let,
                "range": beam_range,
                "beam_energy": extract_num(edir),
                "step": sdir,
                "step_num": extract_num(sdir),
                "energy_dir": edir,
            }

    if not plot_data:
        raise ValueError("No valid data to return.")

    return plot_data


def load_sobp_data(results_dir="data/SOBP"):
    depth_dose_file = os.path.join(results_dir, "depth_dose.csv")
    data_file = os.path.join(results_dir, "data.csv")

    if not os.path.exists(depth_dose_file) or not os.path.exists(data_file):
        raise FileNotFoundError(f"SOBP data files not found in '{results_dir}'.")

    # Load dense depth dose curve
    try:
        # skiprows=1 to ignore headers
        dd_data = np.loadtxt(depth_dose_file, delimiter=",", skiprows=1)
        z_dense = dd_data[:, 0]
        dose_dense = dd_data[:, 1]
    except Exception as e:
        raise ValueError(f"Error reading depth_dose.csv: {e}")

    # Load sparse measured data
    try:
        # Depth[cm],yF[keV/um],yD[keV/um], r/I_beam
        meas_data = np.loadtxt(data_file, delimiter=",", skiprows=1)
        z_sparse = meas_data[:, 0]
        yf_sparse = meas_data[:, 1]  # y_F = LET_t in keV/um
        yd_sparse = meas_data[:, 2]  # y_bar_D in keV/um
        fluence_sparse = meas_data[:, 3]  # r/I_beam
    except Exception as e:
        raise ValueError(f"Error reading data.csv: {e}")

    # Sort sparse arrays strictly by depth to allow safe interpolation
    sort_idx = np.argsort(z_sparse)
    z_sparse = z_sparse[sort_idx]
    yf_sparse = yf_sparse[sort_idx]
    yd_sparse = yd_sparse[sort_idx]
    fluence_sparse = fluence_sparse[sort_idx]

    # Sort dense arrays strictly by depth
    sort_idx_dense = np.argsort(z_dense)
    z_dense = z_dense[sort_idx_dense]
    dose_dense = dose_dense[sort_idx_dense]

    # Interpolate sparse data onto dense grid
    # Left and right bounds use the edge values to prevent extrapolation crashes
    yf_interp_kev = np.interp(
        z_dense, z_sparse, yf_sparse, left=yf_sparse[0], right=yf_sparse[-1]
    )
    yd_interp_kev = np.interp(
        z_dense, z_sparse, yd_sparse, left=yd_sparse[0], right=yd_sparse[-1]
    )
    fluence_interp = np.interp(
        z_dense,
        z_sparse,
        fluence_sparse,
        left=fluence_sparse[0],
        right=fluence_sparse[-1],
    )

    # Convert to standard units expected by API (eV/um)
    let_array_eV_um = yf_interp_kev * 1000.0
    y_bar_D_eV_um = yd_interp_kev * 1000.0

    plot_data = {
        "SOBP": {
            "z": z_dense,
            "e": dose_dense,  # Depth dose acts as energy for plotting/logic
            "f": fluence_interp,
            "let": yf_interp_kev,  # Store in keV/um for UI display
            "let_eV_um": let_array_eV_um,
            "y_bar_D_eV_um": y_bar_D_eV_um,
            "range": z_dense[-1],
            "beam_energy": "SOBP",
            "step": "N/A",
        }
    }

    return plot_data


def calc_g_parameter(let_0_eV_um, l, i_leakage_pa, rate, E_0, E_z_max):
    W = 30.0
    e_const = 1.6e-19
    i_leakage_a = i_leakage_pa * 1e-12

    q_0 = let_0_eV_um * l / W * e_const

    if E_z_max == 0 or rate == 0 or q_0 == 0:
        calculated_g = 0.0
    else:
        calculated_g = 100.0 * i_leakage_a / rate / q_0 * (E_0 / E_z_max)

    return calculated_g, q_0


def calc_integration_times(
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
    W = 30.0
    e_const = 1.6e-19

    if g_use == 0 or n_min == 0 or E_0 == 0 or rate == 0 or q_0 == 0:
        empty = np.zeros_like(z_array)
        return {
            "I_z": empty,
            "T_int_min_z": empty,
            "T_int_min_comp1": empty,
            "T_int_min_comp2": empty,
            "T_int_max_min_z": empty,
            "T_int_max_max_z": empty,
            "T_int_max_comp1": empty,
            "T_int_max_comp2_min": empty,
            "T_int_max_comp2_max": empty,
            "T_exp_min": empty,
            "T_exp_max": empty,
        }

    I_0 = rate * q_0 * g_use

    with np.errstate(divide="ignore", invalid="ignore"):
        I_z = I_0 * (e_array / E_0)
        I_z = np.nan_to_num(I_z, posinf=0.0, neginf=0.0)

    q1 = Q_min_C / n_min / g_use
    q_z = let_array_eV_um * l / W * e_const

    with np.errstate(divide="ignore", invalid="ignore"):
        T_int_min_comp1 = (n_min / I_z) * g_use * q_z
        T_int_min_comp1 = np.nan_to_num(T_int_min_comp1, posinf=0.0, neginf=0.0)

        T_int_min_comp2 = (n_min / I_z) * g_use * q1
        T_int_min_comp2 = np.nan_to_num(T_int_min_comp2, posinf=0.0, neginf=0.0)

        T_int_min_z = np.maximum(T_int_min_comp1, T_int_min_comp2)

    y_bar_D_charge_min = y_bar_D_min_eV_um / W * e_const * l * g_use
    y_bar_D_charge_max = y_bar_D_max_eV_um / W * e_const * l * g_use

    with np.errstate(divide="ignore", invalid="ignore"):
        T_int_max_comp1 = Q_max_C / I_z
        T_int_max_comp1 = np.nan_to_num(T_int_max_comp1, posinf=0.0, neginf=0.0)

        T_int_max_comp2_min = (y_bar_D_charge_min / 20.0) / I_z
        T_int_max_comp2_min = np.nan_to_num(T_int_max_comp2_min, posinf=0.0, neginf=0.0)

        T_int_max_comp2_max = (y_bar_D_charge_max / 20.0) / I_z
        T_int_max_comp2_max = np.nan_to_num(T_int_max_comp2_max, posinf=0.0, neginf=0.0)

        T_int_max_min_z = np.minimum(T_int_max_comp1, T_int_max_comp2_min)
        T_int_max_max_z = np.minimum(T_int_max_comp1, T_int_max_comp2_max)

        T_exp_min = y_bar_D_charge_min / I_z
        T_exp_min = np.nan_to_num(T_exp_min, posinf=0.0, neginf=0.0)

        T_exp_max = y_bar_D_charge_max / I_z
        T_exp_max = np.nan_to_num(T_exp_max, posinf=0.0, neginf=0.0)

    return {
        "I_z": I_z,
        "T_int_min_z": T_int_min_z,
        "T_int_min_comp1": T_int_min_comp1,
        "T_int_min_comp2": T_int_min_comp2,
        "T_int_max_min_z": T_int_max_min_z,
        "T_int_max_max_z": T_int_max_max_z,
        "T_int_max_comp1": T_int_max_comp1,
        "T_int_max_comp2_min": T_int_max_comp2_min,
        "T_int_max_comp2_max": T_int_max_comp2_max,
        "T_exp_min": T_exp_min,
        "T_exp_max": T_exp_max,
    }


def export_plot_data(folder_path, data_dict):
    import csv

    for plot_name, columns in data_dict.items():
        file_path = os.path.join(folder_path, f"{plot_name}.csv")
        with open(file_path, "w", newline="") as f:
            writer = csv.writer(f)
            headers = [col[0] for col in columns]
            writer.writerow(headers)

            if not columns:
                continue

            num_rows = len(columns[0][1])
            for i in range(num_rows):
                row = [col[1][i] for col in columns]
                writer.writerow(row)
