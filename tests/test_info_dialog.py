"""
Qt tests for the measurement InfoDialog's gain selection: the applied gain
can be re-picked from the loaded hardware config's gain table (each option
labelled with its total capacitance), so the archived gpio mask +
capacitance pair is always a physically realizable option — no arbitrary
values. Archives measured before the gain was recorded show a placeholder
until one is picked.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.session_model import SessionModel  # noqa: E402
from services.data_exporter import DataExporter  # noqa: E402
from views.info_dialog import InfoDialog  # noqa: E402


class FakeManager:
    """Duck-typed hardware manager with a small two-channel gain table."""

    is_loaded = True

    _TABLE = {
        1: [("1x", 22.0, [False, False]), ("15.6x", 342.0, [True, True])],
        2: [("1x", 22.0, [False, False]), ("5x", 110.0, [True, False])],
    }

    def get_gain_labels(self, channel):
        return [label for label, _, _ in self._TABLE[channel]]

    def get_capacitance(self, channel, label):
        return dict((l, c) for l, c, _ in self._TABLE[channel])[label]

    def get_gain_mask(self, channel, label):
        return dict((l, m) for l, _, m in self._TABLE[channel])[label]

    def get_compatible_gain_labels(self, channel, other_channel_label=None):
        return self.get_gain_labels(channel)

    def devices_summary(self):
        return {"devices": {}}


class _NoHardware:
    is_loaded = False


def _item_texts(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def test_old_archive_gain_pickable_from_table():
    """No recorded gain: placeholder selected, capacitance shown per
    option, picking one sets mask AND capacitance (saved as a pair)."""
    session = SessionModel()
    assert session.gpio_mask_ch1 is None
    assert session.capacitance_pf_ch1 == 100.0  # the pre-save default

    dlg = InfoDialog(session, FakeManager())
    texts = _item_texts(dlg.ch1_gain_input)
    assert texts[0] == "— gain not recorded —"
    assert "1x — 22 pF" in texts and "15.6x — 342 pF" in texts
    assert dlg.ch1_gain_input.currentData() is None

    dlg.ch1_gain_input.setCurrentIndex(
        dlg.ch1_gain_input.findData("15.6x")
    )
    dlg.ch1_gain_input.activated.emit(dlg.ch1_gain_input.currentIndex())
    assert session.gpio_mask_ch1 == [True, True]
    assert session.capacitance_pf_ch1 == 342.0
    assert dlg.ch1_mask_label.text() == "mask 11"

    # the exporter persists the consistent pair as the hardware snapshot
    settings = DataExporter.build_settings_dict(
        session, analysis_meta={}, hardware_manager=_NoHardware()
    )
    ch1 = settings["hardware_settings"]["channels"]["ch1"]
    assert ch1["gpio_mask"] == [True, True]
    assert ch1["capacitance_pF"] == 342.0


def test_recorded_gain_preselected():
    """A v3 archive's mask selects the matching option on open."""
    session = SessionModel()
    session.gpio_mask_ch1 = [True, True]
    session.capacitance_pf_ch1 = 342.0

    dlg = InfoDialog(session, FakeManager())
    assert dlg.ch1_gain_input.currentData() == "15.6x"
    # opening the dialog alone changes nothing
    assert session.capacitance_pf_ch1 == 342.0


def test_capacitance_only_match_fallback():
    """Mask absent but capacitance recorded: the matching option is
    selected by capacitance."""
    session = SessionModel()
    session.gpio_mask_ch2 = None
    session.capacitance_pf_ch2 = 110.0

    dlg = InfoDialog(session, FakeManager())
    assert dlg.ch2_gain_input.currentData() == "5x"


def test_no_hardware_config_readonly():
    """Without a loaded config there is no gain table: the stored value is
    shown, not editable."""
    session = SessionModel()
    dlg = InfoDialog(session, _NoHardware())
    assert not dlg.ch1_gain_input.isEnabled()
    assert dlg.ch1_gain_input.itemText(0) == "100 pF (no hardware config loaded)"
    # and the session is untouched
    assert session.capacitance_pf_ch1 == 100.0


def test_populate_does_not_emit_selection():
    """Populating the combos must not rewrite the session."""
    session = SessionModel()
    session.capacitance_pf_ch1 = 123.0
    InfoDialog(session, FakeManager())
    assert session.capacitance_pf_ch1 == 123.0
    assert session.gpio_mask_ch1 is None


def test_repicking_the_only_gain_applies_it():
    """No wiring: the table's single option (fixed capacitance, all pins
    off) may already be the displayed selection — re-picking it must still
    write the mask + capacitance to the session (currentIndexChanged alone
    stays silent when the index does not change)."""
    session = SessionModel()
    # an old archive that recorded the capacitance but no mask: the option
    # is pre-selected by capacitance, yet the session mask stays None
    session.gpio_mask_ch1 = None
    session.capacitance_pf_ch1 = 22.0

    dlg = InfoDialog(session, FakeManager())
    assert dlg.ch1_gain_input.currentData() == "1x"
    assert session.gpio_mask_ch1 is None  # display-only selection so far

    # the user picks the (already shown) item: activated must fire
    dlg.ch1_gain_input.activated.emit(dlg.ch1_gain_input.currentIndex())
    assert session.gpio_mask_ch1 == [False, False]
    assert session.capacitance_pf_ch1 == 22.0
