"""
Correctness tests for calc_stat.calculate_results.

Each test constructs a dataset whose expected statistics can be computed
analytically (or via plain NumPy), then calls calculate_results and asserts
that the returned values match within tight or reasonable tolerances.

Dictionary layout reminder
--------------------------
Every sub-list has 4 entries:   [val_ch1, unc_ch1, val_ch2, unc_ch2]
indices                           0        1        2        3

Channel indexing inside calc_stat
----------------------------------
  idx=0 → ch1  → ii=0   (values at [0], uncertainties at [1])
  idx=1 → ch2  → ii=2   (values at [2], uncertainties at [3])

The interleaved split (get_ch_matrix with both_ch_cov=False) converts
  channel_data → y = channel_data[1::2]
                 x = channel_data[0::2][:len(y)]
so statistics inside "var_cov" / "var_autocov" are computed on y (or x for
the other channel) – i.e. every *other* sample.  The "var" block uses the
full (NaN-filtered) channel.
"""

import os
import sys

import numpy as np
import pytest

# the repo root is on sys.path via the root conftest.py
from core.calc_stat import calculate_results

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RNG = np.random.default_rng(0)  # fixed seed → deterministic synthetic data


def _make_data(ch0: np.ndarray, ch1: np.ndarray) -> np.ndarray:
    """Stack two 1-D arrays into shape (n, 2)."""
    n = min(len(ch0), len(ch1))
    return np.column_stack((ch0[:n], ch1[:n]))


def _interleaved_y(channel_data: np.ndarray) -> np.ndarray:
    """Reproduce get_ch_matrix splitting for one channel."""
    d = channel_data[~np.isnan(channel_data)]
    y = d[1::2]
    return y


def _interleaved_xy(channel_data: np.ndarray):
    """Reproduce get_ch_matrix splitting: returns (y, x)."""
    d = channel_data[~np.isnan(channel_data)]
    y = d[1::2]
    x = d[0::2][: len(y)]
    return y, x


# ---------------------------------------------------------------------------
# 1. "var" block – full-channel statistics
# ---------------------------------------------------------------------------


class TestVarBlock:
    """Tests for result["var"] using the full NaN-filtered channel."""

    @pytest.fixture
    def simple_data(self):
        """200-sample data with known mean/variance for each channel."""
        np.random.seed(7)
        ch0 = np.random.normal(loc=10.0, scale=3.0, size=200)
        ch1 = np.random.normal(loc=5.0, scale=1.5, size=200)
        return _make_data(ch0, ch1)

    def test_sample_count(self, simple_data):
        res = calculate_results(simple_data)
        # N should equal the number of non-NaN samples in each column
        assert res["var"]["N"][0] == np.sum(~np.isnan(simple_data[:, 0]))
        assert res["var"]["N"][2] == np.sum(~np.isnan(simple_data[:, 1]))

    def test_mean_value_ch1(self, simple_data):
        res = calculate_results(simple_data)
        expected_mean = np.mean(simple_data[:, 0])
        assert res["var"]["Mean"][0] == pytest.approx(expected_mean, rel=1e-9)

    def test_mean_value_ch2(self, simple_data):
        res = calculate_results(simple_data)
        expected_mean = np.mean(simple_data[:, 1])
        assert res["var"]["Mean"][2] == pytest.approx(expected_mean, rel=1e-9)

    def test_variance_value_ch1(self, simple_data):
        res = calculate_results(simple_data)
        expected_var = np.var(simple_data[:, 0], ddof=1)
        assert res["var"]["Variance"][0] == pytest.approx(expected_var, rel=1e-9)

    def test_variance_value_ch2(self, simple_data):
        res = calculate_results(simple_data)
        expected_var = np.var(simple_data[:, 1], ddof=1)
        assert res["var"]["Variance"][2] == pytest.approx(expected_var, rel=1e-9)

    def test_mean_uncertainty_ch1(self, simple_data):
        """unc(mean) = sqrt(var / n)."""
        res = calculate_results(simple_data)
        d = simple_data[:, 0]
        n = len(d)
        expected_unc = np.sqrt(np.var(d, ddof=1) / n)
        assert res["var"]["Mean"][1] == pytest.approx(expected_unc, rel=1e-9)

    def test_mean_uncertainty_ch2(self, simple_data):
        res = calculate_results(simple_data)
        d = simple_data[:, 1]
        n = len(d)
        expected_unc = np.sqrt(np.var(d, ddof=1) / n)
        assert res["var"]["Mean"][3] == pytest.approx(expected_unc, rel=1e-9)

    def test_variance_uncertainty_ch1(self, simple_data):
        """unc(var) = sqrt(2 * var^2 / (n - 1))."""
        res = calculate_results(simple_data)
        d = simple_data[:, 0]
        n = len(d)
        var = np.var(d, ddof=1)
        expected_unc = np.sqrt(2 * var**2 / (n - 1))
        assert res["var"]["Variance"][1] == pytest.approx(expected_unc, rel=1e-9)

    def test_y_bar_d_ch1(self, simple_data):
        """y_bar_d = var / mean  (safe_mean = mean because mean != 0)."""
        res = calculate_results(simple_data)
        d = simple_data[:, 0]
        mean = np.mean(d)
        var = np.var(d, ddof=1)
        expected_yd = var / mean
        assert res["var"]["y_bar_d"][0] == pytest.approx(expected_yd, rel=1e-9)

    def test_y_bar_d_uncertainty_ch1(self, simple_data):
        """unc(y_bar_d) via error propagation."""
        res = calculate_results(simple_data)
        d = simple_data[:, 0]
        n = len(d)
        mean = np.mean(d)
        var = np.var(d, ddof=1)
        expected_unc = (var / mean) * np.sqrt(2 / (n - 1) + var / (n * mean**2))
        assert res["var"]["y_bar_d"][1] == pytest.approx(expected_unc, rel=1e-9)

    def test_y_bar_d_zero_mean_fallback(self):
        """When mean == 0 the code uses safe_mean = 1.0, so y_bar_d = var."""
        # Symmetric data: mean = 0 exactly
        d = np.array([1.0, -1.0] * 50)  # mean == 0 exactly
        data = np.column_stack((d, d + 5))
        res = calculate_results(data)
        var_ch1 = np.var(d, ddof=1)
        # safe_mean == 1.0 → y_bar_d == var / 1.0 == var
        assert res["var"]["y_bar_d"][0] == pytest.approx(var_ch1, rel=1e-9)


