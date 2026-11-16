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
from core.calc_stat import (
    calculate_fft_block,
    calculate_results,
    compute_spectrum,
    find_spectral_peaks,
)

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


class TestCovVarRatio:
    """cov_var_ratio = cov/var (γ(1)/var under var_autocov), with
    block-bootstrap uncertainties; the plain variance method has none."""

    def _ar1(self, seed, n=400, phi=0.6):
        np.random.seed(seed)
        eps = np.random.normal(0, 1.0, n)
        x = np.zeros(n)
        x[0] = eps[0]
        for t in range(1, n):
            x[t] = phi * x[t - 1] + eps[t]
        return x + 5.0

    def test_var_method_has_no_ratio_key(self):
        np.random.seed(21)
        data = _make_data(
            np.random.normal(10, 2, 100), np.random.normal(5, 1, 100)
        )
        res = calculate_results(data, calc_bootstrap=False)
        assert "cov_var_ratio" not in res["var"]
        # the ratio keys stay within their own method families
        assert "cov_var_ratio" not in res["var_autocov"]
        assert "autocov_var_ratio" not in res["var_cov"]
        assert "autocov_var_ratio" not in res["var_fft"]

    def test_var_cov_ratio_is_cov_over_var_with_bootstrap_unc(self):
        data = _make_data(self._ar1(22), np.random.normal(3, 0.5, 400))
        res = calculate_results(data)
        expected = (
            res["var_cov"]["Covariance"][0] / res["var_cov"]["Variance"][0]
        )
        assert res["var_cov"]["cov_var_ratio"][0] == pytest.approx(expected)
        # AR(1): cov(even, odd) ≈ γ(1) and var ≈ γ(0) → ratio ≈ phi
        assert res["var_cov"]["cov_var_ratio"][0] == pytest.approx(0.6, abs=0.15)
        assert res["var_cov"]["cov_var_ratio"][1] > 0.0

    def test_var_autocov_ratio_is_gamma_over_var(self):
        data = _make_data(self._ar1(23), np.random.normal(3, 0.5, 400))
        res = calculate_results(data)
        expected = (
            res["var_autocov"]["AutoCorrelation"][0]
            / res["var_autocov"]["Variance"][0]
        )
        assert res["var_autocov"]["autocov_var_ratio"][0] == pytest.approx(expected)
        # for AR(1), γ(1)/γ(0) = phi exactly
        assert res["var_autocov"]["autocov_var_ratio"][0] == pytest.approx(
            0.6, abs=0.15
        )
        assert res["var_autocov"]["autocov_var_ratio"][1] > 0.0

    def test_var_doublecov_ratio_is_cross_cov_over_own_var(self):
        np.random.seed(24)
        n = 300
        common = np.random.normal(0, 1, n)
        ch0 = common * 2 + np.random.normal(5.0, 0.5, n)
        ch1 = common * 1.5 + np.random.normal(3.0, 0.5, n)
        res = calculate_results(_make_data(ch0, ch1), cov_both_ch=True)
        for slots in (0, 2):
            ratio = res["var_doublecov"]["cov_var_ratio"][slots]
            expected = (
                res["var_doublecov"]["Covariance"][slots]
                / res["var_doublecov"]["Variance"][slots]
            )
            assert ratio == pytest.approx(expected)
            assert ratio > 0.0
        # each channel slot divides by its OWN variance (Cauchy-Schwarz only
        # bounds the cross-cov by sqrt(var_ch1·var_ch2), so the ch2 ratio
        # of this construction legitimately exceeds 1)
        assert res["var_doublecov"]["cov_var_ratio"][0] < 1.0
        assert res["var_doublecov"]["cov_var_ratio"][2] > 1.0
        assert res["var_doublecov"]["cov_var_ratio"][1] > 0.0

    def test_bootstrap_disabled_ratio_unc_zero(self):
        data = _make_data(self._ar1(25), np.random.normal(3, 0.5, 400))
        res = calculate_results(data, calc_bootstrap=False)
        assert res["var_cov"]["cov_var_ratio"][1] == 0.0
        assert res["var_autocov"]["autocov_var_ratio"][1] == 0.0
        # the values themselves are still computed
        assert res["var_cov"]["cov_var_ratio"][0] != 0.0

    def test_disabled_channel_ratio_zeroed(self):
        np.random.seed(26)
        data = _make_data(
            np.random.normal(10, 2, 100), np.random.normal(5, 1, 100)
        )
        res = calculate_results(data, ch2=False, cov_both_ch=True)
        assert res["var_cov"]["cov_var_ratio"][2:] == [0, 0]
        assert res["var_doublecov"]["cov_var_ratio"] == [0, 0, 0, 0]
        assert res["var_autocov"]["autocov_var_ratio"][2:] == [0, 0]


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
    """cov_both_ch=True with a missing channel silently zeroes var_doublecov."""

    @staticmethod
    def _assert_var_doublecov_all_zero(res):
        for key, values in res["var_doublecov"].items():
            assert values == [0, 0, 0, 0], f"var_doublecov['{key}'] not zeroed"

    def test_var_doublecov_zeroed_when_ch1_disabled(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 50),
                np.random.normal(5, 1, 50)
        )
        )
        res = calculate_results(data, ch1=False, cov_both_ch=True)

        self._assert_var_doublecov_all_zero(res)
        # the enabled channel is still processed by the single-channel methods
        assert res["var"]["N"][0] == 0
        assert res["var"]["N"][2] == 50
        assert res["var"]["Mean"][2] != 0
        assert res["var_cov"]["N"][2] > 0

    def test_var_doublecov_zeroed_when_ch2_disabled(self):
        np.random.seed(42)
        data = np.column_stack(
            (
                np.random.normal(10, 2, 50),
                np.random.normal(5, 1, 50)
        )
        )
        res = calculate_results(data, ch2=False, cov_both_ch=True)

        self._assert_var_doublecov_all_zero(res)
        assert res["var"]["N"][2] == 0
        assert res["var"]["N"][0] == 50
        assert res["var"]["Mean"][0] != 0
        assert res["var_cov"]["N"][0] > 0

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



