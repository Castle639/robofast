"""Catalog sources other than Gaia (docs/sky.md, "Catalog sources"), and
the switch that refuses non-commercial data.

The UCAC4 and 2MASS answers here are made from the test sky
(tests/core/skydata.py): each star's G and BP-RP turned into the catalog's
own photometry (B, V, J, Ks) by inverting the relations
:mod:`robofast_core.catalog_sources` uses, with 0.05 mag of scatter, and
served by a fake IRSA TAP service, so the real fetch path (query, CSV,
conversion, tile cut) runs with no network. Frames are rendered from the
test sky itself (the truth) and solved against the other source.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import numpy as np
import pytest

from robofast_core import astrometry as ast
from robofast_core import catalog as cat
from robofast_core import catalog_sources as cs
from robofast_core import zenodo, zenodo_assets
from robofast_core.guider import offset as og
from tests.core.skydata import sky_dir
from tests.core.test_astrometry import (
    DENSE,
    FAU_PHOT,
    HD185144,
    IMAGER_PHOT,
    SPARSE,
    WHEN,
    check,
    fau,
    imager,
    scene,
)


@pytest.fixture(scope="module")
def truth() -> cat.Catalog:
    return cat.catalog_at(sky_dir())


def _invert(f, target: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """x in [lo, hi] with f(x) = target (f monotonic), by interpolation."""
    x = np.linspace(lo, hi, 2001)
    y = f(x)
    order = np.argsort(y)
    return np.interp(target, y[order], x[order])


def raw_columns(stars: cat.Stars, rng: np.random.Generator, *, pm: bool) -> dict:
    """The test sky's stars as UCAC4 / 2MASS columns (module doc)."""
    n = len(stars)
    bp_rp = np.nan_to_num(stars.bp_rp, nan=0.8)
    x = _invert(lambda x: cs.bprp_from_jk(x, 0.0), bp_rp, -0.2, 1.1)
    k = stars.g - (cs.g_from_jk(x, 0.0) - 0.0)  # G - Ks at J - Ks = x
    j = k + x
    v = cat.g_to_v(stars.g, bp_rp)
    bv = _invert(lambda c: cs.g_from_bv(c, 0.0), stars.g - v, -0.4, 3.3)
    noise = lambda: rng.normal(0.0, 0.05, n)  # noqa: E731
    return {
        "uniqueid": np.arange(1, n + 1, dtype=float),
        "cntr": np.arange(1, n + 1, dtype=float),
        "ra": stars.ra,
        "dec": stars.dec,
        "pmrac": stars.pmra if pm else np.full(n, np.nan),
        "pmdc": stars.pmdec if pm else np.full(n, np.nan),
        "momag": stars.g + noise(),
        "bmag": v + bv + noise(),
        "vmag": v + noise(),
        "jmag": j + noise(),
        "kmag": k + noise(),
        "j_m": j + noise(),
        "k_m": k + noise(),
    }


def csv_text(cols: dict, names: list[str]) -> str:
    out = io.StringIO()
    out.write(",".join(names) + "\n")
    for i in range(len(cols["ra"])):
        out.write(
            ",".join("" if not np.isfinite(cols[n][i]) else repr(float(cols[n][i])) for n in names)
            + "\n"
        )
    return out.getvalue()