# ---------------------------------------------------------------------------
# 2. "var_cov" block – covariance-based statistics (interleaved split)
# ---------------------------------------------------------------------------


class TestVarCovBlock:
    """Tests for result["var_cov"] using the interleaved (even/odd) split."""

    @pytest.fixture
    def corr_data(self):
        """Data with known cross-covariance between the two sub-series x, y."""
        np.random.seed(13)
        n = 300
        ch0 = np.random.normal(loc=8.0, scale=2.0, size=n)
        ch1 = np.random.normal(loc=4.0, scale=1.0, size=n)
        return _make_data(ch0, ch1)

    def test_mean_ch1(self, corr_data):
        """Mean in var_cov is computed on the *y* sub-series (odd indices)."""
        res = calculate_results(corr_data)
        y, _ = _interleaved_xy(corr_data[:, 0])
        expected = np.mean(y)
        assert res["var_cov"]["Mean"][0] == pytest.approx(expected, rel=1e-9)

    def test_mean_ch2(self, corr_data):
        """Mean in var_cov for ch2 is computed on the *x* sub-series (even indices)."""
        res = calculate_results(corr_data)
        _, x = _interleaved_xy(corr_data[:, 1])
        expected = np.mean(x)
        assert res["var_cov"]["Mean"][2] == pytest.approx(expected, rel=1e-9)

    def test_variance_ch1(self, corr_data):
        res = calculate_results(corr_data)
        y, _ = _interleaved_xy(corr_data[:, 0])
        expected = np.var(y, ddof=1)
        assert res["var_cov"]["Variance"][0] == pytest.approx(expected, rel=1e-9)

    def test_covariance_ch1(self, corr_data):
        """Covariance is np.cov(x, y, ddof=1)[0, 1] on the interleaved pair."""
        res = calculate_results(corr_data)
        y, x = _interleaved_xy(corr_data[:, 0])
        expected_cov = np.cov(x, y, ddof=1)[0, 1]
        assert res["var_cov"]["Covariance"][0] == pytest.approx(expected_cov, rel=1e-9)

    def test_y_bar_d_ch1(self, corr_data):
        """y_bar_d = (var - cov) / mean."""
        res = calculate_results(corr_data)
        y, x = _interleaved_xy(corr_data[:, 0])
        mean = np.mean(y)
        var = np.var(y, ddof=1)
        cov = np.cov(x, y, ddof=1)[0, 1]
        expected = (var - cov) / mean
        assert res["var_cov"]["y_bar_d"][0] == pytest.approx(expected, rel=1e-9)

    def test_cov_mean_sq_ch1(self, corr_data):
        """cov_mean_sq = cov / mean^2."""
        res = calculate_results(corr_data)
        y, x = _interleaved_xy(corr_data[:, 0])
        mean = np.mean(y)
        cov = np.cov(x, y, ddof=1)[0, 1]
        expected = cov / (mean**2)
        assert res["var_cov"]["cov_mean_sq"][0] == pytest.approx(expected, rel=1e-9)

    def test_uncertainties_are_nonnegative(self, corr_data):
        """Bootstrap uncertainties (odd indices) must be non-negative."""
        res = calculate_results(corr_data)
        for metric in ["Mean", "Variance", "Covariance", "y_bar_d", "cov_mean_sq"]:
            assert res["var_cov"][metric][1] >= 0, f"var_cov[{metric}][1] < 0 for ch1"
            assert res["var_cov"][metric][3] >= 0, f"var_cov[{metric}][3] < 0 for ch2"

    def test_uncertainties_plausible_magnitude(self, corr_data):
        """Bootstrap std of the mean should be within 2x the analytical estimate."""
        res = calculate_results(corr_data)
        y, _ = _interleaved_xy(corr_data[:, 0])
        analytical_unc_mean = np.sqrt(np.var(y, ddof=1) / len(y))
        boot_unc_mean = res["var_cov"]["Mean"][1]
        assert boot_unc_mean == pytest.approx(analytical_unc_mean, rel=0.5)


