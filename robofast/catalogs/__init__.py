"""Catalogs: legacy target lists in robofast's intermediate form.

:mod:`.model` is the intermediate form (programs, target lists, offset
patterns, sealed private lists) and its mapping onto request groups
(:func:`to_requests`). The importers of particular formats are site code
(docs/design.md 3.10), under ``robofast/sites/<site>/``: the TRES /
FLWO 60-inch telshell catalogs (``robofast.sites.flwo60.catalogs``,
with its command line; docs/catalogs.md) and MINERVA's target-list Sheet
(``robofast.sites.minerva.target_sheet``; docs/minerva_sheet.md).
"""

from robofast.catalogs.model import (
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
