"""Star catalogs other than Gaia, for users Gaia's licence does not cover
(docs/sky.md, "Catalog sources" and "Licences").

Gaia DR3 is CC BY-NC 3.0 IGO: commercial use needs ESA's permission. The
sources here are US Government catalogs, served by IRSA (NASA/IPAC):

* ``ucac4`` -- USNO's UCAC4 (Zacharias et al. 2013): 113 million stars to
  R ~ 16, positions at J2000.0 with proper motions (1-6 mas/yr), APASS
  B, V, g, r, i and 2MASS J, H, Ks photometry. IRSA table ``ucac4_sources``.
* ``2mass`` -- the 2MASS All-Sky Point Source Catalog (Skrutskie et al.
  2006): J, H, Ks, positions at the epoch each field was observed
  (1997-2001), **no proper motions**. IRSA table ``fp_psc``.

Each is fetched per HEALPix tile, like Gaia (:class:`IrsaTap`: a cone
round the tile, then the tile's own stars kept), and converted to the
catalog's records with an **estimated Gaia G and BP-RP**, from the Gaia
EDR3 documentation's photometric relations (section 5.5.1, tables 5.7 and
5.8; Riello et al. 2021):

* from Johnson B, V:  ``G - V = -0.04749 - 0.0124 x - 0.2901 x^2 + 0.02008 x^3``,
  x = B-V (-0.4 < x < 3.3; scatter 0.05 mag);
* from 2MASS J, Ks:   ``G - Ks = 0.1683 + 3.803 x - 1.45 x^2 + 0.7867 x^3`` and
  ``BP-RP = 0.09396 + 2.581 x - 2.782 x^2 + 2.788 x^3 - 0.8027 x^4``,
  x = J-Ks (-0.2 < x < 1.1; scatter 0.13 and 0.10 mag).

UCAC4 uses B, V when APASS has both, else J, Ks, else its own model
magnitude (579-642 nm, about R: taken as G, good to a few tenths). 2MASS
uses J, Ks. Outside the validity range the colour is clipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from robofast.catalog import (
    CATALOG_SOURCES,
    TILE_DTYPE,
    CatalogSource,
    ang2pix,
    pix2ang,
    separation,
)

log = logging.getLogger(__name__)

__all__ = [
    "IRSA_TAP",
    "IrsaTap",
    "bprp_from_jk",
    "g_from_bv",
    "g_from_jk",
    "parse_table_csv",
    "tile_cone",
    "twomass_records",
    "ucac4_records",
]

#: IRSA's synchronous TAP service.
IRSA_TAP = "https://irsa.ipac.caltech.edu/TAP/sync"


# -- photometric relations (module doc) ------------------------------------------------


def _poly(x: np.ndarray, coeffs: tuple[float, ...]) -> np.ndarray:
    return sum(c * x**i for i, c in enumerate(coeffs))


def g_from_bv(b: Any, v: Any) -> np.ndarray:
    """Gaia G from Johnson B and V (module doc); NaN where either is."""
    b, v = np.asarray(b, dtype=float), np.asarray(v, dtype=float)
    x = np.clip(b - v, -0.4, 3.3)
    return v + _poly(x, (-0.04749, -0.0124, -0.2901, 0.02008))


def g_from_jk(j: Any, k: Any) -> np.ndarray:
    """Gaia G from 2MASS J and Ks (module doc); NaN where either is."""
    j, k = np.asarray(j, dtype=float), np.asarray(k, dtype=float)
    x = np.clip(j - k, -0.2, 1.1)
    return k + _poly(x, (0.1683, 3.803, -1.45, 0.7867))


def bprp_from_jk(j: Any, k: Any) -> np.ndarray:
    """Gaia BP-RP from 2MASS J and Ks (module doc); NaN where either is."""
    x = np.clip(np.asarray(j, dtype=float) - np.asarray(k, dtype=float), -0.2, 1.1)
    return _poly(x, (0.09396, 2.581, -2.782, 2.788, -0.8027))


# -- records ----------------------------------------------------------------------------


def _records(n: int) -> np.ndarray:
    rec = np.zeros(n, TILE_DTYPE)
    rec["bp_rp"] = np.nan
    return rec


def ucac4_records(cols: dict[str, np.ndarray]) -> np.ndarray:
    """IRSA ``ucac4_sources`` columns (``uniqueid, ra, dec, pmrac, pmdc,
    momag, bmag, vmag, jmag, kmag``) as :data:`TILE_DTYPE` records."""
    n = len(cols["ra"])
    rec = _records(n)
    rec["source_id"] = np.nan_to_num(cols["uniqueid"]).astype(np.int64)
    rec["ra"], rec["dec"] = cols["ra"], cols["dec"]
    rec["pmra"] = np.nan_to_num(cols["pmrac"])
    rec["pmdec"] = np.nan_to_num(cols["pmdc"])
    g_bv = g_from_bv(cols["bmag"], cols["vmag"])
    g_jk = g_from_jk(cols["jmag"], cols["kmag"])
    rec["g"] = np.where(np.isfinite(g_bv), g_bv, np.where(np.isfinite(g_jk), g_jk, cols["momag"]))
    rec["bp_rp"] = bprp_from_jk(cols["jmag"], cols["kmag"])
    return rec


def twomass_records(cols: dict[str, np.ndarray]) -> np.ndarray:
    """IRSA ``fp_psc`` columns (``cntr, ra, dec, j_m, k_m``) as
    :data:`TILE_DTYPE` records (no proper motion)."""
    rec = _records(len(cols["ra"]))
    rec["source_id"] = np.nan_to_num(cols["cntr"]).astype(np.int64)
    rec["ra"], rec["dec"] = cols["ra"], cols["dec"]
    rec["g"] = g_from_jk(cols["j_m"], cols["k_m"])
    rec["bp_rp"] = bprp_from_jk(cols["j_m"], cols["k_m"])
    return rec


def parse_table_csv(text: str) -> dict[str, np.ndarray]:
    """A TAP CSV answer as ``{column: float array}`` (empty cells NaN;
    column names lower case)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    head = [h.strip().strip('"').lower() for h in lines[0].split(",")]
    rows = [ln.split(",") for ln in lines[1:]]
    out: dict[str, np.ndarray] = {}
    for j, h in enumerate(head):
        vals = np.full(len(rows), np.nan)
        for i, r in enumerate(rows):
            v = r[j].strip().strip('"') if j < len(r) else ""
            if v:
                try:
                    vals[i] = float(v)
                except ValueError:
                    pass
        out[h] = vals
    return out