# ---------------------------------------------------------------------------
# var_fft — frequency-domain variance–covariance estimator
# ---------------------------------------------------------------------------

FS = 1000.0        # Hz
N_FFT = 100_000    # samples


def _synth_from_psd(level, rng, lines=(), offset=0.0, fs=FS, n=N_FFT):
    """Build a time series with a prescribed one-sided PSD.

    level : callable f(Hz) -> PSD level (units^2/Hz) for the broadband part
    lines : iterable of (frequency_hz, variance) discrete components
    """
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    amp = 0.5 * np.sqrt(level(freqs) * fs * n)
    Z = (rng.standard_normal(freqs.size) + 1j * rng.standard_normal(freqs.size)) * amp
    Z[0] = 0.0
    if n % 2 == 0:
        Z[-1] = 0.0
    for f0, var in lines:
        k = int(round(f0 * n / fs))
        Z[k] += n * np.sqrt(var / 2.0) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    y = np.fft.irfft(Z, n)
    return y - y.mean() + offset


class TestVarFftDefaults:
    """calc_fft disabled (default) or unusable -> the section stays zeroed."""

    def test_disabled_by_default_all_zero(self):
        np.random.seed(1)
        data = np.column_stack(
            (np.random.normal(50, 2, 500), np.random.normal(50, 2, 500))
        )
        res = calculate_results(data, calc_bootstrap=False)
        for key in ("N", "Mean", "Variance", "Covariance", "y_bar_d"):
            assert res["var_fft"][key] == [0, 0, 0, 0], f"var_fft['{key}'] not zero"

    def test_enabled_without_sample_freq_zeroed(self):
        np.random.seed(1)
        data = np.column_stack(
            (np.random.normal(50, 2, 500), np.random.normal(50, 2, 500))
        )
        res = calculate_results(data, calc_bootstrap=False, calc_fft=True)
        assert res["var_fft"]["N"] == [0, 0, 0, 0]

    def test_empty_band_zeroed(self):
        rng = np.random.default_rng(2)
        y = _synth_from_psd(lambda f: 0.008, rng, offset=50.0)
        data = np.column_stack((y, y))
        # band entirely above Nyquist -> no kept bin -> not computed
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(600.0, 700.0)],
        )
        assert res["var_fft"]["N"] == [0, 0, 0, 0]


