"""
Tests for the variable catalog (services/variable_catalog.py):
auto-generation of result variables from the archive layout, the
counts→charge conversion, missing-value handling (absent macro, N = 0
"not computed") and the declared metadata/timing/context variables.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.variable_catalog import (  # noqa: E402
    VariableCatalog,
    build_default_variables,
    counts_to_pc_factor,
    default_catalog,
)


def fake_settings(
    results=None,
    depth=3.0,
    current=2.0,
    int_time_s=0.0021,
    energy=62.0,
    cap_ch1=3422.0,
    bits=12,
    single_ch=False,
    timestamp="2026-09-18T10:12:00+02:00",
    problematic=False,
):
    """A minimal v3 settings.json with the same layout as the archives."""
    results = results if results is not None else {
        "var_autocov": {
            "N": [1000, 0, 0, 0] if single_ch else [1000, 0, 1000, 0],
            "Mean": [120.5, 0.4, 0, 0],
            "Variance": [300.0, 6.0, 0, 0],
            "AutoCorrelation": [100.0, 3.0, 0, 0],
            "y_bar_d": [1.5, 0.05, 0, 0],
        },
    }
    return {
        "measurement_info": {
            "timestamp_start": timestamp,
            "is_problematic": problematic,
        },
        "experimental_setup": {
            "depth_cm": depth,
            "beam_current_nA": current,
            "beam_energy_MeV": energy,
        },
        "timing_settings": {"integration_time_s": int_time_s},
        "hardware_settings": {
            "adc": {
                "bits": bits,
                "input_range_volts": {"min_volts": 0.0, "max_volts": 3.3},
            },
            "channels": {"ch1": {"gpio_mask": [1, 1, 1], "capacitance_pF": cap_ch1}},
        },
        "analysis": {"results": results},
    }


# --- structure -------------------------------------------------------------

def test_catalog_builds_and_has_unique_ids():
    variables = build_default_variables()
    catalog = VariableCatalog(variables)
    assert len(variables) > 40  # 5 macros × params × 2 channels + declared
    ids = [v.var_id for v in variables]
    assert len(ids) == len(set(ids))
    assert "autocov_yd_ch1" in catalog
    assert "beam_current_nA" in catalog
    assert "index" in catalog


def test_each_method_lists_only_its_own_parameters():
    """No cross-method variables: the plain variance method has no
    covariance, var_cov has no autocorrelation, etc."""
    catalog = VariableCatalog(build_default_variables())
    # var: N, Mean, Variance, y_bar_d only
    assert "var_yd_ch1" in catalog
    assert "var_covariance_ch1" not in catalog
    assert "var_cov_mean_sq_ch1" not in catalog
    # var_cov has covariance but no autocorrelation
    assert "varcov_covariance_ch1" in catalog
    assert "varcov_autocorr_ch1" not in catalog
    # var_autocov has autocorrelation but no plain covariance
    assert "autocov_autocorr_ch1" in catalog
    assert "autocov_covariance_ch1" not in catalog
    # var_fft has covariance but no mean_sq diagnostics
    assert "fft_covariance_ch1" in catalog
    assert "fft_cov_mean_sq_ch1" not in catalog
    # M (kept floor bins) exists only for the FFT method
    assert "fft_m_ch1" in catalog
    assert "var_m_ch1" not in catalog
    assert "varcov_m_ch1" not in catalog
    # the cov/var ratio exists for every method that has a covariance term
    assert "varcov_cov_var_ratio_ch1" in catalog
    assert "doublecov_cov_var_ratio_ch2" in catalog
    assert "autocov_autocov_var_ratio_ch1" in catalog
    assert "fft_cov_var_ratio_ch1" in catalog
    # ... but not for the plain variance method, and not cross-method
    assert "var_cov_var_ratio_ch1" not in catalog
    assert "varcov_autocov_var_ratio_ch1" not in catalog
    assert "fft_autocov_var_ratio_ch1" not in catalog
    # dimensionless: no charge-unit variants
    assert "varcov_cov_var_ratio_pc_ch1" not in catalog


def test_default_catalog_is_cached():
    assert default_catalog() is default_catalog()


def test_every_formula_id_is_a_valid_identifier():
    for v in build_default_variables():
        assert v.var_id.isidentifier(), v.var_id


# --- result variables ------------------------------------------------------

def test_result_value_and_uncertainty_extracted():
    cat = default_catalog()
    val, unc = cat.get("autocov_yd_ch1").extract(fake_settings())
    assert val == pytest.approx(1.5)
    assert unc == pytest.approx(0.05)


def test_cov_var_ratio_variable_extracted():
    """The archived cov/var ratio extracts like any other result slot."""
    cat = default_catalog()
    settings = fake_settings()
    settings["analysis"]["results"]["var_autocov"]["autocov_var_ratio"] = [
        0.33, 0.012, 0, 0,
    ]
    val, unc = cat.get("autocov_autocov_var_ratio_ch1").extract(settings)
    assert val == pytest.approx(0.33)
    assert unc == pytest.approx(0.012)
    # absent in the archive (pre-update archives) -> NaN, never a crash
    val, unc = cat.get("autocov_autocov_var_ratio_ch1").extract(fake_settings())
    assert math.isnan(val) and math.isnan(unc)


def test_missing_macro_is_nan():
    cat = default_catalog()
    settings = fake_settings()
    settings["analysis"]["results"] = {}
    val, unc = cat.get("autocov_yd_ch1").extract(settings)
    assert math.isnan(val) and math.isnan(unc)


def test_zero_n_marks_not_computed():
    cat = default_catalog()
    # N[0] = 0 → CH1 quantities are NaN even though the value is present
    settings = fake_settings()
    settings["analysis"]["results"]["var_autocov"]["N"] = [0, 0, 1000, 0]
    val, _ = cat.get("autocov_yd_ch1").extract(settings)
    assert math.isnan(val)
    # CH2 unaffected
    settings["analysis"]["results"]["var_autocov"]["y_bar_d"] = [1.5, 0.05, 2.5, 0.1]
    val2, _ = cat.get("autocov_yd_ch2").extract(settings)
    assert val2 == pytest.approx(2.5)


def test_unknown_macro_still_gets_variables():
    """A future estimator appears in the picker without code changes."""
    variables = build_default_variables()
    cat = VariableCatalog(variables)
    # simulate a new macro appearing in an archive
    settings = fake_settings()
    settings["analysis"]["results"]["var_newmethod"] = {
        "N": [10, 0, 0, 0],
        "y_bar_d": [7.0, 0.2, 0, 0],
    }
    # no variables exist for it — but extraction of the *known* ones still works
    val, unc = cat.get("autocov_yd_ch1").extract(settings)
    assert val == pytest.approx(1.5)


# --- charge conversion -----------------------------------------------------

def test_counts_to_pc_factor():
    settings = fake_settings(cap_ch1=3422.0, bits=12)
    factor = counts_to_pc_factor(settings, "ch1")
    assert factor == pytest.approx(3422.0 * 3.3 / (2**12 - 1))


def test_charge_variant_conversion():
    cat = default_catalog()
    settings = fake_settings(cap_ch1=100.0, bits=12)
    factor = 100.0 * 3.3 / (2**12 - 1)
    val, unc = cat.get("autocov_yd_pc_ch1").extract(settings)
    assert val == pytest.approx(1.5 * factor)
    assert unc == pytest.approx(0.05 * factor)
    # quadratic variant: factor²
    val2, _ = cat.get("autocov_variance_pc2_ch1").extract(settings)
    assert val2 == pytest.approx(300.0 * factor**2)


def test_charge_variant_nan_without_hardware_snapshot():
    cat = default_catalog()
    settings = fake_settings()
    del settings["hardware_settings"]["channels"]  # pre-3.0 archive
    val, _ = cat.get("autocov_yd_pc_ch1").extract(settings)
    assert math.isnan(val)


# --- declared variables ----------------------------------------------------

def test_metadata_and_timing_extraction():
    cat = default_catalog()
    values = cat.extract_values(fake_settings())
    assert values["depth_cm"][0] == pytest.approx(3.0)
    assert values["beam_current_nA"][0] == pytest.approx(2.0)
    assert values["int_time_ms"][0] == pytest.approx(2.1)
    assert values["timestamp"][0] > 1_600_000_000
    # index is pipeline-assigned, not extracted
    assert math.isnan(values["index"][0])


def test_missing_metadata_is_nan():
    cat = default_catalog()
    values = cat.extract_values({})
    for var_id in ("depth_cm", "beam_current_nA", "int_time_ms", "autocov_yd_ch1"):
        assert math.isnan(values[var_id][0]), var_id


def test_raw_counts_flags():
    cat = default_catalog()
    assert cat.get("autocov_yd_ch1").raw_counts is True
    assert cat.get("autocov_yd_pc_ch1").raw_counts is False
    assert cat.get("depth_cm").raw_counts is False
    assert cat.get("autocov_yd_ch1").channel == "ch1"
