"""
End-to-end tests of the multi-analysis data path (no UI): workspace
scanning (services/measurement_catalog.py), the live record, filtering
and the evaluate/group pipeline (services/multi_analysis_service.py)
over fake measurement archives written to a temporary folder.
"""

import json
import os
import sys
import zipfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from models.data_model import DataModel  # noqa: E402
from models.session_model import SessionModel  # noqa: E402
from services.measurement_catalog import (  # noqa: E402
    LIVE_FILENAME,
    build_live_record,
    scan_workspace,
)
from services.multi_analysis_service import (  # noqa: E402
    apply_filters,
    distinct_values,
    format_value,
    refresh_values,
    run_pipeline,
)
from services.variable_catalog import default_catalog  # noqa: E402


def make_settings(
    timestamp="2026-09-18T10:00:00+02:00",
    depth=3.0,
    current=2.0,
    int_time_s=0.0021,
    yd=1.5,
    yd_unc=0.05,
    mean=120.0,
    cap=3422.0,
    problematic=False,
):
    return {
        "file_format_version": "3.0",
        "measurement_info": {
            "timestamp_start": timestamp,
            "timestamp_end": timestamp,
            "acquisition_mode": "debug",
            "is_problematic": problematic,
        },
        "experimental_setup": {
            "depth_cm": depth,
            "beam_current_nA": current,
            "beam_energy_MeV": 62.0,
        },
        "timing_settings": {
            "integration_time_s": int_time_s,
            "reset_time_s": 0.0005,
            "period_s": int_time_s + 0.0005,
            "sample_frequency_hz": 1.0 / (int_time_s + 0.0005),
        },
        "hardware_settings": {
            "adc": {
                "bits": 12,
                "input_range_volts": {"min_volts": 0.0, "max_volts": 3.3},
            },
            "channels": {"ch1": {"gpio_mask": [1, 1, 1], "capacitance_pF": cap}},
            "single_channel_mode": True,
        },
        "analysis": {
            "results": {
                "var_autocov": {
                    "N": [1000, 0, 0, 0],
                    "Mean": [mean, 0.4, 0, 0],
                    "Variance": [300.0, 6.0, 0, 0],
                    "AutoCorrelation": [100.0, 3.0, 0, 0],
                    "y_bar_d": [yd, yd_unc, 0, 0],
                },
            },
        },
    }


def write_zip(folder, name, settings):
    path = folder / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("settings.json", json.dumps(settings))
        zf.writestr("data.csv", "timestamp,ch1_start,ch1_stop\n0,1,2\n")
    return str(path)


@pytest.fixture()
def workspace(tmp_path):
    """A small depth-scan campaign: 2 depths × 2 integration times."""
    write_zip(tmp_path, "m_1030.zip", make_settings(timestamp="2026-09-18T10:30:00+02:00", depth=3.0, int_time_s=0.0021, yd=1.5, current=2.0))
    write_zip(tmp_path, "m_1031.zip", make_settings(timestamp="2026-09-18T10:31:00+02:00", depth=3.0, int_time_s=0.0042, yd=1.6, current=2.0))
    write_zip(tmp_path, "m_1032.zip", make_settings(timestamp="2026-09-18T10:32:00+02:00", depth=5.0, int_time_s=0.0021, yd=2.5, current=2.0))
    write_zip(tmp_path, "m_1033.zip", make_settings(timestamp="2026-09-18T10:33:00+02:00", depth=5.0, int_time_s=0.0042, yd=2.6, current=4.0))
    return str(tmp_path)


# --- scanning ---------------------------------------------------------------

def test_scan_workspace_finds_and_sorts_measurements(workspace):
    records = scan_workspace(workspace)
    assert [os.path.basename(r.path) for r in records] == [
        "m_1030.zip", "m_1031.zip", "m_1032.zip", "m_1033.zip",
    ]
    assert all(r.selected for r in records)
    assert records[0].timestamp_epoch < records[-1].timestamp_epoch


def test_scan_skips_foreign_and_corrupted_archives(workspace, tmp_path):
    # not a measurement zip
    (tmp_path / "other.zip").write_bytes(b"not a zip")
    # legacy csv ignored
    (tmp_path / "legacy.csv").write_text("timestamp,ch1\n0,1\n")
    records = scan_workspace(workspace)
    assert len(records) == 4


def test_scan_missing_folder_is_empty():
    assert scan_workspace("/nonexistent/path") == []


# --- live record -------------------------------------------------------------

def test_build_live_record_matches_archive_shape():
    session = SessionModel()
    session.depth_cm = 3.0
    session.beam_current_na = 2.0
    session.int_time = 0.0021
    session.timestamp_start = "2026-09-18T11:00:00+02:00"
    data_model = DataModel()
    data_model.result["var_autocov"]["y_bar_d"] = [1.7, 0.06, 0, 0]
    data_model.result["var_autocov"]["N"] = [500, 0, 0, 0]

    record = build_live_record(session, data_model)
    assert record.is_live
    assert record.filename == LIVE_FILENAME
    cat = default_catalog()
    values = cat.extract_values(record.settings)
    assert values["autocov_yd_ch1"][0] == pytest.approx(1.7)
    assert values["depth_cm"][0] == pytest.approx(3.0)


# --- filtering ---------------------------------------------------------------