class TestVarFftWhiteNoise:
    """Pure white noise: Parseval-exact variance, cov ~ 0, y_bar_d = white/mean."""

    WHITE_VAR = 4.0
    LVL = WHITE_VAR / (FS / 2)   # 0.008 units^2/Hz

    def _run(self, **kwargs):
        rng = np.random.default_rng(3)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        data = np.column_stack((y, y))
        return calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS, **kwargs
        ), y

    def test_parseval_variance_matches_time_domain(self):
        res, y = self._run()
        assert res["var_fft"]["Variance"][0] == pytest.approx(
            np.var(y, ddof=0), rel=1e-8
        )

    def test_covariance_near_zero(self):
        res, _ = self._run()
        assert res["var_fft"]["Covariance"][0] == pytest.approx(0.0, abs=0.2)

    def test_y_bar_d_is_white_over_mean(self):
        res, _ = self._run()
        assert res["var_fft"]["Mean"][0] == pytest.approx(50.0, abs=0.1)
        assert res["var_fft"]["y_bar_d"][0] == pytest.approx(
            self.WHITE_VAR / 50.0, rel=0.05
        )

    def test_n_and_uncertainty_slots(self):
        res, _ = self._run()
        assert res["var_fft"]["N"][0] == N_FFT
        assert res["var_fft"]["N"][2] == N_FFT
        # M: the number of kept floor bins (full band kept -> every AC bin,
        # Nyquist included), archived next to N with no uncertainty
        assert res["var_fft"]["M"][0] == N_FFT // 2
        assert res["var_fft"]["M"][2] == N_FFT // 2
        assert res["var_fft"]["M"][1] == 0
        assert res["var_fft"]["M"][3] == 0
        # bootstrap disabled: mean/var/cov uncertainties stay 0 ...
        for key in ("N", "M", "Mean", "Variance", "Covariance"):
            assert res["var_fft"][key][1] == 0
            assert res["var_fft"][key][3] == 0
        # ... while the analytical sqrt(1/M) y_bar_d uncertainty is present
        # (nothing excluded: M = N/2 -> sqrt(2/N), the variance-method value)
        expected = res["var_fft"]["y_bar_d"][0] * np.sqrt(2.0 / N_FFT)
        assert res["var_fft"]["y_bar_d"][1] == pytest.approx(expected)
        assert res["var_fft"]["y_bar_d"][3] == pytest.approx(expected)

    def test_nan_samples_dropped(self):
        rng = np.random.default_rng(4)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        y[50_000:53_000] = np.nan
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS
        )
        assert res["var_fft"]["N"][0] == N_FFT - 3000
        assert res["var_fft"]["y_bar_d"][0] * 50.0 == pytest.approx(
            self.WHITE_VAR, rel=0.05
        )


class TestVarFftBandExclusion:
    """Floor L below 100 Hz, 3L plateau above, 0.5-variance line at 250 Hz.

    true: white = 4.0, plateau excess = 6.4, line = 0.5, total = 10.9.
    The band is used ONLY for the floor: variance/covariance stay global.
    """

    LVL = 0.008

    def _run(self, **kwargs):
        rng = np.random.default_rng(5)
        y = _synth_from_psd(
            lambda f: np.where(f < 100.0, self.LVL, 3 * self.LVL),
            rng,
            lines=[(250.0, 0.5)],
            offset=50.0,
        )
        data = np.column_stack((y, y))
        return calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS, **kwargs
        ), y

    def test_band_exclusion_recovers_white_and_cov(self):
        res, y = self._run(fft_bands=[(0.0, 100.0)])
        white = res["var_fft"]["y_bar_d"][0] * 50.0
        assert white == pytest.approx(4.0, rel=0.05)
        assert res["var_fft"]["Variance"][0] == pytest.approx(np.var(y, ddof=0), rel=1e-8)
        assert res["var_fft"]["Covariance"][0] == pytest.approx(6.9, abs=0.5)

    def test_threshold_clipped_floor(self):
        # clip at 8x the floor level: protects L if a peak lurks inside the band
        res, _ = self._run(fft_bands=[(0.0, 100.0)], fft_threshold=8 * self.LVL)
        white = res["var_fft"]["y_bar_d"][0] * 50.0
        assert white == pytest.approx(4.0, rel=0.02)

    def test_no_band_overestimates_white(self):
        # the plateau is below any sensible threshold: without band exclusion
        # the floor absorbs it and white is heavily overestimated (documents
        # why fft_bands exists)
        res, _ = self._run()
        white = res["var_fft"]["y_bar_d"][0] * 50.0
        assert white > 2 * 4.0


