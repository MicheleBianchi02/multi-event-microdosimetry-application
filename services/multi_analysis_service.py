"""
Evaluation pipeline of the multi-measurement analysis: takes the
selected measurement records, assigns the context variables
(``index``), evaluates the X and Y formulas over the variable catalog
and splits the valid points into per-group series (plus the live point
kept separate for styling). Pure Python/numpy — no Qt — so the whole
pipeline is unit-testable.

Filtering (distinct-value selection on metadata variables) also lives
here so the dialog and the tests share one implementation.
"""

from dataclasses import dataclass

import numpy as np

from core.formula import evaluate_formula, referenced_names
from services.variable_catalog import default_catalog

# Variables offered as filter/grouping keys (metadata-style, distinct
# values formatted with %g).
FILTER_VAR_IDS = (
    "depth_cm",
    "beam_current_nA",
    "beam_energy_MeV",
    "detector_voltage_V",
    "int_time_ms",
)
GROUP_VAR_IDS = FILTER_VAR_IDS + ("reset_time_ms", "capacitance_ch1_pF", "capacitance_ch2_pF")

MISSING_LABEL = "—"


@dataclass
class SeriesResult:
    label: str  # group label (MISSING_LABEL for a missing group value)
    is_live: bool
    x: np.ndarray
    y: np.ndarray
    x_unc: np.ndarray
    y_unc: np.ndarray
    filenames: list
    timestamps: list
    y_label: str = ""  # the Y expression this series belongs to (multi-Y)


@dataclass
class PipelineResult:
    series: list  # list[SeriesResult], one per (y-expression, group, live/archive)
    n_selected: int
    n_plotted: int
    n_excluded: int
    excluded_files: list  # filenames dropped for non-finite x/y
    warnings: list


def format_value(value) -> str:
    """Compact numeric formatting used for filters, group labels, tables."""
    if value is None:
        return MISSING_LABEL
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if np.isnan(value):
        return MISSING_LABEL
    return f"{value:g}"


def refresh_values(records, catalog=None) -> None:
    """(Re)extract every variable of every record into ``record.values``
    (invalidates the previous extraction — the live record changes over
    time)."""
    catalog = catalog or default_catalog()
    for record in records:
        record.values = catalog.extract_values(record.settings)


def distinct_values(records, var_id: str) -> list:
    """Sorted distinct formatted values of a variable across records
    (used to populate the filter combos). Missing values are included
    as '—' so they remain selectable."""
    seen = set()
    for record in records:
        pair = record.values.get(var_id)
        seen.add(format_value(pair[0]) if pair else MISSING_LABEL)
    return sorted(seen)


def apply_filters(records, active_filters: dict) -> list:
    """Keep the records matching every {var_id: formatted_value} entry."""
    kept = []
    for record in records:
        ok = True
        for var_id, wanted in active_filters.items():
            pair = record.values.get(var_id)
            got = format_value(pair[0]) if pair else MISSING_LABEL
            if got != wanted:
                ok = False
                break
        if ok:
            kept.append(record)
    return kept


def run_pipeline(records, x_expr: str, y_expr, group_var_id=None, catalog=None) -> PipelineResult:
    """Evaluate the formulas over the *selected* records.

    ``y_expr`` is one expression or a list of them (multi-Y: each gets
    its own series; a record participates only where **every**
    expression is finite, so the series stay comparable).

    The records are sorted chronologically; ``index`` runs 1..N over
    that order. Points whose x or any y is non-finite (missing value,
    division by zero, "not computed" result) are excluded and reported.

    Raises FormulaError when an expression is invalid — the caller
    shows the message; nothing is raised for per-point problems.
    """
    catalog = catalog or default_catalog()
    y_exprs = [y_expr] if isinstance(y_expr, str) else list(y_expr)

    selected = [r for r in records if r.selected]
    selected.sort(key=lambda r: (r.timestamp_epoch == r.timestamp_epoch, r.timestamp_epoch))

    # Context variable: sequence number after filtering/sorting
    for i, record in enumerate(selected, start=1):
        record.values["index"] = (float(i), 0.0)

    var_ids = [v.var_id for v in catalog]
    env = {}
    for var_id in var_ids:
        vals = np.array([r.values.get(var_id, (np.nan, np.nan))[0] for r in selected], dtype=float)
        uncs = np.array([r.values.get(var_id, (np.nan, np.nan))[1] for r in selected], dtype=float)
        env[var_id] = (vals, uncs)

    x_res = evaluate_formula(x_expr, env)
    y_results = [(expr, evaluate_formula(expr, env)) for expr in y_exprs]

    finite = np.isfinite(x_res.values)
    for _, res in y_results:
        finite &= np.isfinite(res.values)
    n_plotted = int(np.count_nonzero(finite))

    excluded_files = [
        selected[i].filename for i in range(len(selected)) if not finite[i]
    ]

    # Group keys (one per record, formatted)
    if group_var_id and group_var_id in env:
        group_raw = env[group_var_id][0]
        group_keys = [format_value(v) for v in group_raw]
    else:
        group_keys = [""] * len(selected)

    warnings = _mixing_warnings(
        selected, [x_expr] + y_exprs, finite, catalog
    )

    series = []
    for y_label, y_res in y_results:
        combo = []
        seen = set()
        for i, record in enumerate(selected):
            if not finite[i]:
                continue
            key = (group_keys[i], record.is_live)
            if key not in seen:
                seen.add(key)
                combo.append(key)
        for group_label, is_live in combo:
            idx = [
                i
                for i in range(len(selected))
                if finite[i]
                and group_keys[i] == group_label
                and selected[i].is_live == is_live
            ]
            series.append(
                SeriesResult(
                    label=group_label or "all",
                    is_live=is_live,
                    x=x_res.values[idx],
                    y=y_res.values[idx],
                    x_unc=x_res.unc[idx],
                    y_unc=y_res.unc[idx],
                    filenames=[selected[i].filename for i in idx],
                    timestamps=[selected[i].timestamp_iso for i in idx],
                    y_label=y_label,
                )
            )

    return PipelineResult(
        series=series,
        n_selected=len(selected),
        n_plotted=n_plotted,
        n_excluded=len(selected) - n_plotted,
        excluded_files=excluded_files,
        warnings=warnings,
    )


def _mixing_warnings(selected, exprs, finite, catalog) -> list:
    """Warn when a raw-counts variable is plotted across measurements
    with different gain/ADC: counts are then not directly comparable
    (the charge-unit variants are)."""
    warnings = []
    used = set()
    for expr in exprs:
        used |= referenced_names(expr)
    counts_vars = [catalog.get(vid) for vid in sorted(used)]
    counts_vars = [v for v in counts_vars if v is not None and v.raw_counts and v.channel]
    for ch in ("ch1", "ch2"):
        cap_id = f"capacitance_{ch}_pF"
        combos = set()
        for i, record in enumerate(selected):
            if not finite[i]:
                continue
            cap = record.values.get(cap_id, (np.nan,))[0]
            bits = record.values.get("adc_bits", (np.nan,))[0]
            if not (np.isfinite(cap) and np.isfinite(bits)):
                continue  # unknown gain proves nothing (e.g. live run w/o config)
            combos.add((format_value(cap), format_value(bits)))
        if len(combos) > 1:
            names = ", ".join(v.var_id for v in counts_vars if v.channel == ch)
            if names:
                warnings.append(
                    f"{names} is in raw counts but the plotted measurements "
                    f"mix different gains/ADCs ({len(combos)} combinations) — "
                    f"use the _pc (charge) variants to compare them."
                )
    return warnings