def test_distinct_values_and_filters(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    assert distinct_values(records, "depth_cm") == ["3", "5"]
    assert distinct_values(records, "int_time_ms") == ["2.1", "4.2"]

    only_3cm = apply_filters(records, {"depth_cm": "3"})
    assert len(only_3cm) == 2
    both = apply_filters(records, {"depth_cm": "3", "int_time_ms": "4.2"})
    assert len(both) == 1


def test_format_value():
    assert format_value(3.0) == "3"
    assert format_value(float("nan")) == "—"
    assert format_value(None) == "—"
    assert format_value(0.0021) == "0.0021"


# --- pipeline ----------------------------------------------------------------

def test_pipeline_yd_vs_current_with_grouping(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    result = run_pipeline(records, "beam_current_nA", "autocov_yd_ch1", group_var_id="int_time_ms")
    assert result.n_selected == 4
    assert result.n_plotted == 4
    labels = sorted(s.label for s in result.series)
    assert labels == ["2.1", "4.2"]
    by_label = {s.label: s for s in result.series}
    assert list(by_label["2.1"].y) == pytest.approx([1.5, 2.5])
    assert list(by_label["4.2"].y) == pytest.approx([1.6, 2.6])
    assert result.warnings == []  # all measurements share gain/ADC


def test_pipeline_index_reflects_filter_and_order(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    records[1].selected = False  # drop m_1031
    result = run_pipeline(records, "index", "autocov_yd_ch1")
    single = result.series[0]
    assert list(single.x) == [1.0, 2.0, 3.0]  # renumbered after deselection
    assert list(single.y) == pytest.approx([1.5, 2.5, 2.6])


def test_pipeline_excludes_non_finite_points(workspace, tmp_path):
    # a measurement without computed results (empty analysis.results → NaN yd)
    broken = make_settings(timestamp="2026-09-18T10:34:00+02:00")
    broken["analysis"]["results"] = {}
    write_zip(tmp_path, "m_1034.zip", broken)

    records = scan_workspace(str(tmp_path))
    refresh_values(records)
    result = run_pipeline(records, "beam_current_nA", "autocov_yd_ch1")
    assert result.n_selected == 5
    assert result.n_plotted == 4
    assert result.n_excluded == 1
    assert result.excluded_files == ["m_1034.zip"]


def test_pipeline_formula_with_uncertainty(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    result = run_pipeline(records, "beam_current_nA", "autocov_yd_ch1 / beam_current_nA")
    s = result.series[0]
    # y = 1.5/2, 1.6/2, 2.5/2, 2.6/4 with relative error of yd only
    assert list(s.y) == pytest.approx([0.75, 0.8, 1.25, 0.65])
    assert list(s.y_unc) == pytest.approx([0.05 / 2, 0.05 / 2, 0.05 / 2, 0.05 / 4])


def test_pipeline_live_point_is_a_separate_series(workspace):
    records = scan_workspace(workspace)
    session = SessionModel()
    session.depth_cm = 3.0
    session.beam_current_na = 6.0
    session.int_time = 0.0021
    session.timestamp_start = "2026-09-18T12:00:00+02:00"
    data_model = DataModel()
    data_model.result["var_autocov"]["y_bar_d"] = [3.3, 0.1, 0, 0]
    data_model.result["var_autocov"]["N"] = [100, 0, 0, 0]
    live = build_live_record(session, data_model)
    refresh_values([live])

    records.append(live)
    result = run_pipeline(records, "beam_current_nA", "autocov_yd_ch1", group_var_id="int_time_ms")
    live_series = [s for s in result.series if s.is_live]
    assert len(live_series) == 1
    assert live_series[0].y[0] == pytest.approx(3.3)
    assert live_series[0].label == "2.1"  # grouped with its integration time


def test_pipeline_mixing_gain_warning(workspace, tmp_path):
    write_zip(
        tmp_path,
        "m_1040.zip",
        make_settings(timestamp="2026-09-18T10:40:00+02:00", cap=100.0, depth=3.0, int_time_s=0.0021),
    )
    records = scan_workspace(str(tmp_path))
    refresh_values(records)
    result = run_pipeline(records, "beam_current_nA", "autocov_yd_ch1")
    assert any("raw counts" in w for w in result.warnings)
    # charge variant does not warn
    result_pc = run_pipeline(records, "beam_current_nA", "autocov_yd_pc_ch1")
    assert result_pc.warnings == []


def test_pipeline_invalid_formula_raises(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    from core.formula import FormulaError

    with pytest.raises(FormulaError):
        run_pipeline(records, "beam_current_nA", "yd_ch1 +")


# --- multi-Y ------------------------------------------------------------------

def test_pipeline_multi_y_creates_series_per_expression(workspace):
    records = scan_workspace(workspace)
    refresh_values(records)
    result = run_pipeline(
        records,
        "beam_current_nA",
        ["autocov_yd_ch1", "autocov_mean_ch1"],
    )
    labels = sorted(s.y_label for s in result.series)
    assert labels == ["autocov_mean_ch1", "autocov_yd_ch1"]
    by_label = {s.y_label: s for s in result.series}
    assert list(by_label["autocov_yd_ch1"].y) == pytest.approx([1.5, 1.6, 2.5, 2.6])
    # Mean is the same constant for all four fake measurements
    assert list(by_label["autocov_mean_ch1"].y) == pytest.approx([120.0] * 4)
    assert list(by_label["autocov_mean_ch1"].x) == pytest.approx([2.0, 2.0, 2.0, 4.0])


def test_pipeline_multi_y_excludes_records_invalid_in_any_expression(tmp_path):
    # varcov missing entirely in this archive -> its yd is NaN there
    settings = make_settings(timestamp="2026-09-18T10:30:00+02:00")
    write_zip(tmp_path, "only_var.zip", settings)

    records = scan_workspace(str(tmp_path))
    refresh_values(records)
    result = run_pipeline(records, "beam_current_nA", ["var_yd_ch1", "varcov_yd_ch1"])
    # comparable series: a record must be valid for EVERY expression
    assert result.n_selected == 1
    assert result.n_plotted == 0
    assert result.n_excluded == 1
