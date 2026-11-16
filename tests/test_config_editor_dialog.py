"""
Qt smoke tests for the hardware config editor dialog: the editable firmware
version of a device, and importing a circuit fragment from a file (the
"Save as Component…" round-trip the shipped examples/ATOMIQA_1.json was
produced with). Validation itself is covered by test_editor_model.py.
"""

import json
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication, QFormLayout, QLineEdit  # noqa: E402

_app = QApplication.instance() or QApplication([])

from helpers import all_pico_config  # noqa: E402
from views.hardware_config_editor import HardwareConfigEditorDialog  # noqa: E402


def _field_for_label(form: QFormLayout, label_text: str):
    for row in range(form.rowCount()):
        label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if label is not None and field is not None:
            widget = label.widget()
            if widget is not None and widget.text().startswith(label_text):
                return field.widget()
    return None


def test_firmware_version_is_loaded_edited_and_cleared():
    cfg = all_pico_config()
    cfg["devices"]["pico_1"]["firmware_version"] = "1.0"
    dialog = HardwareConfigEditorDialog(cfg, [])
    dialog.device_list.setCurrentRow(0)  # pico_1

    fw_input = _field_for_label(dialog.device_form, "Firmware version:")
    assert isinstance(fw_input, QLineEdit)
    assert fw_input.text() == "1.0"  # the stored version shows up

    fw_input.textEdited.emit(" 2.3 ")  # what typing produces
    assert dialog.get_config()["devices"]["pico_1"]["firmware_version"] == "2.3"

    fw_input.textEdited.emit("")  # cleared: the key leaves the file again
    assert "firmware_version" not in dialog.get_config()["devices"]["pico_1"]


def test_circuit_added_from_file_round_trips(tmp_path, monkeypatch):
    """A circuit saved with "Save as Component…" (the shape of
    examples/ATOMIQA_1.json) imports back through Add from File…"""
    fragment = {
        "kind": "circuit",
        "suggested_id": "ATOMIQA_1",
        "data": {
            "name": "ATOMIQA_1",
            "fixed_capacitance_pF": 100.0,
            "switchable_capacitors": [],
            "model": "ATOMIQA_1_R1.1#11",
            "note": "Gated integrator.",
        },
    }
    path = tmp_path / "atomiqa.json"
    path.write_text(json.dumps(fragment))

    import views.hardware_config_editor as hce

    monkeypatch.setattr(
        hce.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), ""))
    )
    dialog = HardwareConfigEditorDialog(all_pico_config(), [])
    dialog._add_fragment_from_file("circuit")

    circuit = dialog.get_config()["circuits"].get("ATOMIQA_1")
    assert circuit is not None
    assert circuit["fixed_capacitance_pF"] == 100.0
    # an extra unbound circuit is a warning, not an error: saving stays possible
    assert dialog.save_btn.isEnabled()


def test_circuit_add_from_file_refuses_a_device_fragment(tmp_path, monkeypatch):
    fragment = {
        "kind": "device",
        "suggested_id": "pic_1",
        "data": {"name": "PIC", "driver": "pic_serial_v1",
                 "connection": {"type": "serial", "baud_rate": 115200}},
    }
    path = tmp_path / "pic.json"
    path.write_text(json.dumps(fragment))

    import views.hardware_config_editor as hce

    refused = []
    monkeypatch.setattr(
        hce.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), ""))
    )
    monkeypatch.setattr(
        hce.QMessageBox, "warning", staticmethod(lambda *a, **k: refused.append(a) or 1)
    )
    dialog = HardwareConfigEditorDialog(all_pico_config(), [])
    before = list(dialog.get_config()["circuits"])
    dialog._add_fragment_from_file("circuit")

    assert dialog.get_config()["circuits"].keys() == set(before)
    assert refused  # the user was told why


def test_shipped_example_circuit_fragment_is_loadable():
    """examples/ATOMIQA_1.json is a circuit saved via 'Save as Component…':
    it must stay a loadable fragment (the import path above)."""
    from hardware.config import editor_model as em

    example = os.path.join(
        os.path.dirname(__file__), "..", "examples", "ATOMIQA_1.json"
    )
    fragment = em.load_fragment(example)
    assert fragment["kind"] == "circuit"
    assert fragment["data"]["fixed_capacitance_pF"] == 100.0


def test_channel_termination_and_adc_input_round_trip():
    """The Channels tab sets how the signal reaches the acquisition device
    (termination dropdown) and which physical terminal it lands on (ADC
    input) — e.g. RSE + AI6 — and a reopened editor shows both again."""
    cfg = all_pico_config()
    dialog = HardwareConfigEditorDialog(cfg, [])
    assert dialog.ch1_termination.currentData() == "differential"  # default

    dialog.ch1_termination.setCurrentIndex(1)  # RSE — applies immediately
    dialog.ch1_adc_input.setText("AI6")
    dialog.ch1_adc_input.editingFinished.emit()

    ch1 = dialog.get_config()["channels"]["ch1"]
    assert ch1["termination"] == "rse"
    assert ch1["adc_input"] == "AI6"

    reopened = HardwareConfigEditorDialog(dialog.get_config(), [])
    assert reopened.ch1_termination.currentData() == "rse"
    assert reopened.ch1_adc_input.text() == "AI6"
