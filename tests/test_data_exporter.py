"""
Round-trip test: v3 measurement archive with embedded provenance.
"""
import json
import os

import pytest

from core.load_file import load_file
from models.session_model import SessionModel
from services.data_exporter import DataExporter

EXAMPLE = os.path.join(
    os.path.dirname(__file__), "..", "examples", "hardware_config_all_pico.json"
)


@pytest.fixture
def loaded_manager():
    from hardware.core.hardware_manager import HardwareManager

    manager = HardwareManager()
    manager.load_config(EXAMPLE)
    return manager


@pytest.fixture
def session():
    model = SessionModel()
    model.gpio_mask_ch1 = [True, False, True]
    model.capacitance_pf_ch1 = 3422.0
    return model


def test_settings_dict_v3(loaded_manager, session):
    settings = DataExporter.build_settings_dict(session, hardware_manager=loaded_manager)
    assert settings["file_format_version"] == "3.0"
    # The app always acquires in debug mode (start/stop stored); the session
    # default reflects that decision.
    assert settings["measurement_info"]["acquisition_mode"] == "debug"
    assert settings["experimental_setup"]["particle"] == session.particle
    hw = settings["hardware_settings"]
    assert hw["adc"]["bits"] == 12
    assert hw["adc"]["input_range_volts"]["max_volts"] == 3.3
    assert hw["channels"]["ch1"]["gpio_mask"] == [True, False, True]
    assert hw["channels"]["ch1"]["capacitance_pF"] == pytest.approx(3422.0)
    assert hw["hardware_config_sha256"] == loaded_manager.config_sha256
    assert "pico_1" in hw["hardware"]["devices"]
    # what the timing controller generates (internal / ramp / markers)
    assert settings["timing_settings"]["timing_mode"] == "internal"


def test_zip_roundtrip_with_provenance(loaded_manager, session, tmp_path):
    csv_path = tmp_path / "run.csv"
    csv_path.write_text("timestamp,ch1,ch2\n0.0,100,100\n")
    zip_path = tmp_path / "measurement.zip"
    setup_path = tmp_path / "setup.json"
    setup_path.write_text(json.dumps({"experimental_setup": {"facility": "CNAO"}}))

    ok = DataExporter.export_to_zip(
        session_model=session,
        temp_csv_path=str(csv_path),
        zip_filepath=str(zip_path),
        hardware_manager=loaded_manager,
        hardware_config_text=loaded_manager.config_text,
        setup_json_text=setup_path.read_text(),
    )
    assert ok

    result = load_file(str(zip_path))
    settings = result["settings"]

    # embedded provenance present and matching
    assert "hardware_config" in result["embedded"]
    embedded = json.loads(result["embedded"]["hardware_config"])
    assert embedded == json.loads(loaded_manager.config_text)
    assert "setup" in result["embedded"]

    # applied gain restored from the snapshot (B4)
    ch1 = settings["hardware_settings"]["channels"]["ch1"]
    assert ch1["gpio_mask"] == [True, False, True]
    assert ch1["capacitance_pF"] == pytest.approx(3422.0)


def test_no_config_archive_still_valid(session, tmp_path):
    """Measurements taken without a loaded config save without provenance."""
    csv_path = tmp_path / "run.csv"
    csv_path.write_text("timestamp,ch1,ch2\n0.0,100,100\n")
    zip_path = tmp_path / "measurement.zip"
    assert DataExporter.export_to_zip(session, str(csv_path), str(zip_path))
    result = load_file(str(zip_path))
    assert result["settings"]["file_format_version"] == "3.0"
    assert result["embedded"] == {}


def test_acquisition_mode_debug(session, tmp_path):
    session.debug_mode = True
    settings = DataExporter.build_settings_dict(session, hardware_manager=None)
    assert settings["measurement_info"]["acquisition_mode"] == "debug"


FFT_PARAMS = {
    "excluded_bands": [[100.0, 500.0]],
    "threshold_ch1": 0.0464,
    "threshold_ch2": 0.1049,
    "method": "welch",
    "window": "hann",
    "nperseg": 8192,
    "diagnostics": {
        "ch1": {
            "floor_level": 0.008001,
            "threshold": 0.0464,
            # only the peaks the user selected are archived
            "peaks": [{"f_lo": 100.24, "f_center": 100.3, "f_hi": 100.36,
                       "variance": 0.079}],
            "peaks_detected": 47,
        },
        "ch2": {"floor_level": 0.018002, "threshold": 0.1049, "peaks": [],
                "peaks_detected": 0},
    },
}


def test_fft_block_absent_without_analysis(session):
    """No FFT analysis run -> the archive has no fft key at all."""
    settings = DataExporter.build_settings_dict(
        session,
        analysis_meta={
            "analysis_timestamp": "2026-09-11T10:00:00+02:00",
            "n_bootstraps": 1,
        },
    )
    assert "fft" not in settings["analysis"]


def test_fft_block_persisted(session, tmp_path):
    """Applied FFT analysis: params + diagnostics land in analysis.fft."""
    results = {
        "var_fft": {
            "N": [1000, 0, 1000, 0],
            "Mean": [50.0, 0, 30.0, 0],
            "Variance": [4.1, 0, 9.2, 0],
            "Covariance": [0.1, 0, 0.2, 0],
            "y_bar_d": [0.08, 0, 0.3, 0],
        }
    }
    csv_path = tmp_path / "run.csv"
    csv_path.write_text("timestamp,ch1,ch2\n0.0,100,100\n")
    zip_path = tmp_path / "measurement.zip"
    ok = DataExporter.export_to_zip(
        session_model=session,
        temp_csv_path=str(csv_path),
        zip_filepath=str(zip_path),
        results_dict=results,
        analysis_meta={
            "analysis_timestamp": "2026-09-11T10:00:00+02:00",
            "n_bootstraps": 1,
            "fft": dict(FFT_PARAMS),
        },
    )
    assert ok

    loaded = load_file(str(zip_path))
    analysis = loaded["settings"]["analysis"]
    assert analysis["fft"] == FFT_PARAMS
    # results travel in the same block (the fifth estimator)
    assert analysis["results"]["var_fft"]["Variance"][0] == 4.1
    # JSON-safety: no tuples leaked through
    json.dumps(analysis)


def test_update_analysis_swaps_fft_block(session, tmp_path):
    """Re-analysis replaces the fft block; a cleared analysis removes it."""
    csv_path = tmp_path / "run.csv"
    csv_path.write_text("timestamp,ch1,ch2\n0.0,100,100\n")
    zip_path = tmp_path / "measurement.zip"
    assert DataExporter.export_to_zip(
        session, str(csv_path), str(zip_path),
        analysis_meta={"analysis_timestamp": "t1", "n_bootstraps": 1,
                       "fft": dict(FFT_PARAMS)},
    )
    meta = {"analysis_timestamp": "t2", "n_bootstraps": 1}
    assert DataExporter.update_analysis_in_zip(str(zip_path), {}, meta)
    analysis = load_file(str(zip_path))["settings"]["analysis"]
    assert "fft" not in analysis