# ---------------------------------------------------------------------------
# 3. "var_autocov" block – autocovariance-based statistics
# ---------------------------------------------------------------------------


def _lag1_autocov(series: np.ndarray) -> float:
    """Lag-1 autocovariance with denominator (n - 1), matching batch_lagged_auto_cov."""
    s = series.reshape(1, -1)
    n = s.shape[1]
    means = np.mean(s, axis=1, keepdims=True)
    centered = s - means
    result = np.einsum("ij,ij->i", centered[:, : n - 1], centered[:, 1:]) / (n - 1)
    return float(result[0])


class TestVarAutoCovBlock:
    """Tests for result["var_autocov"]."""

    @pytest.fixture
    def ar1_data(self):
        """AR(1) process – has non-zero lag-1 autocovariance."""
        np.random.seed(99)
        n = 400
        phi = 0.6
        sigma = 1.0
        eps = np.random.normal(0, sigma, n)
        x = np.zeros(n)
        x[0] = eps[0]
        for t in range(1, n):
            x[t] = phi * x[t - 1] + eps[t]
        x += 5.0  # shift so mean != 0
        ch1 = x
        ch2 = np.random.normal(loc=3.0, scale=0.5, size=n)
        return _make_data(ch1, ch2)

    def test_autocov_value_ch1(self, ar1_data):
        """Auto-Covariance should match the batch_lagged_auto_cov formula on full channel."""
        res = calculate_results(ar1_data)
        d = ar1_data[:, 0]
        expected_autocov = _lag1_autocov(d)
        assert res["var_autocov"]["AutoCorrelation"][0] == pytest.approx(
            expected_autocov, rel=1e-9
        )

    def test_autocov_value_ch2(self, ar1_data):
        res = calculate_results(ar1_data)
        d = ar1_data[:, 1]
        expected_autocov = _lag1_autocov(d)
        assert res["var_autocov"]["AutoCorrelation"][2] == pytest.approx(
            expected_autocov, rel=1e-9
        )

    def test_mean_ch1(self, ar1_data):
        res = calculate_results(ar1_data)
        expected_mean = np.mean(ar1_data[:, 0])
        assert res["var_autocov"]["Mean"][0] == pytest.approx(expected_mean, rel=1e-9)

    def test_variance_ch1(self, ar1_data):
        res = calculate_results(ar1_data)
        expected_var = np.var(ar1_data[:, 0], ddof=1)
        assert res["var_autocov"]["Variance"][0] == pytest.approx(
            expected_var, rel=1e-9
        )

    def test_y_bar_d_ch1(self, ar1_data):
        """y_bar_d = (var - auto_cov) / mean."""
        res = calculate_results(ar1_data)
        d = ar1_data[:, 0]
        mean = np.mean(d)
        var = np.var(d, ddof=1)
        autocov = _lag1_autocov(d)
        expected_yd = (var - autocov) / mean
        assert res["var_autocov"]["y_bar_d"][0] == pytest.approx(expected_yd, rel=1e-9)

    def test_autocov_mean_sq_ch1(self, ar1_data):
        """autocov_mean_sq = auto_cov / mean^2."""
        res = calculate_results(ar1_data)
        d = ar1_data[:, 0]
        mean = np.mean(d)
        autocov = _lag1_autocov(d)
        expected = autocov / (mean**2)
        assert res["var_autocov"]["autocov_mean_sq"][0] == pytest.approx(
            expected, rel=1e-9
        )

    def test_uncertainties_nonnegative(self, ar1_data):
        res = calculate_results(ar1_data)
        for metric in [
            "Mean",
            "Variance",
            "AutoCorrelation",
            "y_bar_d",
            "autocov_mean_sq",
        ]:
            assert res["var_autocov"][metric][1] >= 0
            assert res["var_autocov"][metric][3] >= 0

    def test_iid_autocov_near_zero(self):
        """For truly i.i.d. data the lag-1 autocov should be much smaller than the variance."""
        np.random.seed(42)
        d = np.random.normal(loc=10, scale=2, size=500)
        data = _make_data(d, d + 3)
        res = calculate_results(data)
        # |autocov| << variance for i.i.d. data
        assert (
            abs(res["var_autocov"]["AutoCorrelation"][0])
            < res["var_autocov"]["Variance"][0]
        )


