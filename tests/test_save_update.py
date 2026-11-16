"""
Regression tests for the save/update flow of a finished measurement
(review finding N1, 2026-09-28): after the first successful save
``session.file_path`` IS the ZIP, and the save handler used it as the
raw-data source on every subsequent save (the UPDATE FILE button) —
``export_to_zip`` truncated the archive while re-reading it as ``data.csv``,
silently destroying the measurement's raw data (the workspace temp CSV had
already been deleted by the first save).

The save path must rebuild the CSV from memory once the online temp is gone,
and a re-save must keep the published ramp section linked into settings.json
(the binaries did not change, only the analysis is refreshed).
"""
import json
import os
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

_app = QApplication.instance() or QApplication([])

# every popup is stubbed before anything can block the offscreen run
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.critical = staticmethod(lambda *a, **k: None)


@pytest.fixture
def controller(tmp_path):
    from app import AppController

    ctrl = AppController(simulate=True, baud_rate=115200, single_channel=False)
    ctrl.session_model.active_workspace_dir = str(tmp_path)
    yield ctrl
    mc = ctrl.measurement_controller
    if mc.is_running:
        mc.handle_stop_measurement()


def _run_and_stop(controller, rows):
    """Run a short simulated measurement in the workspace and stop it, with
    `rows` debug tuples guaranteed to reach the online temp CSV."""
    mc = controller.measurement_controller
    mc.arming_istant = 0.0  # skip the >= 3 s arm gap
    mc.start_device()
    assert mc.is_running
    mc.data_model.append_batch(rows)
    mc.handle_stop_measurement()
    return mc


def _read_member(zip_path, member):
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(member).decode()


def test_update_after_save_keeps_data_csv(controller, tmp_path):
    mc = _run_and_stop(
        controller,
        [((100, 200), (4096, 4096)), ((150, 260), (300, 380))],
    )
    online_csv = os.path.join(str(tmp_path), ".active_run.csv.tmp")
    assert os.path.exists(online_csv)

    zip_path = str(tmp_path / "measurement.zip")
    mc.handle_save_data(zip_path)                            # first save
    assert controller.session_model.file_path == zip_path    # the session now points at the ZIP
    assert not os.path.exists(online_csv)                    # its temp was consumed
    first = _read_member(zip_path, "data.csv")

    mc.handle_save_data(zip_path)                            # UPDATE FILE
    second = _read_member(zip_path, "data.csv")

    # the update must rewrite the SAME rows from memory, not re-read the ZIP
    # (the old code fed the ZIP back in as the CSV source: data.csv came back
    # as binary garbage and the raw data was lost with the deleted temp)
    lines = second.splitlines()
    assert lines[0] == "timestamp,ch1_start,ch1_stop,ch2_start,ch2_stop"
    assert len(lines) - 1 == mc.data_model.ptr
    assert second == first


def test_resave_keeps_the_published_ramp_section(controller, tmp_path):
    session = controller.session_model
    session.timing_mode = "ramp"
    session.int_time = 0.1
    session.reset_time = 0.1
    session.period_s = 0.2
    session.ramp_dt_s = 2e-5            # applied dt (mirror value)
    session.samples_per_window = 5      # int/dt + 1

    mc = _run_and_stop(controller, [])
    assert mc._ramp_summary is not None

    zip_path = str(tmp_path / "measurement.zip")
    mc.handle_save_data(zip_path)
    first = json.loads(_read_member(zip_path, "settings.json"))
    assert "ramp" in first
    assert os.path.isdir(os.path.join(str(tmp_path), "ramp"))

    mc.handle_save_data(zip_path)       # UPDATE FILE
    second = json.loads(_read_member(zip_path, "settings.json"))
    assert second.get("ramp") == first["ramp"]
