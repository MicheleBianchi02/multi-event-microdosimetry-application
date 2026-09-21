"""
Measurement catalog for the multi-measurement analysis: scanning a
workspace folder into lightweight records and building the "live"
record of the current in-progress measurement.

A :class:`MeasurementRecord` carries only the archived
``settings.json`` (never ``data.csv``), so scanning hundreds of
measurements stays fast. The same record shape serves archived
measurements and the live one — the live record is serialised through
``DataExporter.build_settings_dict`` + ``DataModel.result``, so every
variable extractor works on it unchanged.

``scan_workspace`` is the single scanning implementation: the offline
import page reuses it for its measurement cards.
"""

import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

LIVE_FILENAME = "LIVE — current measurement"


@dataclass
class MeasurementRecord:
    path: str  # ZIP path on disk (None for the live record)
    filename: str
    settings: dict
    timestamp_epoch: float  # NaN when unknown
    is_live: bool = False
    values: dict = field(default_factory=dict)  # var_id -> (value, unc), filled by the catalog
    selected: bool = True  # per-measurement checkbox in the UI

    @property
    def timestamp_iso(self):
        iso = (self.settings.get("measurement_info") or {}).get("timestamp_start")
        return iso or ""

    @property
    def is_problematic(self):
        return bool((self.settings.get("measurement_info") or {}).get("is_problematic", False))


def parse_timestamp(settings: dict) -> float:
    iso = (settings.get("measurement_info") or {}).get("timestamp_start")
    if not iso:
        return float("nan")
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return float("nan")


def make_record(path, filename, settings, is_live=False) -> MeasurementRecord:
    return MeasurementRecord(
        path=path,
        filename=filename,
        settings=settings,
        timestamp_epoch=parse_timestamp(settings),
        is_live=is_live,
    )


def scan_workspace(workspace_dir: str) -> list:
    """List every measurement ZIP of a workspace folder, oldest first.

    Only archives carrying a readable ``settings.json`` are returned
    (legacy CSVs and foreign ZIPs are skipped, as on the import page).
    Unreadable files are logged and skipped, never fatal.
    """
    records = []
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return records

    for name in sorted(os.listdir(workspace_dir)):
        if not name.lower().endswith(".zip"):
            continue
        path = os.path.join(workspace_dir, name)
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "settings.json" not in zf.namelist():
                    continue
                with zf.open("settings.json") as sf:
                    settings = json.load(sf)
            records.append(make_record(path, name, settings))
        except Exception as exc:  # corrupted archive: skip, keep scanning
            logger.warning("Skipping unreadable measurement %s: %s", name, exc)

    # Chronological order (NaN timestamps keep scan order, at the end)
    records.sort(key=lambda r: (r.timestamp_epoch == r.timestamp_epoch, r.timestamp_epoch))
    return records


def build_live_record(session_model, data_model, hardware_manager=None) -> MeasurementRecord:
    """Serialise the current (possibly running) measurement into the same
    record shape as an archived one, via the settings builder used at save
    time — formulas valid for saved measurements work unchanged on it."""
    from services.data_exporter import DataExporter

    settings = DataExporter.build_settings_dict(
        session_model,
        results_dict=data_model.result,
        hardware_manager=hardware_manager,
    )
    # While a run is in progress there is no timestamp_end yet; the
    # extractor only reads timestamp_start, which the builder fills.
    return make_record(None, LIVE_FILENAME, settings, is_live=True)
