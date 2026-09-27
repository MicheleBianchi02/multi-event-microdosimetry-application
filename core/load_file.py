import re
import zipfile
import json
import io

import numpy as np
import pandas as pd

from core.constants import DEFAULT_ADC_BITS, OVERFLOW_VALUE, overflow_sentinel

def load_file(file_path: str) -> dict:
    """
    Loads measurement data from either a ZIP archive (v2 format) or a legacy CSV file.
    """
    if file_path.endswith('.zip'):
        return _load_zip(file_path)
    else:
        return _load_csv(file_path)

def _adc_bits_from_settings(settings: dict | None) -> int:
    """ADC resolution recorded in the archive: v3 stores it under
    hardware_settings.adc.bits, older archives in the top-level
    adc_num_bits; None (legacy CSV, no settings) falls back to 12 bit."""
    if not settings:
        return DEFAULT_ADC_BITS
    bits = settings.get("hardware_settings", {}).get("adc", {}).get("bits")
    if bits is None:
        bits = settings.get("adc_num_bits")
    try:
        return int(bits) if bits else DEFAULT_ADC_BITS
    except (TypeError, ValueError):
        return DEFAULT_ADC_BITS

def _load_zip(file_path: str) -> dict:
    with zipfile.ZipFile(file_path, 'r') as zf:
        settings_path = None
        data_path = None
        hardware_config_path = None
        setup_path = None
        for name in zf.namelist():
            # Allow matching settings.json in a subfolder (like when compressed by GNOME)
            if name.endswith("settings.json"):
                settings_path = name
            elif name.endswith("data.csv"):
                data_path = name
            elif name.endswith("hardware_config.json"):
                hardware_config_path = name
            elif name.endswith("setup.json"):
                setup_path = name

        if not settings_path:
            raise KeyError("There is no settings.json in the archive")
        if not data_path:
            raise KeyError("There is no data.csv in the archive")

        with zf.open(settings_path) as f:
            settings = json.load(f)

        # v3 archives embed the hardware config (and campaign setup) used for
        # the run, so they can be interpreted without any external file
        embedded = {}
        if hardware_config_path is not None:
            with zf.open(hardware_config_path) as f:
                embedded["hardware_config"] = f.read().decode("utf-8")
        if setup_path is not None:
            with zf.open(setup_path) as f:
                embedded["setup"] = f.read().decode("utf-8")

        with zf.open(data_path) as f:
            first_line = f.readline().decode('utf-8').strip()
            adc_bits = _adc_bits_from_settings(settings)
            # v3 data.csv stores overflow as NaN; the sentinel mapping below
            # is a safety net using the archive's own ADC depth.
            result = _parse_csv_content(f, first_line, overflow_sentinel(adc_bits))
            result["settings"] = settings
            result["embedded"] = embedded
            result["adc_bits"] = adc_bits
            return result

def _load_csv(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
        # Plain CSVs are legacy format 1: 12-bit data with the 32768 sentinel.
        result = _parse_csv_content(f, first_line, OVERFLOW_VALUE)
        result["settings"] = None
        result["adc_bits"] = DEFAULT_ADC_BITS
        return result

def _parse_csv_content(file_obj, header_line: str, sentinel: int) -> dict:
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
        data[data == sentinel] = np.nan
        start_value = None
        stop_value = None
    else:
        debug_mode = True
        start_value = raw_data[:, [1, 3]].copy()
        start_value[start_value == sentinel] = np.nan
        stop_value = raw_data[:, [2, 4]].copy()
        # same NaN mapping as the live path (B15): the returned stop array
        # holds NaN where the sample overflowed, not the raw sentinel
        stop_value[stop_value == sentinel] = np.nan

        data = stop_value - start_value
        data[data < 0] = np.nan

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
