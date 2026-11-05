"""MINERVA's legacy target CSV as request groups (request importer
``minerva.legacy_csv``).

A dispatch plan imports it (with ``plugins: ["robofast.sites.minerva"]``)::

    "imports": [{"type": "minerva.legacy_csv", "path": "targets.csv"}]

(options: ``instrument``, the dispatch config's name of the instrument the
targets are for, default ``"spectrograph"``).
"""

from __future__ import annotations

import csv
from pathlib import Path

from robofast.request_model import RequestGroup

__all__ = ["from_legacy_csv"]


def from_legacy_csv(path: str | Path, *, instrument: str = "spectrograph") -> list[RequestGroup]:
    """Convert a legacy MINERVA target spreadsheet export (``targets.csv``,
    as read by minerva-control's ``targetlist.mkdict``) to request groups.

    Columns used: ``name``, ``ra`` (hours), ``dec`` (deg), ``exptime`` (s),
    ``priority``, ``active`` (1 to include), and optionally ``seplimit``
    (s between visits; legacy default 3 h), ``maxobs`` (visits per night;
    legacy default 2) and ``num`` (exposures; default 1). A header's
    parenthesised unit, e.g. ``ra (hours)``, is ignored, as the legacy reader
    did. Each active row becomes a standing request with that cadence
    (``extra_params.cadence``) and ``ipp_value`` the legacy priority; with
    the default instrument its configuration is a ``SPECTRUM`` (fed by every
    free telescope, as legacy's ``doSpectra`` with its ``tele_list``).
    """
    groups = []
    with Path(path).open(newline="") as f:
        reader = csv.DictReader(f)
        for n, raw in enumerate(reader, start=2):
            row = {k.split("(")[0].strip(): (v or "").strip() for k, v in raw.items() if k}
            what = f"{path}:{n}"
            try:
                if row.get("active", "1") not in ("1", "1.0", "True", "true"):
                    continue
                name = row["name"]
                seplimit = float(row["seplimit"]) if row.get("seplimit") else 3 * 3600.0
                maxobs = int(float(row["maxobs"])) if row.get("maxobs") else 2
                count = int(float(row["num"])) if row.get("num") else 1
                key = name.replace(" ", "_")
                spectrum = instrument == "spectrograph"
                config = {
                    "id": f"{key}/c",
                    "type": "SPECTRUM" if spectrum else "EXPOSE",
                    "instrument_type": instrument,
                    "target": {
                        "type": "ICRS",
                        "name": name,
                        "ra": float(row["ra"]) * 15.0,
                        "dec": float(row["dec"]),
                    },
                    "instrument_configs": [
                        {"exposure_time": float(row["exptime"]), "exposure_count": count}
                    ],
                }
                cadence = {"min_interval": seplimit, "max_per_night": maxobs}
                groups.append(
                    RequestGroup.from_dict(
                        {
                            "id": key,
                            "name": name,
                            "proposal": "legacy",
                            "ipp_value": float(row["priority"]),
                            "operator": "SINGLE",
                            "observation_type": "NORMAL",
                            "requests": [
                                {
                                    "id": f"{key}/r",
                                    "extra_params": {"cadence": cadence},
                                    "configurations": [config],
                                }
                            ],
                        },
                        what,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{what}: {exc}") from exc
    return groups
