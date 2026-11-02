"""Catalogs: legacy target lists in robofast's intermediate form.

:mod:`.model` is the intermediate form (programs, target lists, offset
patterns, sealed private lists) and its mapping onto request groups
(:func:`to_requests`). The importers of particular formats are site code
(docs/design.md 3.10), under ``robofast_core/sites/<site>/``: the TRES /
FLWO 60-inch telshell catalogs (``robofast_core.sites.flwo60.catalogs``,
with its command line; docs/catalogs.md) and MINERVA's target-list Sheet
(``robofast_core.sites.minerva.target_sheet``; docs/minerva_sheet.md).
"""

from robofast_core.catalogs.model import (
    FORMAT,
    CatalogTarget,
    Offset,
    OffsetPattern,
    PrivateCatalog,
    Program,
    TargetList,
    from_dict,
    to_requests,
)

__all__ = [
    "FORMAT",
    "CatalogTarget",
    "Offset",
    "OffsetPattern",
    "PrivateCatalog",
    "Program",
    "TargetList",
    "from_dict",
    "to_requests",
]