class TestVarFftOptions:
    """Welch / Hann / per-channel behaviour of the FFT estimator."""

    LVL = 0.008  # white var 4.0

    def test_welch_recovers_white(self):
        rng = np.random.default_rng(6)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_method="welch", fft_nperseg=8192,
        )
        assert res["var_fft"]["y_bar_d"][0] * 50.0 == pytest.approx(4.0, rel=0.05)
        assert res["var_fft"]["Variance"][0] == pytest.approx(
            np.var(y, ddof=0), rel=0.02
        )

    def test_hann_window_recovers_white(self):
        rng = np.random.default_rng(7)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_window="hann",
        )
        assert res["var_fft"]["y_bar_d"][0] * 50.0 == pytest.approx(4.0, rel=0.02)
        assert res["var_fft"]["Variance"][0] == pytest.approx(
            np.var(y, ddof=0), rel=0.02
        )

    def test_ch2_disabled_zero_slots(self):
        rng = np.random.default_rng(8)
        y1 = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        y2 = _synth_from_psd(lambda f: self.LVL, rng, offset=30.0)
        data = np.column_stack((y1, y2))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS, ch2=False
        )
        assert res["var_fft"]["N"][0] == N_FFT
        assert res["var_fft"]["N"][2] == 0
        assert res["var_fft"]["Variance"][2] == 0
        assert res["var_fft"]["y_bar_d"][2] == 0

    def test_multiple_disjoint_bands(self):
        rng = np.random.default_rng(9)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0), (200.0, 300.0)],
        )
        assert res["var_fft"]["y_bar_d"][0] * 50.0 == pytest.approx(4.0, rel=0.05)


class TestCalculateFftBlock:
    """The standalone block used by the FFT analysis dialog."""

    LVL = 0.008  # white var 4.0 at fs 1000

    def _channel(self, line=None):
        rng = np.random.default_rng(10)
        return _synth_from_psd(
            lambda f: self.LVL, rng, offset=50.0,
            lines=[line] if line else (),
        )

    def test_matches_calculate_results_var_fft(self):
        y = self._channel()
        data = np.column_stack((y, y))
        stats, _ = calculate_fft_block(y, FS, bands=[(0.0, 100.0)])
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0)],
        )
        assert res["var_fft"]["N"][0] == stats["N"]
        assert res["var_fft"]["Variance"][0] == pytest.approx(stats["Variance"])
        assert res["var_fft"]["Covariance"][0] == pytest.approx(stats["Covariance"])
        assert res["var_fft"]["y_bar_d"][0] == pytest.approx(stats["y_bar_d"])

    def test_not_computable_returns_zero_stats_and_none_diagnostics(self):
        stats, diag = calculate_fft_block(np.array([1.0, np.nan]), FS)
        assert stats["N"] == 0 and diag is None
        stats, diag = calculate_fft_block(self._channel(), FS, bands=[(600.0, 700.0)])
        assert stats["N"] == 0 and diag is None

    def test_diagnostics_floor_and_mask(self):
        y = self._channel()
        stats, diag = calculate_fft_block(y, FS, bands=[(0.0, 100.0)])
        assert diag is not None
        assert diag["f_nyquist"] == FS / 2
        assert len(diag["freqs"]) == len(diag["psd"])
        assert diag["floor_level"] == pytest.approx(self.LVL, rel=0.05)
        kept = diag["kept_mask"]
        assert kept[1 : int(100.0 * len(diag["freqs"]) / (FS / 2))].all()
        assert not kept[int(150.0 * len(diag["freqs"]) / (FS / 2)) :].any()

    def test_diagnostics_peaks_above_threshold(self):
        y = self._channel(line=(250.0, 0.5))
        stats, diag = calculate_fft_block(y, FS, threshold=8 * self.LVL)
        assert diag["peaks"], "the 250 Hz line must be detected"
        peak = max(diag["peaks"], key=lambda p: p["variance"])
        assert peak["f_lo"] <= 250.0 <= peak["f_hi"]
        assert peak["variance"] == pytest.approx(0.5, rel=0.3)

    def test_no_threshold_no_peaks(self):
        _, diag = calculate_fft_block(self._channel(), FS)
        assert diag["peaks"] == []

    def test_compute_spectrum_is_the_displayed_and_integrated_spectrum(self):
        y = self._channel()
        for method, nperseg in (("periodogram", 8192), ("welch", 8192)):
            freqs, psd = compute_spectrum(
                y, FS, method=method, window="rect", nperseg=nperseg
            )
            _, diag = calculate_fft_block(
                y, FS, method=method, window="rect", nperseg=nperseg
            )
            assert np.allclose(freqs, diag["freqs"])
            assert np.allclose(psd, diag["psd"])
        _, psd_w = compute_spectrum(y, FS, method="welch", nperseg=8192)
        # nearest segment count keeps the resolution within a few % of the
        # requested fs/nperseg while truncating at most a handful of samples
        requested = 8192 // 2 + 1
        assert abs(len(psd_w) - requested) < requested * 0.05


