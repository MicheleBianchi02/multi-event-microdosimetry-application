"""
Qt smoke tests for the multi-analysis dialog: opens it on a fake
workspace, checks the measurement table, the filters, formula
validation feedback, the pipeline wiring behind replot() and the live
point. Rendering itself is not verified here — the data pipeline has
its own tests (test_multi_analysis.py).
"""

import json
import os
import sys
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from models.data_model import DataModel  # noqa: E402
from models.session_model import SessionModel  # noqa: E402
from views.multi_analysis_dialog import DEFAULT_PRESETS, MultiAnalysisDialog  # noqa: E402


def make_settings(timestamp, depth, int_time_s, yd, current=2.0, problematic=False):
    return {
        "measurement_info": {
            "timestamp_start": timestamp,
            "timestamp_end": timestamp,
            "is_problematic": problematic,
        },
        "experimental_setup": {
            "depth_cm": depth,
            "beam_current_nA": current,
        },
        "timing_settings": {"integration_time_s": int_time_s},
        "hardware_settings": {
            "adc": {
                "bits": 12,
                "input_range_volts": {"min_volts": 0.0, "max_volts": 3.3},
            },
            "channels": {"ch1": {"gpio_mask": [1, 1, 1], "capacitance_pF": 3422.0}},
        },
        "analysis": {
            "results": {
                "var_autocov": {
                    "N": [1000, 0, 0, 0],
                    "Mean": [120.0, 0.4, 0, 0],
                    "y_bar_d": [yd, 0.05, 0, 0],
                },
            },
        },
    }


@pytest.fixture()
def workspace(tmp_path):
    zips = [
        ("m1.zip", "2026-09-18T10:30:00+02:00", 3.0, 0.0021, 1.5),
        ("m2.zip", "2026-09-18T10:31:00+02:00", 3.0, 0.0042, 1.6),
        ("m3.zip", "2026-09-18T10:32:00+02:00", 5.0, 0.0021, 2.5),
        ("m4.zip", "2026-09-18T10:33:00+02:00", 5.0, 0.0042, 2.6, ),  # plain
    ]
    for name, ts, depth, int_s, yd in zips:
        with zipfile.ZipFile(tmp_path / name, "w") as zf:
            zf.writestr("settings.json", json.dumps(make_settings(ts, depth, int_s, yd)))
            zf.writestr("data.csv", "timestamp,ch1_start,ch1_stop\n0,1,2\n")
    return str(tmp_path)


def make_dialog(workspace):
    session = SessionModel()
    session.timestamp_start = "2026-09-18T12:00:00+02:00"
    session.beam_current_na = 6.0
    session.depth_cm = 3.0
    data_model = DataModel()
    data_model.result["var_autocov"]["y_bar_d"] = [3.3, 0.1, 0, 0]
    data_model.result["var_autocov"]["N"] = [100, 0, 0, 0]
    dialog = MultiAnalysisDialog(session, data_model)
    dialog.open_with(workspace)
    return dialog


def test_dialog_opens_workspace_and_shows_table(workspace):
    dialog = make_dialog(workspace)
    assert dialog.table.rowCount() == 4
    assert "4 measurements" in dialog.status_lbl.text()
    assert dialog.x_status.text() == "empty"