# ---------------------------------------------------------------------------
# 4. "var_doublecov" block – cross-channel covariance
# ---------------------------------------------------------------------------


class TestVarDoubleCovBlock:
    """Tests for result["var_doublecov"] (cov_both_ch=True)."""

    @pytest.fixture
    def cross_data(self):
        np.random.seed(55)
        n = 200
        common = np.random.normal(0, 1, n)
        ch0 = common * 2 + np.random.normal(loc=5.0, scale=0.5, size=n)
        ch1 = common * 1.5 + np.random.normal(loc=3.0, scale=0.5, size=n)
        return _make_data(ch0, ch1)

    def _dual_channel_xy(self, data):
        """Reproduce get_ch_matrix(both_ch_cov=True)."""
        y = data[:, 0]
        x = data[:, 1]
        length = min(len(x), len(y))
        y, x = y[:length], x[:length]
        valid = ~np.isnan(x) & ~np.isnan(y)
        return y[valid], x[valid]

    def test_covariance_ch1(self, cross_data):
        """Covariance = np.cov(x, y, ddof=1)[0,1] on the dual-channel filtered data."""
        res = calculate_results(cross_data, cov_both_ch=True)
        y, x = self._dual_channel_xy(cross_data)
        # idx=0 → data_matrix_cov = y; covariance = np.cov(x, y)[0,1]
        expected_cov = np.cov(x, y, ddof=1)[0, 1]
        assert res["var_doublecov"]["Covariance"][0] == pytest.approx(
            expected_cov, rel=1e-9
        )

    def test_mean_ch1(self, cross_data):
        res = calculate_results(cross_data, cov_both_ch=True)
        y, _ = self._dual_channel_xy(cross_data)
        expected = np.mean(y)
        assert res["var_doublecov"]["Mean"][0] == pytest.approx(expected, rel=1e-9)

    def test_variance_ch1(self, cross_data):
        res = calculate_results(cross_data, cov_both_ch=True)
        y, _ = self._dual_channel_xy(cross_data)
        expected = np.var(y, ddof=1)
        assert res["var_doublecov"]["Variance"][0] == pytest.approx(expected, rel=1e-9)

    def test_y_bar_d_ch1(self, cross_data):
        """y_bar_d = (var - cov) / mean using dual-channel x, y."""
        res = calculate_results(cross_data, cov_both_ch=True)
        y, x = self._dual_channel_xy(cross_data)
        mean = np.mean(y)
        var = np.var(y, ddof=1)
        cov = np.cov(x, y, ddof=1)[0, 1]
        expected = (var - cov) / mean
        assert res["var_doublecov"]["y_bar_d"][0] == pytest.approx(expected, rel=1e-9)

    def test_uncertainties_nonnegative(self, cross_data):
        res = calculate_results(cross_data, cov_both_ch=True)
        for metric in ["Mean", "Variance", "Covariance", "y_bar_d", "cov_mean_sq"]:
            assert res["var_doublecov"][metric][1] >= 0
            assert res["var_doublecov"][metric][3] >= 0