class TestWelchParseval:
    """Welch must integrate to the same variance as the other estimators.

    Regression: per-segment mean removal discarded the between-segment
    variance (all power below fs/nperseg), so the Welch variance came out
    systematically lower than the time-domain variance on drifting data.
    """

    LVL = 0.008  # white var 4.0

    def _drifting_channel(self):
        rng = np.random.default_rng(11)
        y = _synth_from_psd(lambda f: self.LVL, rng, offset=50.0)
        t = np.arange(len(y)) / FS
        # 0.05 Hz drift: below the Welch resolution fs/8192 ~ 0.122 Hz
        return y + 0.3 * np.sin(2 * np.pi * 0.05 * t)

    def test_welch_variance_matches_time_domain(self):
        y = self._drifting_channel()
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_method="welch", fft_nperseg=8192,
        )
        assert res["var_fft"]["Variance"][0] == pytest.approx(
            np.var(y, ddof=0), rel=1e-4
        )

    def test_periodogram_and_welch_agree_on_variance(self):
        y = self._drifting_channel()
        data = np.column_stack((y, y))
        common = dict(calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS)
        per = calculate_results(data, **common)
        wel = calculate_results(data, fft_method="welch", fft_nperseg=8192, **common)
        assert per["var_fft"]["Variance"][0] == pytest.approx(
            wel["var_fft"]["Variance"][0], rel=0.005
        )

    def test_line_area_conserved_between_estimators(self):
        # a line keeps its integrated variance when the bins widen; with the
        # global-mean Welch there is no per-segment absorption either.
        # The floor MUST come from a line-free band: with the plain global
        # mean the line's power is absorbed by L-hat and cov would be 0 by
        # construction.
        rng = np.random.default_rng(12)
        y = _synth_from_psd(
            lambda f: self.LVL, rng, offset=50.0, lines=[(250.0, 0.5)]
        )
        data = np.column_stack((y, y))
        common = dict(
            calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 200.0)],
        )
        per = calculate_results(data, **common)
        wel = calculate_results(data, fft_method="welch", fft_nperseg=8192, **common)
        assert per["var_fft"]["Covariance"][0] == pytest.approx(0.5, rel=0.1)
        assert wel["var_fft"]["Covariance"][0] == pytest.approx(
            per["var_fft"]["Covariance"][0], rel=0.1
        )