class FakeIrsa:
    """IRSA's TAP service for a fixed set of rows: answers every query with
    all of them (the fetcher must cut the tile itself) and keeps the ADQL."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.queries: list[str] = []

    def __call__(self, url: str, data: dict, timeout: float) -> str:
        assert url == cs.IRSA_TAP and data["LANG"] == "ADQL" and data["FORMAT"] == "csv"
        self.queries.append(data["QUERY"])
        return self.text


def other_catalog(
    truth, tmp, name: str, fields, seed: int = 1, radius: float = 0.2
) -> tuple[cat.Catalog, FakeIrsa]:
    """A ``name`` (``ucac4`` or ``2mass``) catalog over ``fields`` made from
    the test sky (module doc), its tiles fetched through :class:`FakeIrsa`."""
    rng = np.random.default_rng(seed)
    parts = [truth.cone(ra, dec, radius, epoch=2000.0) for ra, dec in fields]
    stars = cat.Stars.concat(parts)
    if name == "2mass":
        # Positions at the 2MASS epoch, no proper motion.
        cols = raw_columns(stars, rng, pm=False)
        fake = FakeIrsa(csv_text(cols, ["cntr", "ra", "dec", "j_m", "k_m"]))
        fetch = cs.IrsaTap("fp_psc", "cntr, ra, dec, j_m, k_m", "k_m < {mag}",
                           cs.twomass_records, post=fake)  # fmt: skip
    else:
        cols = raw_columns(stars, rng, pm=True)
        names = ["uniqueid", "ra", "dec", "pmrac", "pmdc", "momag", "bmag", "vmag"]
        fake = FakeIrsa(csv_text(cols, [*names, "jmag", "kmag"]))
        fetch = cs.IrsaTap("ucac4_sources", ", ".join(names), "vmag < {mag}",
                           cs.ucac4_records, post=fake)  # fmt: skip
    tiles = cat.TileCache(tmp, fetch=fetch, source=name, allow_noncommercial=False)
    return cat.Catalog(tiles, bright=None, allow_noncommercial=False), fake


# -- photometry -------------------------------------------------------------------------


def test_photometric_relations():
    # A Sun-like star: B-V 0.65 -> G - V ~ -0.17; J-Ks 0.37 -> G - Ks ~ 1.42,
    # BP-RP ~ 0.79 (Gaia EDR3 documentation, tables 5.7, 5.8).
    assert cs.g_from_bv(10.65, 10.0) == pytest.approx(10.0 - 0.1726, abs=1e-3)
    assert cs.g_from_jk(9.37, 9.0) == pytest.approx(9.0 + 1.4167, abs=1e-3)
    assert cs.bprp_from_jk(9.37, 9.0) == pytest.approx(0.794, abs=1e-3)
    assert np.isnan(cs.g_from_bv(np.nan, 10.0))
    # Out of range colours are clipped, not extrapolated.
    assert cs.g_from_jk(14.0, 10.0) == pytest.approx(cs.g_from_jk(11.1, 10.0))


def test_ucac4_magnitude_fallbacks():
    nan = np.nan
    cols = {
        "uniqueid": np.array([1.0, 2.0, 3.0]),
        "ra": np.array([10.0, 10.1, 10.2]),
        "dec": np.array([5.0, 5.0, 5.0]),
        "pmrac": np.array([12.0, nan, 0.0]),
        "pmdc": np.array([-3.0, nan, 0.0]),
        "momag": np.array([11.0, 12.0, 13.0]),
        "bmag": np.array([11.65, nan, nan]),
        "vmag": np.array([11.0, nan, nan]),
        "jmag": np.array([nan, 10.37, nan]),
        "kmag": np.array([nan, 10.0, nan]),
    }
    rec = cs.ucac4_records(cols)
    assert rec["g"][0] == pytest.approx(cs.g_from_bv(11.65, 11.0))  # APASS B, V
    assert rec["g"][1] == pytest.approx(cs.g_from_jk(10.37, 10.0))  # 2MASS J, Ks
    assert rec["g"][2] == pytest.approx(13.0)  # UCAC's own magnitude
    assert rec["pmra"][0] == 12.0 and rec["pmra"][1] == 0.0
    assert np.isnan(rec["bp_rp"][0]) and rec["bp_rp"][1] == pytest.approx(0.794, abs=1e-3)
    assert list(rec["source_id"]) == [1, 2, 3]


# -- fetching ---------------------------------------------------------------------------


def test_tiles_are_fetched_through_irsa_and_cut_to_the_tile(truth, tmp_path):
    catalog, fake = other_catalog(truth, tmp_path, "ucac4", [SPARSE])
    stars = catalog.cone(*SPARSE, 0.1, epoch=WHEN)
    assert len(stars) > 10 and set(stars.source) == {"ucac4"}
    assert fake.queries and all("FROM ucac4_sources" in q for q in fake.queries)
    assert all("CIRCLE('ICRS'" in q and "vmag < 16" in q for q in fake.queries)
    # Each tile file holds only its own stars, under the limit.
    tiles = catalog.tiles
    for p in cat.tiles_in_cone(tiles.level, *SPARSE, 0.1):
        rec = np.load(tiles.path(p))
        assert (cat.ang2pix(tiles.level, rec["ra"], rec["dec"]) == p).all()
        assert (rec["g"] < 16.0).all()
    m = (tiles.directory / "manifest.json").read_text()
    assert tiles.directory.name == "ucac4-g16-hpx7"
    assert '"source": "ucac4"' in m and '"noncommercial": false' in m
    # Estimated G agrees with the truth (the relations' own scatter + noise).
    t = truth.cone(*SPARSE, 0.1, epoch=WHEN)
    d = [
        float(stars.g[i] - t.g[np.argmin(cat.separation(stars.ra[i], stars.dec[i], t.ra, t.dec))])
        for i in range(len(stars))
    ]
    assert abs(np.median(d)) < 0.05 and np.std(d) < 0.2
    # Offline, cached: no new query.
    n = len(fake.queries)
    catalog.cone(*SPARSE, 0.1, epoch=WHEN)
    assert len(fake.queries) == n


def test_tile_cone_holds_the_tile():
    for pix in (0, 47392, 196607):
        ra, dec, r = cs.tile_cone(7, pix)
        cra, cdec = cat.pix2ang(
            7, np.array([pix] * 4), np.array([0, 1, 0, 1]), np.array([0, 0, 1, 1])
        )
        assert (cat.separation(ra, dec, cra, cdec) < r).all()


# -- what must work with another source ----------------------------------------------------


@pytest.mark.parametrize("name", ["ucac4", "2mass"])
def test_plate_solving_on_another_source(truth, tmp_path, name):
    catalog, _ = other_catalog(truth, tmp_path, name, [SPARSE, HD185144])
    cam = imager(pa=37.5)
    frame = scene(truth, cam, IMAGER_PHOT).render(*HD185144, WHEN, 10.0)
    sol = ast.solve_near(frame.image, catalog, *HD185144, ast.SolverConfig(0.61, search_radius=0.1),
                         epoch=WHEN)  # fmt: skip
    # 2MASS has no proper motions: its positions are a quarter century old.
    check(sol, frame, cam, pos=0.5 if name == "ucac4" else 1.5)
    assert sol.n_matched >= 10


def test_fau_solve_and_simulated_field_on_ucac4(truth, tmp_path):
    catalog, _ = other_catalog(truth, tmp_path, "ucac4", [DENSE], radius=0.1)
    cam = fau()
    # The simulator renders the other source's stars, through the zero
    # point of the port, from their estimated G.
    frame = scene(catalog, cam, FAU_PHOT).render(*DENSE, WHEN, 5.0)
    assert frame.image.max() > frame.truth["sky_adu"] + 50 and len(frame.truth["x"]) > 20
    cfg = ast.SolverConfig(0.265, search_radius=0.05, min_matches=3)
    sol = ast.solve_near(frame.image, catalog, *DENSE, cfg, epoch=WHEN)
    check(sol, frame, cam, pos=0.5, pa=0.1)


def test_guide_star_choice_on_ucac4(truth, tmp_path):
    catalog, _ = other_catalog(truth, tmp_path, "ucac4", [SPARSE])
    target = og.TargetPosition(*SPARSE, epoch=2000.0)
    kw = dict(fiber=(320.0, 240.0), sensor=(640, 480), plate_scale=0.5, pa=0.0, flip=False)
    when = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    ours, _ = og.choose_guide_star(catalog.cone(*SPARSE, 0.1, epoch=when), target, when, **kw)
    theirs, _ = og.choose_guide_star(truth.cone(*SPARSE, 0.1, epoch=when), target, when, **kw)
    assert ours is not None and theirs is not None
    # The same star (the estimated G may reorder near-equal candidates; it
    # must at least be a star of the truth within 0.1").
    t = truth.cone(*SPARSE, 0.1, epoch=when)
    assert cat.separation(ours.ra, ours.dec, t.ra, t.dec).min() * 3600 < 0.1


# -- refusing non-commercial data -----------------------------------------------------------


def test_allow_noncommercial_false_refuses_gaia(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Gaia's bright stars were read")

    monkeypatch.setattr(cat, "bright_stars", boom)
    monkeypatch.setattr(cat, "_default_bright", boom)
    for cfg in ({"allow_noncommercial": False}, {"source": "gaia", "allow_noncommercial": False}):
        with pytest.raises(cat.NonCommercialRefused, match="non-commercial use only"):
            cat.catalog_from_config({**cfg, "cache_dir": str(tmp_path)})
    with pytest.raises(cat.NonCommercialRefused):
        cat.TileCache(tmp_path, allow_noncommercial=False)
    with pytest.raises(cat.NonCommercialRefused):
        cat.Catalog(None, bright=None, live=cat.VizierSource(), allow_noncommercial=False)
    # A commercial-friendly source loads, and never touches Gaia's files:
    # not the bright stars, not the Hipparcos supplement next to them.
    (tmp_path / "hipparcos-not-in-gaia.npy").write_bytes(b"")
    c = cat.catalog_from_config(
        {"source": "ucac4", "allow_noncommercial": False, "cache_dir": str(tmp_path)}
    )
    assert c.source.name == "ucac4" and c.bright is None
    assert c.tiles.directory.name == "ucac4-g16-hpx7"
    assert cat.Catalog(None, allow_noncommercial=False).bright is None


def test_the_environment_is_a_floor(tmp_path, monkeypatch):
    monkeypatch.setenv(cat.NONCOMMERCIAL_ENV, "0")
    assert not cat.noncommercial_allowed(True)
    with pytest.raises(cat.NonCommercialRefused):
        cat.catalog_from_config({"allow_noncommercial": True, "cache_dir": str(tmp_path)})
    with pytest.raises(cat.NonCommercialRefused):
        cat.bright_stars()
    with pytest.raises(zenodo.AssetUnavailable, match="non-commercial"):
        zenodo.fetch_file(zenodo_assets.GAIA_EXTRACTS, cat.BRIGHT_FILE, root=tmp_path)
    cat.catalog_from_config({"source": "2mass", "cache_dir": str(tmp_path)})
    monkeypatch.setenv(cat.NONCOMMERCIAL_ENV, "1")
    assert cat.noncommercial_allowed(None) and not cat.noncommercial_allowed(False)


def test_config_errors(tmp_path):
    with pytest.raises(ValueError, match=r"catalog.source: catalog source 'tycho' is not one of"):
        cat.catalog_from_config({"source": "tycho", "cache_dir": str(tmp_path)})
    with pytest.raises(ValueError, match="live service is Gaia's"):
        cat.catalog_from_config({"source": "ucac4", "live": True, "cache_dir": str(tmp_path)})
    with pytest.raises(ValueError, match="allow_noncommercial: true or false"):
        cat.catalog_from_config({"allow_noncommercial": "no", "cache_dir": str(tmp_path)})


def test_a_plugin_registers_its_own_source(tmp_path):
    mine = cat.CatalogSource(
        name="mine",
        title="a site's own catalog",
        licence="the site's",
        noncommercial=False,
        licence_url="",
        epoch=2010.0,
        proper_motion=False,
        directory="mine-hpx{level}",
        bright_file="mine_bright.npy",
        magnitudes="G",
        fetcher=lambda url: None,
    )
    cat.CATALOG_SOURCES.register("mine", mine)
    try:
        c = cat.catalog_from_config({"source": "mine", "fetch": False, "cache_dir": str(tmp_path)})
        assert c.tiles.directory == tmp_path / "mine-hpx7" and c.source.epoch == 2010.0
        with pytest.raises(ValueError, match="registered twice"):
            cat.CATALOG_SOURCES.register("mine", mine)
    finally:
        cat.CATALOG_SOURCES.unregister("mine")


def test_every_source_states_its_licence():
    for name in cat.CATALOG_SOURCES:
        s = cat.catalog_source(name)
        assert s.licence and s.licence_url.startswith("https://")
    assert cat.catalog_source("gaia").noncommercial
    assert not cat.catalog_source("ucac4").noncommercial
    assert not cat.catalog_source("2mass").noncommercial
