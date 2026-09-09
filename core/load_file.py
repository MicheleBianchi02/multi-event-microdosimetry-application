import re
import zipfile
import json
import io

import numpy as np
import pandas as pd

from core.constants import OVERFLOW_VALUE

def load_file(file_path: str) -> dict:
    """
    Loads measurement data from either a ZIP archive (v2 format) or a legacy CSV file.
    """
    if file_path.endswith('.zip'):
        return _load_zip(file_path)
    else:
        return _load_csv(file_path)

def _load_zip(file_path: str) -> dict:
    with zipfile.ZipFile(file_path, 'r') as zf:
        settings_path = None
        data_path = None
        for name in zf.namelist():
            # Allow matching settings.json in a subfolder (like when compressed by GNOME)
            if name.endswith("settings.json"):
                settings_path = name
            elif name.endswith("data.csv"):
                data_path = name
                
        if not settings_path:
            raise KeyError("There is no settings.json in the archive")
        if not data_path:
            raise KeyError("There is no data.csv in the archive")
            
        with zf.open(settings_path) as f:
            settings = json.load(f)
            
        with zf.open(data_path) as f:
            first_line = f.readline().decode('utf-8').strip()
            result = _parse_csv_content(f, first_line)
            result["settings"] = settings
            return result

def _load_csv(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
        result = _parse_csv_content(f, first_line)
        result["settings"] = None
        return result

def _parse_csv_content(file_obj, header_line: str) -> dict:
    if not header_line:
        raise RuntimeError("Empty file")

    m_int = re.search(r"integration time \[s\]:\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", header_line)
    m_rst = re.search(r"reset time \[s\]:\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", header_line)
    m_frq = re.search(r"sample frequency \[Hz\]:\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", header_line)

    if not (m_int and m_rst and m_frq):
        if header_line.startswith("timestamp"):
            int_time = 0.0
            reset_time = 0.0
            sample_freq = 0.0
        else:
            raise RuntimeError("Faulty header")
    else:
        int_time = float(m_int.group(1))
        reset_time = float(m_rst.group(1))
        sample_freq = float(m_frq.group(1))

    try:
        # We don't use skiprows=1 because file_obj has already advanced past the header line
        df = pd.read_csv(file_obj, header=None, na_values=["na", "NA", "nan", "NaN"])
    except Exception as exc:
        raise RuntimeError("Invalid measurement data") from exc

    if df.empty:
        raise RuntimeError("No measurement rows found")

    num_cols = df.shape[1]
    if num_cols not in [3, 5]:
        raise RuntimeError("Column number")

    raw_data = df.to_numpy(dtype=float)
    elapsed_time = float(raw_data[-1, 0])

    if num_cols == 3:
        debug_mode = False
        data = raw_data[:, 1:3].copy()
        data[data == OVERFLOW_VALUE] = np.nan
        start_value = None
        stop_value = None
    else:
        debug_mode = True
        start_value = raw_data[:, [1, 3]].copy()
        start_value[start_value == OVERFLOW_VALUE] = np.nan
        stop_value = raw_data[:, [2, 4]].copy()

        stop_values_valid = stop_value.copy()
        stop_values_valid[stop_values_valid == OVERFLOW_VALUE] = np.nan

        data = stop_values_valid - start_value
        data[data < 0] = np.nan
        data[data == OVERFLOW_VALUE] = np.nan

    return {
        "int_time": int_time,
        "reset_time": reset_time,
        "sample_freq": sample_freq,
        "debug_mode": debug_mode,
        "elapsed_time": elapsed_time,
        "data": data,
        "start_value": start_value,
        "stop_value": stop_value,
    }