class TestPerChannelThresholds:
    """Channels with different white noise levels need separate thresholds.

    A single line high enough for the noisier channel would sit inside the
    other channel's fuzz (and vice versa), corrupting the clipped-mean floor.
    """

    def test_different_floors_recovered_with_own_thresholds(self):
        rng = np.random.default_rng(13)
        y1 = _synth_from_psd(lambda f: 0.008, rng, offset=50.0)   # white 4.0
        y2 = _synth_from_psd(lambda f: 0.018, rng, offset=30.0)   # white 9.0
        data = np.column_stack((y1, y2))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0)],
            fft_threshold=8 * 0.008,
            fft_threshold_ch2=8 * 0.018,
        )
        white1 = res["var_fft"]["y_bar_d"][0] * 50.0
        white2 = res["var_fft"]["y_bar_d"][2] * 30.0
        assert white1 == pytest.approx(4.0, rel=0.05)
        assert white2 == pytest.approx(9.0, rel=0.05)

    def test_wrong_single_threshold_biases_the_noisier_channel(self):
        # the shared-line failure mode: applying CH1's (low) threshold to the
        # noisier CH2 clips its fuzz and biases its white noise level down
        rng = np.random.default_rng(14)
        y1 = _synth_from_psd(lambda f: 0.008, rng, offset=50.0)
        y2 = _synth_from_psd(lambda f: 0.018, rng, offset=30.0)
        data = np.column_stack((y1, y2))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0)],
            fft_threshold=8 * 0.008,
            fft_threshold_ch2=8 * 0.008,   # CH1's line on CH2: too low
        )
        white2 = res["var_fft"]["y_bar_d"][2] * 30.0
        # expected clipped floor: L*(1-exp(-T/L)) with L=0.018, T=8*0.008
        expected_biased = 0.018 * (1 - np.exp(-8 * 0.008 / 0.018)) * (FS / 2)
        assert white2 == pytest.approx(expected_biased, rel=0.05)
        assert white2 < 8.8 < 9.0


