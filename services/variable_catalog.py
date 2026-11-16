"""
Variable catalog for the multi-measurement analysis: the single source
of truth for every quantity that can appear in a plot formula.

Each :class:`Variable` has a stable identifier (used verbatim in
formulas), a human-readable label, a unit, a picker category and an
extractor that pulls a ``(value, uncertainty)`` pair out of a
measurement's settings dictionary (the archived ``settings.json`` or
the live state serialised through the same shape — see
``measurement_catalog.build_live_record``).

Result variables are **auto-generated** from the archive layout
``analysis.results[macro][param] = [ch1, ch1_unc, ch2, ch2_unc]``:
adding a new estimator to ``DataModel.result`` makes its variables
appear here without code changes (only the friendly label maps need an
optional entry). Metadata, timing and hardware variables are declared
by hand; ``index`` and ``timestamp`` are context variables assigned by
the analysis pipeline.

Conventions:
- a missing macro/parameter, or one whose ``N`` is 0 ("not computed",
  the archive convention), extracts to NaN;
- result variables exist in raw counts; the linear/quadratic ones also
  have charge-unit variants (``_pc`` in pC, ``_pc2`` in pC²) converted
  with *that measurement's* recorded capacitance, ADC bits and input
  range, so measurements taken with different gains or ADCs still
  compare correctly in charge units.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Tuple

_nan_pair = (float("nan"), float("nan"))

ValuePair = Tuple[float, float]


def _pair(value, unc=0.0):
    try:
        return (float(value), float(unc))
    except (TypeError, ValueError):
        return _nan_pair


@dataclass(frozen=True)
class Variable:
    var_id: str
    label: str
    unit: str
    category: str
    description: str = ""
    extract: Optional[Callable[[dict], ValuePair]] = None  # None = pipeline-assigned
    # Variables expressed in raw ADC counts: only comparable across
    # measurements with identical gain/ADC (used for the mixing warning).
    raw_counts: bool = False
    channel: str = ""  # "ch1"/"ch2" for result variables ("" otherwise)


# --- label maps for the auto-generated result variables --------------------

RESULT_MACRO_LABELS = {
    "var": "Variance method",
    "var_cov": "Successive covariance method",
    "var_autocov": "Lag-1 autocovariance method",
    "var_doublecov": "Cross-channel covariance method",
    "var_fft": "Frequency-domain (FFT) method",
}

# The parameters each estimator actually computes (mirrors the result
# dictionary / archive schema — a method never shows another method's
# parameters, e.g. the plain variance method has no covariance term).
RESULT_MACRO_PARAMS = {
    "var": ("N", "Mean", "Variance", "y_bar_d"),
    "var_cov": ("N", "Mean", "Variance", "Covariance", "y_bar_d", "cov_mean_sq", "cov_var_ratio"),
    "var_autocov": ("N", "Mean", "Variance", "AutoCorrelation", "y_bar_d", "autocov_mean_sq", "autocov_var_ratio"),
    "var_doublecov": ("N", "Mean", "Variance", "Covariance", "y_bar_d", "cov_mean_sq", "cov_var_ratio"),
    "var_fft": ("N", "M", "Mean", "Variance", "Covariance", "y_bar_d", "cov_var_ratio"),
}

# macro key -> formula id prefix
RESULT_MACRO_PREFIX = {
    "var": "var",
    "var_cov": "varcov",
    "var_autocov": "autocov",
    "var_doublecov": "doublecov",
    "var_fft": "fft",
}

# parameter key -> (id suffix, display label, unit in counts)
RESULT_PARAM_LABELS = {
    "N": ("n", "N samples", ""),
    "M": ("m", "M floor bins", ""),
    "Mean": ("mean", "Mean", "counts"),
    "Variance": ("variance", "Variance", "counts²"),
    "Covariance": ("covariance", "Covariance", "counts²"),
    "AutoCorrelation": ("autocorr", "Autocorrelation", "counts²"),
    "y_bar_d": ("yd", "ȳ_D", "counts"),
    "cov_mean_sq": ("cov_mean_sq", "Cov / Mean²", ""),
    "autocov_mean_sq": ("autocov_mean_sq", "AutoCov / Mean²", ""),
    "cov_var_ratio": ("cov_var_ratio", "Cov / Var", ""),
    "autocov_var_ratio": ("autocov_var_ratio", "AutoCov / Var", ""),
}

# parameters converted to charge units: linear ones scale by the
# counts→pC factor, quadratic ones by its square
_LINEAR_PARAMS = ("Mean", "y_bar_d")
_QUADRATIC_PARAMS = ("Variance", "Covariance", "AutoCorrelation")

_CHANNELS = (("ch1", 0), ("ch2", 2))  # channel -> value index (unc = +1)


def counts_to_pc_factor(settings: dict, ch: str):
    """Per-count charge [pC] of one channel: C[pF] · ΔV/count, with ΔV/count
    = input span / (2^bits − 1) from the archive's frozen hardware snapshot.
    Returns None when the archive does not carry the needed fields
    (pre-3.0 archives, live runs without a loaded hardware config)."""
    hw = settings.get("hardware_settings") or {}
    adc = hw.get("adc") or {}
    rng = adc.get("input_range_volts") or {}
    bits = adc.get("bits")
    cap = ((hw.get("channels") or {}).get(ch) or {}).get("capacitance_pF")
    if bits is None or cap is None or "min_volts" not in rng or "max_volts" not in rng:
        return None
    try:
        span = float(rng["max_volts"]) - float(rng["min_volts"])
        return float(cap) * span / (2 ** int(bits) - 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _result_extractor(macro: str, param: str, ch: str, value_idx: int, scale=None):
    def extract(settings):
        res = (settings.get("analysis") or {}).get("results") or {}
        macro_res = res.get(macro)
        if not isinstance(macro_res, dict):
            return _nan_pair
        n_vals = macro_res.get("N")
        if isinstance(n_vals, (list, tuple)) and len(n_vals) > value_idx:
            try:
                if float(n_vals[value_idx]) == 0:
                    return _nan_pair  # "not computed" per the archive convention
            except (TypeError, ValueError):
                pass
        vals = macro_res.get(param)
        if not isinstance(vals, (list, tuple)) or len(vals) <= value_idx + 1:
            return _nan_pair
        value, unc = float(vals[value_idx]), float(vals[value_idx + 1])
        if scale is not None:
            factor = counts_to_pc_factor(settings, ch)
            if factor is None:
                return _nan_pair
            factor = factor if scale == 1 else factor**scale
            value *= factor
            unc *= factor
        return (value, unc)

    return extract


def _field_extractor(section: str, key: str, scale: float = 1.0):
    def extract(settings):
        value = (settings.get(section) or {}).get(key)
        if value is None:
            return _nan_pair
        try:
            return (float(value) * scale, 0.0)
        except (TypeError, ValueError):
            return _nan_pair

    return extract


def _timestamp_extractor():
    def extract(settings):
        iso = (settings.get("measurement_info") or {}).get("timestamp_start")
        if not iso:
            return _nan_pair
        try:
            return (datetime.fromisoformat(iso).timestamp(), 0.0)
        except (TypeError, ValueError):
            return _nan_pair

    return extract


def build_default_variables() -> list:
    """Assemble the full variable list (auto-generated results + declared
    metadata/timing/hardware + context variables)."""
    variables = []

    # --- auto-generated result variables (one per macro × param × channel;
    # only the parameters the method actually computes are generated)
    for macro, label in RESULT_MACRO_LABELS.items():
        prefix = RESULT_MACRO_PREFIX[macro]
        cat = f"Results — {label}"
        for param in RESULT_MACRO_PARAMS[macro]:
            suffix, p_label, p_unit = RESULT_PARAM_LABELS[param]
            for ch, value_idx in _CHANNELS:
                vid = f"{prefix}_{suffix}_{ch}"
                variables.append(
                    Variable(
                        var_id=vid,
                        label=f"{p_label} — {ch.upper()}",
                        unit=p_unit,
                        category=cat,
                        description=(
                            f"{p_label} of the {label.lower()} ({ch.upper()}) "
                            f"in ADC counts."
                        ),
                        extract=_result_extractor(macro, param, ch, value_idx),
                        raw_counts=bool(p_unit),
                        channel=ch,
                    )
                )
                if param in _LINEAR_PARAMS:
                    variables.append(
                        Variable(
                            var_id=f"{prefix}_{suffix}_pc_{ch}",
                            label=f"{p_label} — {ch.upper()}",
                            unit="pC",
                            category=cat,
                            description=(
                                f"{p_label} of the {label.lower()} ({ch.upper()}) "
                                "converted to charge with this measurement's "
                                "recorded capacitance and ADC range (Q = C·ΔV)."
                            ),
                            extract=_result_extractor(macro, param, ch, value_idx, scale=1),
                            channel=ch,
                        )
                    )
                elif param in _QUADRATIC_PARAMS:
                    variables.append(
                        Variable(
                            var_id=f"{prefix}_{suffix}_pc2_{ch}",
                            label=f"{p_label} — {ch.upper()}",
                            unit="pC²",
                            category=cat,
                            description=(
                                f"{p_label} of the {label.lower()} ({ch.upper()}) "
                                "in charge units (pC²), using this measurement's "
                                "recorded capacitance and ADC range."
                            ),
                            extract=_result_extractor(macro, param, ch, value_idx, scale=2),
                            channel=ch,
                        )
                    )

    # --- measurement metadata
    for var_id, label, unit, key in (
        ("beam_current_nA", "Beam current", "nA", "beam_current_nA"),
        ("beam_energy_MeV", "Beam energy", "MeV", "beam_energy_MeV"),
        ("depth_cm", "Depth in phantom", "cm", "depth_cm"),
        ("detector_voltage_V", "Detector voltage", "V", "detector_voltage_V"),
    ):
        variables.append(
            Variable(
                var_id=var_id,
                label=label,
                unit=unit,
                category="Measurement",
                description=f"{label} recorded in the measurement metadata.",
                extract=_field_extractor("experimental_setup", key),
            )
        )

    # --- timing
    for var_id, label, unit, key, scale in (
        ("int_time_ms", "Integration time", "ms", "integration_time_s", 1000.0),
        ("reset_time_ms", "Reset time", "ms", "reset_time_s", 1000.0),
        ("period_ms", "Period", "ms", "period_s", 1000.0),
        ("sample_freq_hz", "Sample frequency", "Hz", "sample_frequency_hz", 1.0),
    ):
        variables.append(
            Variable(
                var_id=var_id,
                label=label,
                unit=unit,
                category="Timing",
                description=f"Actual (quantized) {label.lower()} of the run.",
                extract=_field_extractor("timing_settings", key, scale),
            )
        )

    # --- hardware
    def _capacitance(ch):
        def extract(settings):
            cap = ((settings.get("hardware_settings") or {}).get("channels") or {}).get(ch)
            if not cap:
                return _nan_pair
            return _pair(cap.get("capacitance_pF"))

        return extract

    def _adc_bits(settings):
        bits = ((settings.get("hardware_settings") or {}).get("adc") or {}).get("bits")
        return _pair(bits)

    for ch in ("ch1", "ch2"):
        variables.append(
            Variable(
                var_id=f"capacitance_{ch}_pF",
                label=f"Capacitance — {ch.upper()}",
                unit="pF",
                category="Hardware",
                description=f"Total integration capacitance applied on {ch.upper()}.",
                extract=_capacitance(ch),
            )
        )
    variables.append(
        Variable(
            var_id="adc_bits",
            label="ADC resolution",
            unit="bits",
            category="Hardware",
            description="ADC bit depth of this measurement.",
            extract=_adc_bits,
        )
    )

    # --- context variables
    variables.append(
        Variable(
            var_id="timestamp",
            label="Measurement timestamp",
            unit="s",
            category="Special",
            description=(
                "Start time of the measurement as seconds since the epoch "
                "(useful to watch drift over a shift)."
            ),
            extract=_timestamp_extractor(),
        )
    )
    variables.append(
        Variable(
            var_id="index",
            label="Measurement index",
            unit="",
            category="Special",
            description=(
                "Position of the measurement in the plotted sequence "
                "(after filtering and sorting by time): 1, 2, 3, …"
            ),
            extract=None,  # assigned by the analysis pipeline
        )
    )

    return variables


class VariableCatalog:
    """Ordered set of variables with lookup by id and grouped-by-category
    access for the picker UI."""

    def __init__(self, variables):
        self._variables = list(variables)
        self._by_id = {v.var_id: v for v in self._variables}

    def __iter__(self):
        return iter(self._variables)

    def __len__(self):
        return len(self._variables)

    def __contains__(self, var_id):
        return var_id in self._by_id

    def get(self, var_id):
        return self._by_id.get(var_id)

    def categories(self):
        """Ordered mapping category -> list of variables (insertion order)."""
        cats = {}
        for v in self._variables:
            cats.setdefault(v.category, []).append(v)
        return cats

    def extract_values(self, settings: dict):
        """Extract every variable from one measurement's settings dict.
        Returns {var_id: (value, unc)}; missing quantities are NaN."""
        values = {}
        for v in self._variables:
            if v.extract is None:
                values[v.var_id] = _nan_pair  # context variables (pipeline-assigned)
            else:
                try:
                    values[v.var_id] = v.extract(settings)
                except Exception:
                    values[v.var_id] = _nan_pair
        return values


_DEFAULT_CATALOG = None


def default_catalog():
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = VariableCatalog(build_default_variables())
    return _DEFAULT_CATALOG
