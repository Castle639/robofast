"""MINERVA: site code, a plug-in (docs/plugins.md, docs/design.md 3.10).

A MINERVA config lists it in its ``plugins`` (``sites/minerva/weather.yaml``,
``sites/minerva/dispatch.example.json``); :func:`register` adds

* the weather page sources ``minerva.ridge``, ``minerva.sky_temps`` and
  ``minerva.mearth`` (:mod:`.weather`) to
  :data:`robofast.weather_sources.SOURCE_TYPES`;
* the request importer ``minerva.legacy_csv`` (:mod:`.legacy_csv`, the
  legacy target CSV) to :data:`robofast.request_model.REQUEST_IMPORTERS`.

The rest is MINERVA's importers, used from their own command lines or
by hand:

* :mod:`.target_sheet` -- the target lists from their Google Sheet
  (docs/minerva_sheet.md);
* :mod:`.focus_model` -- the legacy focus model (docs/autofocus.md);
* :mod:`.guider_calibration` -- the legacy AO calibration (docs/guider.md).
"""

from __future__ import annotations

from robofast.sites.minerva.weather import MearthSource, RidgeSource, SkyTempsSource

__all__ = ["MearthSource", "RidgeSource", "SkyTempsSource", "register"]


def register() -> None:
    from robofast.request_model import REQUEST_IMPORTERS
    from robofast.sites.minerva.legacy_csv import from_legacy_csv
    from robofast.weather_sources import SOURCE_TYPES

    SOURCE_TYPES.register("minerva.ridge", lambda cfg, site, **kw: RidgeSource(cfg, **kw))
    SOURCE_TYPES.register("minerva.sky_temps", lambda cfg, site, **kw: SkyTempsSource(cfg, **kw))
    SOURCE_TYPES.register("minerva.mearth", lambda cfg, site, **kw: MearthSource(cfg, **kw))
    REQUEST_IMPORTERS.register("minerva.legacy_csv", from_legacy_csv)