class TestVarFftUncertainties:
    """Uncertainty scheme of the FFT estimator.

    - Mean and Variance: circular block bootstrap (same machinery as the
      other estimators);
    - y_bar_d (and the white variance): chi-square floor statistics,
      sigma = value * sqrt(1/M) with k the kept floor bins — a first
      approximation that ignores threshold clipping and Welch's segment
      averaging;
    - Covariance: unknown, reported as 0.
    """

    N_SMALL = 4000   # small enough that the bootstrap stays fast
    LVL = 0.008      # white var 4.0 at fs 1000

    def _channel(self, seed=20, n=N_SMALL):
        rng = np.random.default_rng(seed)
        return _synth_from_psd(lambda f: self.LVL, rng, offset=50.0, n=n)

    def test_block_uncertainty_relations(self):
        y = self._channel()
        stats, diag = calculate_fft_block(y, FS)
        k = int(np.count_nonzero(diag["kept_mask"]))
        assert stats["M"] == k
        assert stats["Covariance_unc"] == 0.0
        assert stats["y_bar_d_unc"] == pytest.approx(
            stats["y_bar_d"] * np.sqrt(1.0 / k)
        )
        assert stats["Mean_unc"] > 0.0
        assert stats["Variance_unc"] > 0.0

    def test_block_bootstrap_disabled_zeros_mean_var_unc(self):
        y = self._channel()
        stats, _ = calculate_fft_block(y, FS, calc_bootstrap=False)
        assert stats["Mean_unc"] == 0.0
        assert stats["Variance_unc"] == 0.0
        # the sqrt(2/M) part is analytical: still computed
        assert stats["y_bar_d_unc"] == pytest.approx(
            stats["y_bar_d"] * np.sqrt(1.0 / stats["M"])
        )

    def test_k_counts_only_kept_bins(self):
        y = self._channel()
        stats_full, _ = calculate_fft_block(y, FS)
        stats_kept, diag_kept = calculate_fft_block(y, FS, bands=[(0.0, 100.0)])
        k_full = stats_full["M"]
        k_kept = stats_kept["M"]
        assert k_kept == int(np.count_nonzero(diag_kept["kept_mask"]))
        assert k_kept < k_full
        # excluding bins inflates the relative floor uncertainty by
        # exactly sqrt(k_full / k_kept)
        rel_full = stats_full["y_bar_d_unc"] / stats_full["y_bar_d"]
        rel_kept = stats_kept["y_bar_d_unc"] / stats_kept["y_bar_d"]
        assert rel_kept / rel_full == pytest.approx(
            np.sqrt(k_full / k_kept), rel=1e-9
        )

    def test_results_slots_filled_with_bootstrap(self):
        y = self._channel()
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=True, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0)],
        )
        # fft_bands are the KEPT bands: bins with 0 < f <= 100 Hz
        k = int(round(100.0 * self.N_SMALL / FS))
        assert res["var_fft"]["M"][0] == k
        assert res["var_fft"]["M"][2] == k
        assert res["var_fft"]["Covariance"][1] == 0.0
        assert res["var_fft"]["Mean"][1] > 0.0
        assert res["var_fft"]["Variance"][1] > 0.0
        assert res["var_fft"]["y_bar_d"][1] == pytest.approx(
            res["var_fft"]["y_bar_d"][0] * np.sqrt(1.0 / k)
        )
        assert res["var_fft"]["y_bar_d"][3] == pytest.approx(
            res["var_fft"]["y_bar_d"][0] * np.sqrt(1.0 / k)
        )

    def test_bootstrap_uncertainty_magnitude_plausible(self):
        # iid white noise: the bootstrap mean uncertainty must track the
        # analytical sqrt(var/n) within a factor of ~2
        y = self._channel()
        stats, _ = calculate_fft_block(y, FS)
        analytical = np.sqrt(np.var(y, ddof=1) / len(y))
        assert 0.5 * analytical < stats["Mean_unc"] < 2.0 * analytical

    def test_not_computable_zero_uncertainties(self):
        stats, diag = calculate_fft_block(np.array([1.0, np.nan]), FS)
        assert stats["Mean_unc"] == 0.0
        assert stats["Variance_unc"] == 0.0
        assert stats["y_bar_d_unc"] == 0.0
        assert stats["M"] == 0

    def test_fft_cov_var_ratio_nonwhite_fraction_zero_unc(self):
        # white 4.0 + plateau 6.4 + 0.5-variance line: total 10.9, of which
        # 6.9 is non-white -> ratio ~ 0.63; the uncertainty is unknown -> 0
        rng = np.random.default_rng(27)
        y = _synth_from_psd(
            lambda f: np.where(f < 100.0, self.LVL, 3 * self.LVL),
            rng,
            lines=[(250.0, 0.5)],
            offset=50.0,
        )
        data = np.column_stack((y, y))
        res = calculate_results(
            data, calc_bootstrap=False, calc_fft=True, sample_freq_hz=FS,
            fft_bands=[(0.0, 100.0)],
        )
        ratio = res["var_fft"]["cov_var_ratio"]
        assert ratio[0] == pytest.approx(
            res["var_fft"]["Covariance"][0] / res["var_fft"]["Variance"][0]
        )
        assert ratio[0] == pytest.approx(6.9 / 10.9, abs=0.08)
        assert ratio[1] == 0.0
        assert ratio[3] == 0.0
        # not-computable block: the ratio defaults to zero as well
        stats, _ = calculate_fft_block(np.array([1.0, np.nan]), FS)
        assert stats["cov_var_ratio"] == 0.0
        assert stats["cov_var_ratio_unc"] == 0.0


def test_spectral_peaks_gap_merge():
    """Runs separated by one empty bin merge; two empty bins split."""
    freqs = np.arange(0, 10, dtype=float)          # df = 1
    psd = np.zeros(10)
    psd[[2, 4, 7]] = [5.0, 5.0, 5.0]               # 2|3|4 one peak, 7 another
    peaks = find_spectral_peaks(psd, freqs, threshold=1.0)
    assert len(peaks) == 2
    assert peaks[0]["f_lo"] == pytest.approx(1.5)   # bin edges
    assert peaks[0]["f_hi"] == pytest.approx(4.5)
    assert peaks[0]["f_center"] == pytest.approx(3.0)
    assert peaks[1]["f_center"] == pytest.approx(7.0)
    assert peaks[0]["variance"] == pytest.approx((5 - 1) * 2 * 1.0)  # only the 2 above-threshold bins
    assert peaks[1]["f_lo"] == pytest.approx(6.5)
    assert peaks[1]["variance"] == pytest.approx(4.0)