# ---------------------------------------------------------------------------
# 5. Custom functions
# ---------------------------------------------------------------------------


class TestChannelDisabling:
    """Verify that disabled channels are fully zeroed in all sub-dicts."""

    @pytest.fixture
    def data(self):
        np.random.seed(11)
        d0 = np.random.normal(10, 2, 100)
        d1 = np.random.normal(5, 1, 100)
        return _make_data(d0, d1)

    def _assert_channel2_zero(self, res):
        for key in ["var", "var_cov", "var_autocov"]:
            assert res[key]["N"][2] == 0, f"{key}['N'][2] not zero"
            assert res[key]["Mean"][2] == 0, f"{key}['Mean'][2] not zero"
            assert res[key]["Variance"][2] == 0, f"{key}['Variance'][2] not zero"
            assert res[key]["y_bar_d"][2] == 0, f"{key}['y_bar_d'][2] not zero"
            assert res[key]["Mean"][3] == 0, f"{key}['Mean'][3] not zero"

    def _assert_channel1_zero(self, res):
        for key in ["var", "var_cov", "var_autocov"]:
            assert res[key]["N"][0] == 0, f"{key}['N'][0] not zero"
            assert res[key]["Mean"][0] == 0, f"{key}['Mean'][0] not zero"
            assert res[key]["Variance"][0] == 0, f"{key}['Variance'][0] not zero"
            assert res[key]["y_bar_d"][0] == 0, f"{key}['y_bar_d'][0] not zero"
            assert res[key]["Mean"][1] == 0, f"{key}['Mean'][1] not zero"

    def test_ch2_disabled(self, data):
        res = calculate_results(data, ch1=True, ch2=False)
        self._assert_channel2_zero(res)

    def test_ch1_disabled(self, data):
        res = calculate_results(data, ch1=False, ch2=True)
        self._assert_channel1_zero(res)

    def test_both_disabled(self, data):
        res = calculate_results(data, ch1=False, ch2=False)
        self._assert_channel1_zero(res)
        self._assert_channel2_zero(res)

    def test_ch1_active_values_correct_when_ch2_disabled(self, data):
        """Active channel values must still be correct when the other is off."""
        res = calculate_results(data, ch1=True, ch2=False)
        expected_mean = np.mean(data[:, 0])
        expected_var = np.var(data[:, 0], ddof=1)
        assert res["var"]["Mean"][0] == pytest.approx(expected_mean, rel=1e-9)
        assert res["var"]["Variance"][0] == pytest.approx(expected_var, rel=1e-9)


# ---------------------------------------------------------------------------
# 7. NaN handling – correct statistics on NaN-filtered data
# ---------------------------------------------------------------------------


class TestNaNHandling:
    """After NaN removal, statistics should match NumPy on the clean data."""

    def test_mean_after_nan_removal(self):
        np.random.seed(20)
        d = np.random.normal(loc=8, scale=2, size=200).astype(float)
        d[[5, 30, 100]] = np.nan
        data = _make_data(d, np.random.normal(4, 1, 200))

        res = calculate_results(data)
        clean = d[~np.isnan(d)]
        assert res["var"]["Mean"][0] == pytest.approx(np.mean(clean), rel=1e-9)

    def test_variance_after_nan_removal(self):
        np.random.seed(21)
        d = np.random.normal(loc=8, scale=2, size=200).astype(float)
        d[[0, 50, 150]] = np.nan
        data = _make_data(d, np.random.normal(4, 1, 200))

        res = calculate_results(data)
        clean = d[~np.isnan(d)]
        assert res["var"]["Variance"][0] == pytest.approx(
            np.var(clean, ddof=1), rel=1e-9
        )

    def test_n_after_nan_removal(self):
        np.random.seed(22)
        d = np.random.normal(10, 2, 200).astype(float)
        nan_indices = [1, 2, 3, 4]
        d[nan_indices] = np.nan
        data = _make_data(d, np.random.normal(4, 1, 200))

        res = calculate_results(data)
        assert res["var"]["N"][0] == 200 - len(nan_indices)

    def test_no_nans_in_output(self):
        """The function should never produce NaN values in the result dict."""
        np.random.seed(23)
        d0 = np.random.normal(10, 2, 100).astype(float)
        d1 = np.random.normal(5, 1, 100).astype(float)
        d0[[10, 20]] = np.nan
        d1[[30]] = np.nan
        data = _make_data(d0, d1)

        res = calculate_results(data)
        for top_key, sub in res.items():
            if top_key == "custom_funcs":
                continue
            for metric, vals in sub.items():
                for i, v in enumerate(vals):
                    assert not np.isnan(v), (
                        f"NaN in result[{top_key!r}][{metric!r}][{i}]"
                    )