def test_formulas_plot_and_group(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert dialog.x_status.text() == "✓"
    assert dialog._last_result is not None
    assert dialog._last_result.n_plotted == 4
    # single series without grouping
    assert len(dialog._last_result.series) == 1

    # group by integration time -> two series
    dialog.group_combo.setCurrentIndex(
        dialog.group_combo.findData("int_time_ms")
    )
    dialog.replot()
    labels = sorted(s.label for s in dialog._last_result.series)
    assert labels == ["2.1", "4.2"]


def test_invalid_formula_shows_error(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current + ")  # syntax error
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert "⚠" in dialog.x_status.text()
    assert dialog._last_result is None


def test_unknown_variable_suggestion_shown(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current")  # missing the _nA suffix
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert "beam_current_nA" in dialog.x_status.text()


def test_live_point_inclusion(workspace):
    dialog = make_dialog(workspace)
    dialog.live_check.setChecked(True)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    live = [s for s in dialog._last_result.series if s.is_live]
    assert len(live) == 1
    assert live[0].y[0] == pytest.approx(3.3)


def test_filters_narrow_table(workspace, tmp_path):
    # one problematic measurement
    with zipfile.ZipFile(tmp_path / "m5.zip", "w") as zf:
        zf.writestr(
            "settings.json",
            json.dumps(make_settings("2026-09-18T10:34:00+02:00", 3.0, 0.0021, 9.9, problematic=True)),
        )
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert dialog.table.rowCount() == 5

    # the problematic row is hidden by default (excluded by the checkbox)
    hidden = [row for row in range(5) if dialog.table.isRowHidden(row)]
    assert len(hidden) == 1

    # depth filter: only the two 3 cm, non-problematic rows stay visible
    combo = dialog.filter_combos["depth_cm"]
    combo.setCurrentIndex(combo.findData("3"))
    visible = [row for row in range(5) if not dialog.table.isRowHidden(row)]
    assert len(visible) == 2
    dialog.replot()
    assert dialog._last_result.n_selected == 2

    # unchecking the exclusion brings the problematic row back
    dialog.exclude_problematic_check.setChecked(False)
    visible = [row for row in range(5) if not dialog.table.isRowHidden(row)]
    assert len(visible) == 3
    dialog.replot()
    assert dialog._last_result.n_selected == 3


def test_preset_defaults_load(workspace):
    dialog = make_dialog(workspace)
    assert dialog.preset_combo.count() >= len(DEFAULT_PRESETS)
    dialog.preset_combo.setCurrentIndex(0)  # ȳ_D vs depth
    assert dialog.preset_combo.currentIndex() == 0
    assert dialog.x_edit.text() == "depth_cm"
    assert dialog.y_edit.toPlainText() == "autocov_yd_ch1"


def test_checkbox_deselect_removes_points(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("index")
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert list(dialog._last_result.series[0].x) == [1.0, 2.0, 3.0, 4.0]

    dialog.records[0].selected = False
    dialog._refresh_table()
    dialog.replot()
    assert list(dialog._last_result.series[0].x) == [1.0, 2.0, 3.0]


def test_user_preset_roundtrip(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("depth_cm")
    dialog.y_edit.setPlainText("autocov_yd_ch1 / beam_current_nA")
    # save under a known name without the input dialog
    dialog.user_presets.append(
        {"name": "my preset", "x": "depth_cm", "y": "autocov_yd_ch1 / beam_current_nA", "group": ""}
    )
    dialog._write_user_presets()
    assert os.path.exists(os.path.join(workspace, "analysis_presets.json"))

    dialog2 = MultiAnalysisDialog(SessionModel(), DataModel())
    dialog2.workspace_dir = workspace
    dialog2._load_user_presets()
    names = [dialog2.preset_combo.itemText(i) for i in range(dialog2.preset_combo.count())]
    assert "my preset" in names


# --- units box (channels / pC) -------------------------------------------------

def visible_variable_ids(dialog):
    ids = set()
    for i in range(dialog.variable_tree.topLevelItemCount()):
        cat = dialog.variable_tree.topLevelItem(i)
        if cat.isHidden():
            continue
        for j in range(cat.childCount()):
            child = cat.child(j)
            if not child.isHidden():
                ids.add(child.data(0, Qt.ItemDataRole.UserRole))
    return ids


def test_units_box_halves_variable_tree(workspace):
    dialog = make_dialog(workspace)
    counts_visible = visible_variable_ids(dialog)
    assert "autocov_yd_ch1" in counts_visible
    assert "autocov_yd_pc_ch1" not in counts_visible

    dialog.units_combo.setCurrentIndex(1)  # charge (pC)
    pc_visible = visible_variable_ids(dialog)
    assert "autocov_yd_pc_ch1" in pc_visible
    assert "autocov_yd_ch1" not in pc_visible
    # metadata variables stay visible in both modes
    assert "beam_current_nA" in counts_visible and "beam_current_nA" in pc_visible
    # each mode shows one variant per quantity: same amount of entries,
    # and roughly half of what the catalog holds in total (the other
    # variant is always hidden)
    assert len(pc_visible) == len(counts_visible)
    assert len(counts_visible) < len(dialog.catalog)


def test_units_switch_rewrites_formulas(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("autocov_yd_ch1")
    dialog.y_edit.setPlainText("autocov_mean_ch1 / 2")
    dialog.units_combo.setCurrentIndex(1)  # -> pC
    assert dialog.x_edit.text() == "autocov_yd_pc_ch1"
    assert dialog.y_edit.toPlainText() == "autocov_mean_pc_ch1 / 2"
    # and back to counts
    dialog.units_combo.setCurrentIndex(0)
    assert dialog.x_edit.text() == "autocov_yd_ch1"
    assert dialog.y_edit.toPlainText() == "autocov_mean_ch1 / 2"


def test_pC_formula_evaluates_with_conversion(workspace):
    dialog = make_dialog(workspace)
    dialog.units_combo.setCurrentIndex(1)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_pc_ch1")
    dialog.replot()
    assert dialog._last_result is not None
    assert dialog._last_result.n_plotted == 4


# --- multi-Y ---------------------------------------------------------------------

def test_multi_y_two_expressions_two_series(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1\nautocov_mean_ch1")
    dialog.replot()
    assert dialog._last_result is not None
    labels = sorted(s.y_label for s in dialog._last_result.series)
    assert labels == ["autocov_mean_ch1", "autocov_yd_ch1"]
    # colour-by is meaningless with several expressions: disabled
    assert not dialog.group_combo.isEnabled()


def test_multi_y_y_status_reports_line_numbers(workspace):
    dialog = make_dialog(workspace)
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1\nnope_ch1")
    dialog.replot()
    assert "line 2" in dialog.y_status.text()
    assert "nope_ch1" in dialog.y_status.text()


# --- visibility of problems --------------------------------------------------------

def test_nothing_plotted_message_is_prominent(tmp_path):
    # archive with zeroed results: N=0 -> excluded by design
    settings = make_settings("2026-09-18T10:30:00+02:00", 3.0, 0.0021, 1.5)
    settings["analysis"]["results"]["var_autocov"] = {
        k: [0, 0, 0, 0]
        for k in ("N", "Mean", "y_bar_d")
    }
    with zipfile.ZipFile(tmp_path / "zeroed.zip", "w") as zf:
        zf.writestr("settings.json", json.dumps(settings))

    dialog = make_dialog(str(tmp_path))
    dialog.x_edit.setText("beam_current_nA")
    dialog.y_edit.setPlainText("autocov_yd_ch1")
    dialog.replot()
    assert dialog._last_result.n_plotted == 0
    assert "NOTHING PLOTTED" in dialog.status_lbl.text()
