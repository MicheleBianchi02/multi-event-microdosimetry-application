"""
Tests for the connection UI: the manual-connect dialog (one row per device,
port exclusion, per-row status, Done gating) and the step-1 connection card
(device rows, simulate gate). No physical hardware: ports are faked and the
manager's devices are replaced by duck-typed fakes.
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(__file__))

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from helpers import split_config
from models.session_model import SessionModel
from hardware.core.hardware_manager import HardwareManager

import views.manual_connect_dialog as mcd
from views.manual_connect_dialog import ManualConnectDialog
from views.connection_widget import ConnectionWidget

FAKE_PORTS = [
    {"name": "ttyACM0", "description": "Pico 2", "manufacturer": "RPi", "device": "/dev/ttyACM0"},
    {"name": "ttyACM1", "description": "PIC", "manufacturer": "Microchip", "device": "/dev/ttyACM1"},
    {"name": "ttyUSB0", "description": "Pico 2", "manufacturer": "RPi", "device": "/dev/ttyUSB0"},
]


def _bench_config() -> dict:
    """Split bench with a virtual acquisition: 3 serial devices + 1 dummy."""
    cfg = split_config()
    cfg["devices"]["ni_1"] = {
        "name": "Virtual readout",
        "driver": "dummy_v1",
        "connection": {"type": "dummy"},
        "capabilities": {
            "acquisition": {
                "num_channels": 2,
                "adc": {
                    "bits": 16,
                    "input_ranges_volts": [
                        {"min_volts": -0.2, "max_volts": 0.2},
                        {"min_volts": -2.5, "max_volts": 2.5},
                    ],
                    "default_input_range_volts": {"min_volts": -2.5, "max_volts": 2.5},
                    "supports_oversampling": False,
                },
            }
        },
    }
    return cfg


@pytest.fixture
def manager(tmp_path):
    path = tmp_path / "hw.json"
    path.write_text(json.dumps(_bench_config()))
    m = HardwareManager()
    m.load_config(str(path))
    return m


@pytest.fixture(autouse=True)
def fake_ports(monkeypatch):
    monkeypatch.setattr(mcd, "get_port", lambda: FAKE_PORTS)


class FakeSerialDevice:
    def __init__(self, port="/dev/fake", baud_rate=115200, connect_result=True):
        self._port = port
        self._baud_rate = baud_rate
        self.is_connected = False
        self._connect_result = connect_result
        self.set_params_calls = []

    def connect(self) -> bool:
        if self._connect_result:
            self.is_connected = True
            return True
        self.is_connected = False
        return False

    def disconnect(self) -> bool:
        self.is_connected = False
        return True

    def set_connection_params(self, port, baud_rate) -> None:
        self.set_params_calls.append((port, baud_rate))
        self._port = port
        self._baud_rate = baud_rate


# --------------------------------------------------------------------------
# ManualConnectDialog
# --------------------------------------------------------------------------

def test_one_row_per_device_serial_and_virtual(manager):
    dialog = ManualConnectDialog(manager)
    assert dialog.table.rowCount() == 4
    # serial devices get a combo, the virtual device does not
    assert set(dialog._combos.keys()) == {"pico_1", "pic_g2", "pico_t"}
    # the virtual row explains itself in the port column and needs no connect
    row = dialog._rows.index("ni_1")
    port_cell = dialog.table.cellWidget(row, mcd.COL_PORT)
    assert "virtual" in port_cell.text()
    # the virtual device is inherently connected (its connect() is a no-op)
    assert dialog._status_items["ni_1"].text() == "Connected"


def test_port_chosen_in_one_row_hidden_in_others(manager):
    dialog = ManualConnectDialog(manager)
    combo = dialog._combos["pico_1"]
    combo.setCurrentIndex(combo.findData("/dev/ttyACM0"))
    other_data = [dialog._combos["pic_g2"].itemData(i) for i in range(dialog._combos["pic_g2"].count())]
    assert "/dev/ttyACM0" not in other_data
    # the selection itself is preserved
    assert combo.currentData() == "/dev/ttyACM0"


def test_configured_port_preselected_when_present(manager, monkeypatch):
    # the derived persistent name appears among the enumerated devices
    # (e.g. a symlink listed by the OS): the combo preselects it
    ports = FAKE_PORTS + [
        {"name": "pico_t", "description": "atomiqa persistent name", "manufacturer": "-", "device": "/dev/atomiqa/pico_t"}
    ]
    monkeypatch.setattr(mcd, "get_port", lambda: ports)
    dialog = ManualConnectDialog(manager)
    assert dialog._combos["pico_t"].currentData() == "/dev/atomiqa/pico_t"


def test_connect_all_gates_done_on_role_devices(manager):
    for dev_id in ("pico_1", "pic_g2", "pico_t"):
        manager.devices[dev_id] = FakeSerialDevice()
    dialog = ManualConnectDialog(manager)
    # choose distinct ports so no two rows fight over the same handle
    for dev_id, port in zip(("pico_1", "pic_g2", "pico_t"),
                            ("/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0")):
        combo = dialog._combos[dev_id]
        combo.setCurrentIndex(combo.findData(port))
    dialog._connect_all()
    assert dialog._status_items["pico_1"].text() == "Connected"
    assert dialog.done_btn.isEnabled() is True

    # one role device failing keeps Done disabled
    manager.devices["pico_t"]._connect_result = False
    dialog._connect_all()
    assert "Failed" in dialog._status_items["pico_t"].text()
    assert dialog.done_btn.isEnabled() is False


def test_manual_mapping_reaches_driver_with_config_baud(manager):
    fake = FakeSerialDevice()
    manager.devices["pico_1"] = fake
    dialog = ManualConnectDialog(manager)
    combo = dialog._combos["pico_1"]
    combo.setCurrentIndex(combo.findData("/dev/ttyACM0"))
    dialog._connect_row("pico_1")
    assert fake.set_params_calls == [("/dev/ttyACM0", 115200)]
    assert fake.is_connected is True


def test_initial_errors_from_auto_connect_shown(manager):
    dialog = ManualConnectDialog(manager, initial_errors={"pico_t": "port not found"})
    assert "port not found" in dialog._status_items["pico_t"].text()


# --------------------------------------------------------------------------
# ConnectionWidget (step-1 card)
# --------------------------------------------------------------------------

def test_card_rows_follow_config(manager):
    widget = ConnectionWidget(SessionModel(), simulate=False, hardware_manager=manager)
    assert widget.table.rowCount() == 4
    # the virtual acquisition is inherently connected
    assert "1 of 4" in widget.summary.text()

    manager.devices["pico_t"] = FakeSerialDevice()
    manager.connect_device("pico_t")
    widget.on_connect_result(False)  # refresh path without the gate
    assert "2 of 4" in widget.summary.text()


def test_card_simulate_opens_gate_and_locks_buttons(manager):
    widget = ConnectionWidget(SessionModel(), simulate=False, hardware_manager=manager)
    emitted = []
    widget.connection_successfull.connect(lambda: emitted.append(True))

    widget.set_simulated(True)
    assert emitted == [True]
    assert widget.auto_btn.isEnabled() is False
    assert widget.manual_btn.isEnabled() is False
    assert "Simulation active" in widget.summary.text()

    widget.restart_event()
    assert widget.auto_btn.isEnabled() is True
    assert widget.manual_btn.isEnabled() is True


def test_card_connect_result_emits_gate_on_success(manager):
    widget = ConnectionWidget(SessionModel(), simulate=False, hardware_manager=manager)
    emitted = []
    widget.connection_successfull.connect(lambda: emitted.append(True))

    widget.on_connect_result(False, "boom")
    assert emitted == []
    widget.on_connect_result(True, "All devices connected.")
    assert emitted == [True]


def test_card_without_config():
    widget = ConnectionWidget(SessionModel(), simulate=False, hardware_manager=None)
    assert widget.table.rowCount() == 0
    assert "No hardware configuration loaded" in widget.summary.text()
