"""
Regression tests for DataModel (BUG.md B1).

The measurement archive must store the raw converted samples: what
`ingest_raw_batch` returns (and what `data_model.data` holds) must be the
unfiltered data, with NaN only for overflow / negative differences. ROI
exclusion must not leak NaN into the returned/stored arrays — ROIs live in
`filtered_data` and are re-applied from settings.json on load.
"""

import os
import sys

import numpy as np
import pytest

# Make the app package importable when running from models/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.data_model import DataModel  # noqa: E402
from core.constants import overflow_sentinel  # noqa: E402


def test_operational_batch_returns_raw_data_despite_rois():
    dm = DataModel()
    raw_batch = [(100, 200), (3000, 4000), (500, 600)]

    # Channel ROI on ch1 that excludes the 3000 sample; time ROI that
    # excludes the last sample.
    rois = [(1, 0, 1000)]
    time_rois = [(0.0, 0.1)]  # sample_freq 10 Hz -> keeps only index 0

    result = dm.ingest_raw_batch(
        raw_batch=raw_batch,
        debug_mode=False,
        rois=rois,
        sample_frequency=10.0,
        time_rois=time_rois,
    )

    # Returned data is raw: every sample present, no ROI-induced NaN
    expected = np.array([[100.0, 200.0], [3000.0, 4000.0], [500.0, 600.0]])
    np.testing.assert_array_equal(result, expected)
    np.testing.assert_array_equal(dm.data[: dm.ptr, :], expected)

    # filtered_data still applies the ROIs (ch1 ROI excludes 3000,
    # time ROI excludes the last row entirely)
    assert np.isnan(dm.filtered_data[1, 0])
    assert np.isnan(dm.filtered_data[2, 0])
    assert np.isnan(dm.filtered_data[2, 1])

    # Histogram counts only the ROI-surviving ch1 sample(s)
    assert dm.bar_data_ch1[100] == 1
    assert dm.bar_data_ch1[3000] == 0


def test_operational_batch_keeps_overflow_as_nan():
    dm = DataModel()
    raw_batch = [(overflow_sentinel(12), 100)]  # one past the 12-bit full scale

    result = dm.ingest_raw_batch(
        raw_batch=raw_batch,
        debug_mode=False,
        rois=None,
        sample_frequency=10.0,
        time_rois=[],
    )

    # Overflow is the one legitimate NaN source in the stored data
    assert np.isnan(result[0, 0])
    assert result[0, 1] == 100.0
    assert dm.n_overflow_ch1 == 1


def test_debug_batch_returns_raw_start_stop_and_difference():
    dm = DataModel()
    # ((ch1_start, ch1_stop), (ch2_start, ch2_stop))
    raw_batch = [((10, 110), (20, 120)), ((30, 20), (40, 140))]

    start, stop, diff = dm.ingest_raw_batch(
        raw_batch=raw_batch,
        debug_mode=True,
        rois=[(1, 0, 50)],  # would exclude the 100 difference from ch1 stats
        sample_frequency=10.0,
        time_rois=[],
    )

    np.testing.assert_array_equal(start, np.array([[10.0, 20.0], [30.0, 40.0]]))
    np.testing.assert_array_equal(stop, np.array([[110.0, 120.0], [20.0, 140.0]]))
    # Raw differences: second ch1 sample is negative -> NaN (not ROI NaN)
    np.testing.assert_array_equal(
        diff, np.array([[100.0, 100.0], [np.nan, 100.0]])
    )
    np.testing.assert_array_equal(dm.start_data[: dm.ptr, :], start)
    np.testing.assert_array_equal(dm.stop_data[: dm.ptr, :], stop)
    np.testing.assert_array_equal(dm.data[: dm.ptr, :], diff)

    # The ROI is applied only to the filtered view / statistics
    assert np.isnan(dm.filtered_data[0, 0])
    assert dm.result["var"]["N"][0] == 0  # ch1 has no ROI-surviving sample
