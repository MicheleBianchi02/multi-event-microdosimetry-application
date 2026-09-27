"""
Dynamic ADC context (BUG.md B6): the ADC resolution — and everything derived
from it (histogram bins, overflow sentinel, counts→charge scaling) — comes
from the loaded hardware configuration (live) or from the archive's
hardware_settings.adc.bits (offline), not from a hardcoded 12-bit constant.

The overflow/missing-channel sentinel is 2^bits (one past full scale), and
32768 remains the sentinel only of the legacy CSV format 1.
"""
import json
import os
import sys
import zipfile

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import DEFAULT_ADC_BITS, OVERFLOW_VALUE, overflow_sentinel  # noqa: E402
from core.data_utils import convert_data, convert_data_debug  # noqa: E402
from core.load_file import load_file  # noqa: E402
from models.data_model import DataModel  # noqa: E402


# --------------------------------------------------------------- sentinel

def test_sentinel_is_one_past_full_scale():
    assert overflow_sentinel(12) == 4096
    assert overflow_sentinel(16) == 65536
    # the legacy CSV sentinel stays distinct and larger
    assert OVERFLOW_VALUE == 32768


def test_convert_data_maps_dynamic_sentinel():
    arr = convert_data([(65535, 100), (65536, 200)], sentinel=overflow_sentinel(16))
    assert arr[0, 0] == 65535  # full-scale reading is legitimate…
    assert np.isnan(arr[1, 0])  # …one past it is overflow


def test_convert_data_debug_maps_dynamic_sentinel():
    raw = [
        ((65534, 65535), (10, 20)),   # ok window
        ((65536, 5000), (10, 20)),    # ch1 start overflowed
    ]
    start, stop, diff = convert_data_debug(raw, sentinel=overflow_sentinel(16))
    assert start[0, 0] == 65534
    assert np.isnan(start[1, 0])
    assert np.isnan(diff[1, 0])  # overflow propagates to the difference
    assert diff[0, 0] == 1.0


def test_converters_default_to_the_12bit_fallback():
    arr = convert_data([(4096, 1)])
    assert np.isnan(arr[0, 0])


# ------------------------------------------------------------- DataModel

def test_data_model_default_is_12bit():
    dm = DataModel()
    assert dm.adc_bits == 12
    assert dm.num_bins == 4096
    assert dm.overflow_sentinel == 4096


def test_configure_adc_resizes_histograms():
    dm = DataModel()
    dm.bar_data_ch1[100] += 5  # accumulate something at 12-bit scale
    dm.configure_adc(16)
    assert dm.adc_bits == 16
    assert dm.num_bins == 65536
    assert dm.overflow_sentinel == 65536
    assert len(dm.bar_data_ch1) == 65536
    assert dm.bar_data_ch1.sum() == 0  # bins reset, statistics arrays kept


def test_configure_adc_rejects_nonsense():
    dm = DataModel()
    with pytest.raises(ValueError):
        dm.configure_adc(0)
    with pytest.raises(ValueError):
        dm.configure_adc(32)  # ramp-monitoring data never enters the model


def test_ingest_16bit_batch_counts_full_scale():
    dm = DataModel()
    dm.configure_adc(16)
    sentinel = dm.overflow_sentinel
    raw = [((65000, 65535), (sentinel, sentinel)), ((10, 20), (sentinel, sentinel))]
    start, stop, diff = dm.ingest_raw_batch(
        raw_batch=raw, debug_mode=True, rois=None,
        sample_frequency=10.0, time_rois=[],
    )
    assert diff[0, 0] == 535.0  # 65535 - 65000, no clipping at 4096
    assert np.isnan(diff[:, 1]).all()  # missing channel -> NaN
    assert dm.bar_data_ch1[535] == 1  # counted in the 16-bit histogram
    assert dm.bar_data_ch1[10] == 1   # second window's diff
    assert dm.bar_data_ch1.sum() == 2


def test_clear_keeps_the_configured_bits():
    dm = DataModel()
    dm.configure_adc(14)
    dm.clear()
    assert dm.num_bins == 2**14
    assert len(dm.bar_data_ch1) == 2**14


# ------------------------------------------------------------- load path

def _make_zip(tmp_path, bits, rows, header="timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop"):
    csv = header + "\n" + "\n".join(rows) + "\n"
    settings = {
        "file_format_version": "3.0",
        "measurement_info": {"acquisition_mode": "debug"},
        "hardware_settings": {"adc": {"bits": bits}},
        "timing_settings": {
            "integration_time_s": 0.001, "reset_time_s": 0.0005,
            "period_s": 0.0015, "sample_frequency_hz": 1 / 0.0015,
        },
    }
    path = tmp_path / "measurement.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data.csv", csv)
        zf.writestr("settings.json", json.dumps(settings))
    return path


def test_load_archive_reports_its_adc_bits(tmp_path):
    zf = _make_zip(tmp_path, 16, ["0.0,60000,61000,0,0"])
    result = load_file(str(zf))
    assert result["adc_bits"] == 16
    assert result["data"][0, 0] == 1000.0  # 16-bit diff survives the load


def test_load_archive_maps_sentinel_of_its_own_depth(tmp_path):
    # a 16-bit archive storing the sentinel value (defensive: v3 stores NaN,
    # but the loader maps 2^bits if present)
    zf = _make_zip(tmp_path, 16, ["0.0,65536,65536,0,0"])
    result = load_file(str(zf))
    assert np.isnan(result["start_value"][0, 0])


def test_load_pre30_archive_with_adc_num_bits(tmp_path):
    csv = "timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop\n0.0,60000,61000,0,0\n"
    settings = {"adc_num_bits": 16}  # old top-level field
    path = tmp_path / "old.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("data.csv", csv)
        zf.writestr("settings.json", json.dumps(settings))
    result = load_file(str(path))
    assert result["adc_bits"] == 16


def test_load_legacy_csv_keeps_32768_sentinel(tmp_path):
    csv = "timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop\n0.0,1000,32768,0,0\n"
    path = tmp_path / "legacy.csv"
    path.write_text(csv)
    result = load_file(str(path))
    assert result["adc_bits"] == DEFAULT_ADC_BITS
    assert np.isnan(result["stop_value"][0, 0])  # legacy 32768 still maps to NaN


# ------------------------------------------------------- session signal

def test_session_adc_bits_signal():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])

    from models.session_model import SessionModel
    session = SessionModel()
    assert session.adc_bits == 12

    seen = []
    session.adc_bits_changed.connect(lambda bits: seen.append(bits))
    session.adc_bits = 16
    session.adc_bits = 16  # no change, no signal
    assert seen == [16]