# ---------------------------------------------------------------------------
# 8. Determinism & reproducibility
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Two runs with the same seed should return identical results."""

    def test_same_results_same_seed(self):
        data = RNG.normal(loc=10, scale=2, size=(100, 2))
        np.random.seed(0)
        res1 = calculate_results(data)
        np.random.seed(0)
        res2 = calculate_results(data)

        for top_key, sub in res1.items():
            if top_key == "custom_funcs":
                continue
            for metric, vals in sub.items():
                assert vals == res2[top_key][metric], (
                    f"Mismatch at result[{top_key!r}][{metric!r}]"
                )


# ---------------------------------------------------------------------------
# 9. Consistency across blocks
# ---------------------------------------------------------------------------


class TestCrossBlockConsistency:
    """
    var_cov operates on the interleaved sub-series (y = odd, x = even),
    while var_autocov operates on the full NaN-filtered channel.
    These are different series, so their statistics will differ.

    This class verifies that each block's nominal Mean and Variance
    correctly match their respective input data series independently.
    """

    @pytest.fixture
    def data(self):
        np.random.seed(77)
        ch0 = np.random.normal(loc=9, scale=2, size=200)
        ch1 = np.random.normal(loc=4, scale=1, size=200)
        return _make_data(ch0, ch1)

    def test_var_cov_mean_ch1_matches_odd_subseries(self, data):
        """var_cov uses the odd-indexed sub-series (y) for ch1."""
        res = calculate_results(data)
        y, _ = _interleaved_xy(data[:, 0])
        assert res["var_cov"]["Mean"][0] == pytest.approx(np.mean(y), rel=1e-9)

    def test_var_autocov_mean_ch1_matches_full_channel(self, data):
        """var_autocov uses the full NaN-filtered channel for ch1."""
        res = calculate_results(data)
        full = data[:, 0][~np.isnan(data[:, 0])]
        assert res["var_autocov"]["Mean"][0] == pytest.approx(np.mean(full), rel=1e-9)

    def test_var_cov_and_var_autocov_means_differ_for_ch1(self, data):
        """Confirms that the two blocks purposely operate on different sub-series."""
        res = calculate_results(data)
        # They should differ because one is odd-indexed, the other is the full channel
        assert res["var_cov"]["Mean"][0] != pytest.approx(
            res["var_autocov"]["Mean"][0], rel=1e-3
        )

    def test_var_cov_variance_ch1_matches_odd_subseries(self, data):
        res = calculate_results(data)
        y, _ = _interleaved_xy(data[:, 0])
        assert res["var_cov"]["Variance"][0] == pytest.approx(
            np.var(y, ddof=1), rel=1e-9
        )

    def test_var_autocov_variance_ch1_matches_full_channel(self, data):
        res = calculate_results(data)
        full = data[:, 0][~np.isnan(data[:, 0])]
        assert res["var_autocov"]["Variance"][0] == pytest.approx(
            np.var(full, ddof=1), rel=1e-9
        )

    def test_var_cov_mean_ch2_matches_even_subseries(self, data):
        """var_cov uses the even-indexed sub-series (x) for ch2."""
        res = calculate_results(data)
        _, x = _interleaved_xy(data[:, 1])
        assert res["var_cov"]["Mean"][2] == pytest.approx(np.mean(x), rel=1e-9)

    def test_var_autocov_mean_ch2_matches_full_channel(self, data):
        res = calculate_results(data)
        full = data[:, 1][~np.isnan(data[:, 1])]
        assert res["var_autocov"]["Mean"][2] == pytest.approx(np.mean(full), rel=1e-9)


# ---------------------------------------------------------------------------
# 10. Output structure, edge cases & error handling  (migrated from test_calc_stat.py)
# ---------------------------------------------------------------------------


class TestOutputStructure:
    """Verify the complete shape/key structure of the returned dictionary."""

    def test_top_level_keys(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 100),
                np.random.normal(5, 1, 100)
        )
        )
        result = calculate_results(data)

        assert "var" in result
        assert "var_cov" in result
        assert "var_autocov" in result
        assert "var_doublecov" in result

    def test_var_sub_keys_and_list_length(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 100),
                np.random.normal(5, 1, 100)
        )
        )
        result = calculate_results(data)

        for key in ["N", "Mean", "Variance", "y_bar_d"]:
            assert key in result["var"], f"'var' missing key '{key}'"
            assert len(result["var"][key]) == 4, (
                f"result['var']['{key}'] should have 4 entries"
            )

    def test_both_channels_populated(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 100),
                np.random.normal(5, 1, 100)
        )
        )
        result = calculate_results(data)

        assert result["var"]["N"][0] > 0
        assert result["var"]["N"][2] > 0
        assert result["var"]["Mean"][0] != 0
        assert result["var"]["Mean"][2] != 0


class TestCovBothChannelsGuard:
    """cov_both_ch=True with a missing channel must raise a clear exception."""

    def test_raises_when_ch1_disabled(self):
        data = np.column_stack(
            (
                np.random.normal(10, 2, 50),
                np.random.normal(5, 1, 50)
        )
        )
        with pytest.raises(
            Exception,
            match="Can't calculate covariance between the two channels if one is not used"
        ):
            calculate_results(data, ch1=False, cov_both_ch=True)

    def test_raises_when_ch2_disabled(self):
        data = np.column_stack(
            (
                np.random.normal(10, 2, 50),
                np.random.normal(5, 1, 50)
        )
        )
        with pytest.raises(Exception):
            calculate_results(data, ch2=False, cov_both_ch=True)

    def test_var_doublecov_populated_when_both_enabled(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 100),
                np.random.normal(5, 1, 100)
        )
        )
        res = calculate_results(data, cov_both_ch=True)
        assert res["var_doublecov"]["N"][0] > 0


class TestEdgeCases:
    """Single-row data and constant data should not crash and return sensible zeros."""

    def test_single_row_variance_is_zero(self):
        """n=1 → variance undefined → falls back to 0."""
        data = np.array([[10.0, 5.0]])
        res = calculate_results(data)
        # get_ch_matrix gives y=[], x=[] for n=1, so interleaved blocks have n<=1
        assert res["var"]["Variance"][0] == 0
        assert res["var"]["Variance"][2] == 0

    def test_constant_data_variance_is_zero(self):
        """All-constant channel → zero variance."""
        data = np.ones((50, 2)) * 5.0
        res = calculate_results(data)
        assert res["var"]["Variance"][0] == 0
        assert res["var"]["Variance"][2] == 0

    def test_constant_data_bootstrap_zeroed(self):
        """np.ptp == 0 triggers early exit before bootstrap; cov blocks must be zero."""
        data = np.ones((50, 2)) * 5.0
        res = calculate_results(data)
        assert res["var_cov"]["Variance"][0] == 0
        assert res["var_autocov"]["AutoCorrelation"][0] == 0

    def test_empty_data_all_nans(self):
        """All-NaN input: every entry in the result must be zero (no crash)."""
        data = np.full((10, 2), np.nan)
        res = calculate_results(data, cov_both_ch=True)

        assert res["var"]["N"][0] == 0
        assert res["var"]["Variance"][0] == 0.0
        assert res["var_cov"]["N"][0] == 0
        assert res["var_cov"]["Covariance"][0] == 0.0
        assert res["var_doublecov"]["N"][0] == 0
        assert res["var_doublecov"]["Covariance"][0] == 0.0


class TestBootstrapErrorHandling:
    """When optimal_block_length raises, uncertainties must fall back to zero."""

    def test_monkeypatched_block_length_zeros_uncertainties(self, monkeypatch):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 100),
                np.random.normal(5, 1, 100)
        )
        )

        import core.calc_stat as calc_stat  # the same module calculate_results lives in

        def mock_optimal_block_length(*args, **kwargs):
            raise ValueError("Mock error")

        monkeypatch.setattr(
            calc_stat, "optimal_block_length", mock_optimal_block_length
        )

        res = calculate_results(data)

        # Uncertainty slots (odd indices) should be zeroed on error
        assert res["var_cov"]["Covariance"][1] == 0.0
        assert res["var_autocov"]["AutoCorrelation"][1] == 0.0