# -- fetching ---------------------------------------------------------------------------


def tile_cone(level: int, pix: int) -> tuple[float, float, float]:
    """(ra, dec, radius) deg of a cone that holds HEALPix tile ``pix``."""

    def one(dx: float, dy: float) -> tuple[float, float]:
        ra, dec = pix2ang(level, np.array([pix]), dx, dy)
        return float(np.ravel(ra)[0]), float(np.ravel(dec)[0])

    ra0, dec0 = one(0.5, 0.5)
    corners = [one(dx, dy) for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1))]
    r = max(float(separation(ra0, dec0, ra, dec)) for ra, dec in corners)
    return ra0, dec0, r + 1.0 / 60.0


class IrsaTap:
    """Fetches a tile of an IRSA-hosted catalog through its TAP service:
    the stars in a cone round the tile brighter than a cut on the
    catalog's own magnitudes, converted (``convert``) and kept if they are
    the tile's and have an estimated G under the tile's limit.

    ``post(url, data, timeout) -> text`` sends the query (default httpx;
    the tests give their own)."""

    def __init__(
        self,
        table: str,
        columns: str,
        cut: str,
        convert: Callable[[dict[str, np.ndarray]], np.ndarray],
        *,
        url: str | None = None,
        timeout: float = 600.0,
        post: Callable[[str, dict[str, str], float], str] | None = None,
    ) -> None:
        self.table = table
        self.columns = columns
        #: ADQL condition with ``{mag}`` (the tile's G limit).
        self.cut = cut
        self.convert = convert
        self.url = url or IRSA_TAP
        self.timeout = timeout
        self.post = post or _httpx_post

    def query(self, where: str) -> np.ndarray:
        adql = f"SELECT {self.columns} FROM {self.table} WHERE {where}"
        data = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql}
        cols = parse_table_csv(self.post(self.url, data, self.timeout))
        if not cols:
            return np.zeros(0, TILE_DTYPE)
        return self.convert(cols)

    def __call__(self, level: int, pix: int, mag_limit: float) -> np.ndarray:
        ra, dec, r = tile_cone(level, pix)
        rec = self.query(
            f"CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {r:.6f})) = 1"
            f" AND ({self.cut.format(mag=mag_limit)})"
        )
        keep = (ang2pix(level, rec["ra"], rec["dec"]) == pix) & (rec["g"] < mag_limit)
        return rec[keep]

    def bright(self, mag_limit: float) -> np.ndarray:
        """Every star brighter than ``mag_limit`` (estimated G), all sky."""
        rec = self.query(self.cut.format(mag=mag_limit))
        return rec[rec["g"] < mag_limit]


