"""
Tests for the workspace setup file handling: the setup file may carry any
name — the session remembers it, and hardware-link operations must touch
that file, never create a canonical setup.json behind the user's back.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.session_model import SessionModel  # noqa: E402
from services.shift_service import ShiftService  # noqa: E402


@pytest.fixture
def session():
    return SessionModel()


def test_load_and_save_remember_the_setup_file_name(session, tmp_path):
    custom = tmp_path / "campaign_setup.json"
    assert ShiftService.save_setup(session, str(custom))
    assert session.setup_file_name == "campaign_setup.json"
    assert session.active_workspace_dir == str(tmp_path)

    other = SessionModel()
    assert ShiftService.load_setup(other, str(custom))
    assert other.setup_file_name == "campaign_setup.json"
    assert other.facility == session.facility


def test_link_writes_into_the_loaded_setup_file(session, tmp_path):
    """The user's scenario: a workspace whose setup file is not named
    setup.json — linking a hardware config must not create one."""
    custom = tmp_path / "campaign_setup.json"
    assert ShiftService.save_setup(session, str(custom))

    source = tmp_path / "my_bench.json"
    source.write_text(json.dumps(
        {"schema_version": "1.0", "devices": {}, "circuits": {}, "channels": {},
         "roles": {}, "connections": []}
    ))

    copied = ShiftService.link_hardware_config(
        str(tmp_path), str(source), setup_file=session.setup_file_name
    )
    assert os.path.basename(copied) == "my_bench.json"
    assert os.path.exists(copied)

    # the link landed in the custom file; NO setup.json was created
    assert not (tmp_path / "setup.json").exists()
    link = json.load(open(custom))["hardware"]
    assert link["config_file"] == "my_bench.json"
    from hardware.config import file_sha256

    assert link["config_sha256"] == file_sha256(copied)

    assert ShiftService.read_hardware_link(
        str(tmp_path), setup_file=session.setup_file_name
    ) == link

    ShiftService.clear_hardware_link(str(tmp_path), setup_file=session.setup_file_name)
    assert "hardware" not in json.load(open(custom))


def test_default_name_is_setup_json(session, tmp_path):
    custom = tmp_path / "setup.json"
    assert ShiftService.save_setup(session, str(custom))
    source = tmp_path / "hw.json"
    source.write_text("{}")
    ShiftService.link_hardware_config(str(tmp_path), str(source))
    link = ShiftService.read_hardware_link(str(tmp_path))
    assert link["config_file"] == "hw.json"
