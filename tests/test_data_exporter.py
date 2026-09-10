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
    assert settings["measurement_info"]["acquisition_mode"] == "operational"
    assert settings["experimental_setup"]["particle"] == session.particle
    hw = settings["hardware_settings"]
    assert hw["adc"]["bits"] == 12
    assert hw["adc"]["input_range_volts"]["max_volts"] == 5.0
    assert hw["channels"]["ch1"]["gpio_mask"] == [True, False, True]
    assert hw["channels"]["ch1"]["capacitance_pF"] == pytest.approx(3422.0)
    assert hw["hardware_config_sha256"] == loaded_manager.config_sha256
    assert "pico_1" in hw["hardware"]["devices"]


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