def _httpx_post(url: str, data: dict[str, str], timeout: float) -> str:
    import httpx

    r = httpx.post(url, data=data, timeout=timeout)
    r.raise_for_status()
    return r.text


def _ucac4(url: str | None) -> IrsaTap:
    # The cut keeps every star whose G could be under the limit: G - V
    # and G - R are a few tenths at most for any colour, G - Ks < 0.7
    # only for the bluest stars.
    return IrsaTap(
        "ucac4_sources",
        "uniqueid, ra, dec, pmrac, pmdc, momag, bmag, vmag, jmag, kmag",
        "momag < {mag} + 1 OR vmag < {mag} + 1 OR kmag < {mag} + 0.7",
        ucac4_records,
        url=url,
    )


def _twomass(url: str | None) -> IrsaTap:
    return IrsaTap(
        "fp_psc", "cntr, ra, dec, j_m, k_m", "k_m < {mag} + 0.7", twomass_records, url=url
    )


def _bright(make: Callable[[str | None], IrsaTap]) -> Callable[[float, str | None], np.ndarray]:
    return lambda mag, url: make(url).bright(mag)


CATALOG_SOURCES.register(
    "ucac4",
    CatalogSource(
        name="ucac4",
        title="UCAC4 (USNO; IRSA ucac4_sources)",
        licence=(
            "US Government work (USNO), free of charge; cite Zacharias et al. 2013, AJ 145, 44"
            " and IRSA's DOI 10.26131/IRSA17 (docs/sky.md)"
        ),
        noncommercial=False,
        licence_url="https://irsa.ipac.caltech.edu/data/USNO/UCAC4/overview.html",
        epoch=2000.0,
        proper_motion=True,
        directory="ucac4-g{mag:g}-hpx{level}",
        bright_file="ucac4_bright_stars.npy",
        magnitudes="G estimated from APASS B,V, else 2MASS J,Ks, else UCAC model mag",
        fetcher=_ucac4,
        bright=_bright(_ucac4),
    ),
)
CATALOG_SOURCES.register(
    "2mass",
    CatalogSource(
        name="2mass",
        title="2MASS All-Sky Point Source Catalog (IRSA fp_psc)",
        licence=(
            "public (IRSA: no usage restrictions), acknowledgement requested; cite"
            " Skrutskie et al. 2006, AJ 131, 1163 (docs/sky.md)"
        ),
        noncommercial=False,
        licence_url="https://irsa.ipac.caltech.edu/data_use_terms.html",
        epoch=2000.0,
        proper_motion=False,
        directory="2mass-psc-g{mag:g}-hpx{level}",
        bright_file="2mass_bright_stars.npy",
        magnitudes="G and BP-RP estimated from 2MASS J,Ks",
        fetcher=_twomass,
        bright=_bright(_twomass),
    ),
)
