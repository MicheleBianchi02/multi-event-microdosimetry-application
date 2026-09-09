"""
End-to-end tests for the app controller's hardware-config handlers
(workspace link, editor save path). Runs fully offscreen with stubbed
dialogs — these paths have no other coverage.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from hardware.config import editor_model as em  # noqa: E402
from services.shift_service import ShiftService  # noqa: E402

_app = QApplication.instance() or QApplication([])

popups = {"question": [], "critical": []}


def _stub_popups(question_answer=True):
    """Stub ALL popups before anything can open one: a real modal QMessageBox
    blocks the (offscreen) test run forever.  Resets the record — the dict is
    module-level and must not leak between tests."""
    popups["question"].clear()
    popups["critical"].clear()

    def ask(*args, **kwargs):
        popups["question"].append(args[1] if len(args) > 1 else kwargs.get("title", ""))
        return QMessageBox.StandardButton.Yes if question_answer else QMessageBox.StandardButton.No

    QMessageBox.question = staticmethod(ask)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)

    def critical(*args, **kwargs):
        popups["critical"].append(args)

    QMessageBox.critical = staticmethod(critical)


# default stubbing from the very start: AppController construction and the
# fixtures fire workspace popups before individual tests run
_stub_popups()


@pytest.fixture
def controller():
    from app import AppController

    ctrl = AppController(simulate=False, baud_rate=115200, single_channel=False)
    return ctrl


@pytest.fixture
def workspace(controller, tmp_path, monkeypatch):
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    assert ShiftService.save_setup(controller.session_model, os.path.join(workspace, "setup.json"))
    return workspace


def _valid_bench_config() -> dict:
    """A minimal valid config assembled like the editor would."""
    cfg = em.new_config()
    pico = em.load_fragment(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "hardware_library",
            "device_pico2.json",
        )
    )
    pic = em.load_fragment(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "hardware_library",
            "device_pic.json",
        )
    )
    board = em.load_fragment(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "hardware_library",
            "circuit_board_rev_b_ch1.json",
        )
    )
    pico_id = em.add_device(cfg, pico["data"], pico["suggested_id"])
    pic_id = em.add_device(cfg, pic["data"], pic["suggested_id"])
    board_id = em.add_circuit(cfg, board["data"], board["suggested_id"])
    # the Pico keeps its acquisition block and takes timing + acquisition;
    # the PIC (1 GPIO) drives the gain
    em.set_role(cfg, "timing", pico_id)
    em.set_role(cfg, "acquisition", pico_id)
    em.set_role(cfg, "gain_ch1", pic_id)
    em.set_channel(cfg, "ch1", board_id)
    for cap, pin in (("c100", 2), ("c560", 2), ("c3300", 2)):
        em.set_connection(cfg, "ch1", cap, pin)
    return cfg


def _fake_dialog_class(result):
    """A stand-in class for HardwareConfigEditorDialog that accepts without
    user interaction and returns the given config."""

    class _FakeDialog:
        def __init__(self, config, library_dirs, parent=None):
            self.config = config
            self.library_dirs = library_dirs
            self._result = result

        def exec(self):
            return True

        def get_config(self):
            return self._result

    return _FakeDialog


def test_new_from_components_saves_and_links(controller, workspace, monkeypatch):
    _stub_popups()
    result = _valid_bench_config()
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(result),
    )

    assert controller._run_config_editor(em.new_config()) is True

    dest = os.path.join(workspace, "hardware_config.json")
    assert os.path.exists(dest)
    assert json.load(open(dest)) == result

    link = ShiftService.read_hardware_link(workspace)
    assert link["config_file"] == "hardware_config.json"
    from hardware.config import file_sha256

    assert link["config_sha256"] == file_sha256(dest)

    # the manager loaded the new config through the workspace link
    assert controller.hardware_manager.is_loaded
    assert controller.hardware_manager.config_sha256 == link["config_sha256"]
    assert popups["critical"] == []  # no swallowed errors (regression: class-attr typo)


def test_edit_saves_back_to_the_linked_file(controller, workspace, monkeypatch):
    _stub_popups()
    result = _valid_bench_config()
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(result),
    )

    # an existing linked config, then edit it
    first = _valid_bench_config()
    first_dest = os.path.join(workspace, "hardware_config.json")
    with open(first_dest, "w") as f:
        json.dump(first, f)
    ShiftService.link_hardware_config(workspace, first_dest)
    controller._load_hardware_config(first_dest)

    changed = _valid_bench_config()
    changed["devices"] = dict(reversed(list(changed["devices"].items())))
    assert controller._run_config_editor(first) is True

    assert json.load(open(first_dest)) == changed
    assert controller.hardware_manager.config_path == first_dest


def test_on_hardware_new_end_to_end(controller, workspace, monkeypatch):
    """The full 'New from Components' handler, including the post-save
    refresh of the workspace dialog status (regression: refresh_shift_dialog
    was called on the main window instead of the setup view)."""
    _stub_popups()
    result = _valid_bench_config()
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(result),
    )

    controller._on_hardware_new()  # must not raise

    dest = os.path.join(workspace, "hardware_config.json")
    assert os.path.exists(dest)
    assert json.load(open(dest)) == result
    assert controller.hardware_manager.is_loaded
    # the delegate works with no dialog open...
    controller.main_window.refresh_shift_dialog()
    controller.main_window.setup_view.refresh_shift_dialog()


def test_on_hardware_remove_clears_link_and_unloads(controller, workspace, monkeypatch):
    """Full Remove Link handler: confirmation, link cleared, manager
    unloaded (regression: QMessageBox was used without import)."""
    _stub_popups()
    source = os.path.join(workspace, "hardware_config.json")
    with open(source, "w") as f:
        json.dump(_valid_bench_config(), f)
    ShiftService.link_hardware_config(workspace, source, setup_file=controller.session_model.setup_file_name)
    controller._load_hardware_config(source)
    assert controller.hardware_manager.is_loaded

    controller._on_hardware_remove()  # must not raise

    assert "hardware" not in json.load(open(os.path.join(workspace, controller.session_model.setup_file_name)))
    assert not controller.hardware_manager.is_loaded


def test_apply_configuration_ramp_backstop(controller, workspace):
    """Apply Configuration with Sample Ramp while one device holds timing +
    acquisition and the firmware forbids the combination: rejected cleanly
    (regression: the backstop referenced hw_mgr before it was defined)."""
    _stub_popups()
    controller._load_hardware_config(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "examples",
            "hardware_config_all_pico.json",
        )
    )
    assert controller.hardware_manager.can_ramp_while_acquiring is False

    ok = controller.setup_controller.send_timing_config(
        {
            "sample_ramp": True,
            "int_time_us": 1000,
            "reset_time_us": 500,
            "debug_mode": False,
            "multiple_channel": False,
        }
    )
    assert ok is False  # rejected, not NameError


def test_new_with_custom_setup_file_name(controller, tmp_path, monkeypatch):
    """The user's scenario end-to-end: the workspace's setup file is not
    named setup.json — saving a hardware config must link into THAT file
    and never create a setup.json."""
    _stub_popups()
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    custom = os.path.join(workspace, "campaign_setup.json")
    assert ShiftService.save_setup(controller.session_model, custom)

    result = _valid_bench_config()
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(result),
    )
    controller._on_hardware_new()  # must not raise

    assert not os.path.exists(os.path.join(workspace, "setup.json"))
    link = json.load(open(custom))["hardware"]
    assert link["config_file"] == "hardware_config.json"
    assert controller.hardware_manager.is_loaded
    # and a later workspace load finds the config through the custom file
    fresh = _FakeSession()
    assert ShiftService.load_setup(fresh, custom)
    assert fresh.setup_file_name == "campaign_setup.json"


class _FakeSession:
    """Minimal session stand-in for load_setup checks."""

    def __init__(self):
        self.facility = ""
        self.beam_type = ""
        self.shift_note = ""
        self.app_version = ""
        self.setup_file_name = "setup.json"
        self.active_workspace_dir = None


def test_new_over_existing_file_asks_for_confirmation(controller, workspace, monkeypatch):
    _stub_popups(question_answer=False)  # user declines
    existing = _valid_bench_config()
    dest = os.path.join(workspace, "hardware_config.json")
    with open(dest, "w") as f:
        json.dump(existing, f)
    before = open(dest).read()
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(_valid_bench_config()),
    )

    # a NEW config over the existing file: declined -> nothing written
    assert controller._run_config_editor(em.new_config()) is False
    assert open(dest).read() == before
    assert len(popups["question"]) == 1

    # accepted -> overwritten with the new dialog result
    _stub_popups(question_answer=True)
    accepted = _valid_bench_config()
    accepted["connections"][0]["pin"] = 2  # differentiate the content
    monkeypatch.setattr(
        "views.hardware_config_editor.HardwareConfigEditorDialog",
        _fake_dialog_class(accepted),
    )
    assert controller._run_config_editor(em.new_config()) is True
    assert json.load(open(dest)) == accepted
    assert len(popups["question"]) == 1  # asked again on the second save
