"""MINERVA's target-list Sheet: rows to request groups, refusals, snapshots
and the fallback, the CLI.

Every list here is synthetic: the column headers are legacy's (the
``targets`` and ``B stars`` tabs as minerva-control reads them), the stars
and values are made up. gspread is never imported: a fake client stands in
for it, so nothing touches the network.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from robofast_core.dispatch import DispatchScheduler, TelescopeLimits
from robofast_core.request_model import load_request_groups
from robofast_core.sites.minerva import target_sheet as ms

DATA = Path(__file__).parent / "data" / "minerva_sheet"
SITE = Path(__file__).resolve().parents[2] / "sites" / "minerva"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def table(name: str) -> list[list[str]]:
    return ms.read_csv_text((DATA / f"{name}.csv").read_text())


def spec(name: str) -> ms.ListSpec:
    # The instrument is a site value (sites/minerva/dispatch.example.json).
    return replace(ms.DEFAULT_LISTS[name], instrument="spectrograph")


# -- rows to requests --------------------------------------------------------------


def test_targets_tab_fields():
    sl = ms.convert(table("targets"), spec("targets"), "targets.csv")
    assert [g["name"] for g in sl.groups] == ["SYN-ALPHA", "SYN BETA", "SYN-ZETA"]
    assert (sl.inactive, sl.blank) == (1, 1)  # the inactive row is not checked
    a, b, _ = sl.groups
    assert a["id"] == "RV-SYN-ALPHA" and b["id"] == "RV-SYN_BETA"
    assert a["proposal"] == "minerva-rv" and a["ipp_value"] == 5.0
    r = a["requests"][0]
    assert r["extra_params"]["cadence"] == {"min_interval": 7200.0, "max_per_night": 3}
    assert r["observation_note"] == "MINERVA sheet targets, row 2; V = 6.5; synthetic"
    c = r["configurations"][0]
    assert c["type"] == "SPECTRUM" and c["instrument_type"] == "spectrograph"
    assert c["instrument_configs"] == [
        {"exposure_time": 600.0, "exposure_count": 1, "extra_params": {"iodine": True}}
    ]
    assert c["acquisition_config"] == {"exposure_time": 1.5}
    assert c["target"] == {
        "type": "ICRS",
        "name": "SYN-ALPHA",
        "ra": 22.5,  # 1.5 h
        "dec": 20.0,
        "proper_motion_ra": 10.5,
        "proper_motion_dec": -3.25,
        "epoch": 2000.0,
        "parallax": 20.0,
        "extra_params": {"radial_velocity": -12.3},
    }
    # Blank seplimit / maxobs: legacy's defaults; blank parallax, rv, fauexptime: absent.
    rb = b["requests"][0]
    assert rb["extra_params"]["cadence"] == {"min_interval": 10800.0, "max_per_night": 2}
    tb = rb["configurations"][0]["target"]
    assert "parallax" not in tb and "extra_params" not in tb
    assert "acquisition_config" not in rb["configurations"][0]
    assert rb["observation_note"] == "MINERVA sheet targets, row 3; V = 6.5"
    load_request_groups(sl.groups)  # valid under the request model


def test_bad_rows_are_refused_with_row_and_reason():
    sl = ms.convert(table("targets"), spec("targets"), "targets.csv")
    refused = "\n".join(sl.refused)
    assert len(sl.refused) == 7, refused
    for row, why in [
        (6, "pmra is blank"),
        (7, "iodine 0: legacy ignored this column"),
        (9, "name 'SYN-ALPHA' already on row 2"),
        (10, "radeg 136.0 disagrees with ra 9.0 h x 15 by 3600.0 arcsec"),
        (11, "active 'yes': expected 1 or 0"),
        (12, "priority 0: must be > 0"),
        (13, "dec (J2000 deg) '95.0' is outside [-90, 90]"),
    ]:
        assert f"targets row {row}: {why}" in refused


def test_acquisition_offsets_become_offset_guiding():
    # Row 8: acquisition_offset_north 5, east 0 -- the target is 5" north of
    # the star to guide on (legacy's convention), so the guide star is 5"
    # south of the target. (Legacy applied an offset only when both were
    # non-zero; robofast takes either.)
    sl = ms.convert(table("targets"), spec("targets"), "targets.csv")
    zeta = next(g for g in sl.groups if g["name"] == "SYN-ZETA")
    c = zeta["requests"][0]["configurations"][0]
    assert c["guiding_config"] == {
        "mode": "ON",
        "extra_params": {"guide": "offset", "offset_star": {"east": 0.0, "north": -5.0}},
    }
    alpha = next(g for g in sl.groups if g["name"] == "SYN-ALPHA")
    assert alpha["requests"][0]["configurations"][0]["guiding_config"] == {"mode": "ON"}
    [c] = load_request_groups([zeta])[0].requests[0].configurations
    assert c.guide_on == "offset" and c.offset_star["north"] == -5.0


def test_bstars_share_one_visit_a_night():
    sl = ms.convert(table("bstars"), spec("bstars"))
    assert [g["id"] for g in sl.groups] == ["BSTAR-SYN-B1", "BSTAR-SYN-B2"]
    assert sl.inactive == 1 and not sl.refused
    for g in sl.groups:
        r = g["requests"][0]
        assert g["proposal"] == "iodine-calibration"
        # B stars: the maxobs column is not used; one of the pool a night.
        assert r["extra_params"]["cadence"] == {
            "min_interval": 10800.0,
            "max_per_night": 1,
            "pool": "bstars",
            "limit": "soft",  # a second B star rather than idle (legacy)
        }
        ic = r["configurations"][0]["instrument_configs"][0]
        assert ic["extra_params"] == {"iodine": True}
    groups = load_request_groups(sl.groups)
    d = DispatchScheduler(
        groups,
        ms_site(),
        {"t1": TelescopeLimits(instruments=["spectrograph"], min_alt=21, max_alt=83)},
        instruments={"spectrograph": {"type": "kiwispec"}},
    )
    assert all(r.cadence.pool == "bstars" for r in d.requests.values())
    assert all(r.cadence.limit == "soft" for r in d.requests.values())
    # A list may keep the hard limit.
    hard = ms.convert(table("bstars"), replace(spec("bstars"), limit="hard"))
    assert "limit" not in hard.groups[0]["requests"][0]["extra_params"]["cadence"]
    with pytest.raises(ms.SheetError, match="limit 'maybe'"):
        replace(spec("bstars"), limit="maybe")


def ms_site():
    from robofast_core import sky

    return sky.Site(31.680407, -110.878977, 2316.0)


def test_header_problems_refuse_the_tab():
    rows = table("targets")
    no_pm = [[c for i, c in enumerate(r) if i != 4] for r in rows]  # drop pmra
    with pytest.raises(ms.SheetError, match=r"missing columns \['pmra'\]"):
        ms.convert(no_pm, spec("targets"))
    twice = [[*r, r[13]] for r in rows]  # a second 'priority'
    with pytest.raises(ms.SheetError, match="column 'priority' appears twice"):
        ms.convert(twice, spec("targets"))
    extra = [[*rows[0], "new column (units)"], *[[*r, "x"] for r in rows[1:]]]
    sl = ms.convert(extra, spec("targets"))
    assert any("ignored: ['new column']" in w for w in sl.warnings)
    with pytest.raises(ms.SheetError, match="empty"):
        ms.convert([], spec("targets"))


def test_cell_values_keep_full_precision():
    assert ms._cell_text(1.0) == "1"
    assert ms._cell_text(1.2345678901234) == "1.2345678901234"
    assert ms._cell_text(None) == "" and ms._cell_text(True) == "TRUE"
    assert ms._cell_text(7) == "7" and ms._cell_text(" a ") == " a "


# -- config ------------------------------------------------------------------------


def test_site_example_config_loads():
    cfg = ms.SheetConfig.load(SITE / "target_sheet.example.json")
    # The red-side list is retired: not in the example at all.
    assert set(cfg.lists) == {"targets", "bstars"} == set(cfg.enabled())
    assert cfg.lists["bstars"].limit == "soft"
    assert cfg.lists["bstars"].kind == "bstars" and cfg.lists["bstars"].tab == "B stars"
    assert cfg.key_file == Path("/etc/robofast/secrets/minerva_sheets.json")
    # The public example carries placeholders only, and fetch refuses them.
    assert all(ms._placeholder(s.sheet) for s in cfg.lists.values())
    with pytest.raises(ms.SheetError, match="placeholder"):
        ms.fetch(cfg, client=FakeClient({}))


def test_config_errors(tmp_path):
    with pytest.raises(ms.SheetError, match="unknown keys"):
        ms.SheetConfig.from_dict({"lists": {"t": {"kind": "targets", "tab": "x"}}, "bogus": 1})
    with pytest.raises(ms.SheetError, match="kind 'red'"):
        ms.SheetConfig.from_dict({"lists": {"t": {"kind": "red", "tab": "x", **INSTRUMENT}}})
    with pytest.raises(ms.SheetError, match="'kind', 'instrument' required"):
        ms.SheetConfig.from_dict({"lists": {"t": {"tab": "x"}}})
    with pytest.raises(ms.SheetError, match="no instrument"):
        ms.convert(table("bstars"), ms.DEFAULT_LISTS["bstars"])


def test_key_path_from_the_environment(monkeypatch):
    cfg = config(Path("/nonexistent"))
    monkeypatch.delenv(ms.KEY_ENV, raising=False)
    monkeypatch.setenv(ms.GOOGLE_KEY_ENV, "/google/k.json")
    assert cfg.key_path() == Path("/etc/robofast/secrets/k.json")  # config over Google's
    monkeypatch.setenv(ms.KEY_ENV, "/elsewhere/k.json")
    assert cfg.key_path() == Path("/elsewhere/k.json")
    monkeypatch.delenv(ms.KEY_ENV)
    cfg = replace(cfg, key_file=None)
    assert cfg.key_path() == Path("/google/k.json")
    monkeypatch.delenv(ms.GOOGLE_KEY_ENV)
    assert cfg.key_path() is None


def dummy_key(where: Path, mode: int = 0o600) -> Path:
    """A stand-in key file: not a key, only its metadata is checked."""
    where.mkdir(parents=True, exist_ok=True)
    key = where / "dummy-sa.json"
    key.write_text('{"note": "not a key"}')
    key.chmod(mode)
    return key


def test_a_key_others_can_read_is_refused(tmp_path):
    ok = dummy_key(tmp_path / "ok")
    assert ms.check_key_file(ok) == ok.resolve()
    for mode in (0o640, 0o604, 0o620, 0o644):
        with pytest.raises(ms.KeyFileRefused, match="chmod 600"):
            ms.check_key_file(dummy_key(tmp_path / f"m{mode:o}", mode))
    with pytest.raises(ms.SheetUnavailable, match="not found"):
        ms.check_key_file(tmp_path / "missing.json")


def test_a_key_inside_a_git_work_tree_is_refused(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    key = dummy_key(repo / "sites" / "minerva")
    with pytest.raises(ms.KeyFileRefused, match="inside the git work tree"):
        ms.check_key_file(key)
    # A worktree's .git is a file; a symlink from outside does not hide it.
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: elsewhere\n")
    inside = dummy_key(wt)
    link = tmp_path / "outside" / "k.json"
    link.parent.mkdir()
    link.symlink_to(inside)
    with pytest.raises(ms.KeyFileRefused, match="git work tree"):
        ms.check_key_file(link)
    # $CREDENTIALS_DIRECTORY (systemd LoadCredential=) is expanded.
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "creds"))
    dummy_key(tmp_path / "creds")
    assert ms.check_key_file(Path("$CREDENTIALS_DIRECTORY/dummy-sa.json")).name == "dummy-sa.json"


def test_a_refused_key_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    # Even with a snapshot to fall back to, a world-readable key stops the
    # load: the config is wrong, and silently serving old lists hides it.
    cfg = config(tmp_path)
    ms.load(cfg, offline=False, client=live_client(), now=NOW)
    monkeypatch.delenv(ms.KEY_ENV, raising=False)
    cfg = replace(cfg, key_file=dummy_key(tmp_path / "keys", 0o644))
    with pytest.raises(ms.KeyFileRefused, match="group or others"):
        ms.load(cfg, offline=False, now=NOW)


# -- fetching (a fake gspread) -----------------------------------------------------


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_all_values(self, **kw):
        self.calls.append(kw)
        return self.rows


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, title):
        if title not in self.tabs:
            raise LookupError(f"no worksheet {title!r}")
        return self.tabs[title]


class FakeClient:
    def __init__(self, books, fail: Exception | None = None):
        self.books, self.fail, self.opened = books, fail, []

    def open_by_key(self, key):
        self.opened.append(key)
        if self.fail is not None:
            raise self.fail
        return self.books[key]


SHEET = "synthetic-sheet-key-0123456789"
INSTRUMENT = {"instrument": "spectrograph"}


def config(tmp: Path, **kw) -> ms.SheetConfig:
    # The offset check has tests of its own (below); here it is off, so a
    # live load never reaches for a star catalog.
    kw.setdefault("offset_check", False)
    d = {
        "key_file": "/etc/robofast/secrets/k.json",
        "snapshot_dir": str(tmp / "snaps"),
        "lists": {
            "targets": {"sheet": SHEET, "tab": "targets", "kind": "targets", "id_prefix": "RV"}
            | INSTRUMENT,
            "bstars": {"sheet": SHEET, "tab": "B stars", "kind": "bstars", "id_prefix": "BSTAR"}
            | INSTRUMENT,
        },
        **kw,
    }
    return ms.SheetConfig.from_dict(d)


def live_client() -> FakeClient:
    # Unformatted values, as the Sheets API returns them: numbers as numbers.
    def typed(rows):
        out = [rows[0]]
        for r in rows[1:]:
            new = []
            for c in r:
                try:
                    new.append(float(c) if "." in c else int(c))
                except ValueError:
                    new.append(c)
            out.append(new)
        return out

    book = FakeBook(
        {
            "targets": FakeWorksheet(typed(table("targets"))),
            "B stars": FakeWorksheet(typed(table("bstars"))),
        }
    )
    return FakeClient({SHEET: book})


def test_fetch_reads_unformatted_values_once_per_sheet(tmp_path):
    client = live_client()
    tables = ms.fetch(config(tmp_path), client=client)
    assert client.opened == [SHEET]  # both tabs from one open
    ws = client.books[SHEET].tabs["targets"]
    assert ws.calls == [{"value_render_option": "UNFORMATTED_VALUE"}]
    assert tables["targets"][1][:4] == ["SYN-ALPHA", "1.5", "22.5", "20"]


def test_fetch_errors_never_carry_the_sheet_key(tmp_path):
    client = FakeClient({}, fail=RuntimeError(f"403 for spreadsheets/{SHEET}/values"))
    with pytest.raises(ms.SheetUnavailable) as exc:
        ms.fetch(config(tmp_path), client=client)
    assert SHEET not in str(exc.value) and "<sheet>" in str(exc.value)


def test_client_uses_the_read_only_scope(tmp_path, monkeypatch):
    seen = {}
    gspread = types.ModuleType("gspread")
    gspread.authorize = lambda creds: seen.setdefault("client", ("client", creds))
    sa = types.ModuleType("google.oauth2.service_account")

    class Credentials:
        @staticmethod
        def from_service_account_file(path, scopes):
            seen["path"], seen["scopes"] = path, scopes
            return "creds"

    sa.Credentials = Credentials
    monkeypatch.setitem(sys.modules, "gspread", gspread)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", sa)
    key = tmp_path / "key.json"
    key.write_text("{}")
    key.chmod(0o600)
    assert ms._client(key) == ("client", "creds")
    assert seen["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    with pytest.raises(ms.SheetUnavailable, match="not found"):
        ms._client(tmp_path / "missing.json")


def test_client_without_gspread(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "gspread", None)
    with pytest.raises(ms.SheetUnavailable, match="pip install --group sheets"):
        ms._client(dummy_key(tmp_path))


# -- snapshots and the fallback ----------------------------------------------------


def test_live_load_writes_a_snapshot(tmp_path, caplog):
    cfg = config(tmp_path)
    with caplog.at_level(logging.INFO, logger=ms.__name__):
        loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW)
    assert loaded.live and loaded.path == tmp_path / "snaps" / "20261101T120000Z"
    assert f"MINERVA target lists from {loaded.path}" in caplog.text
    assert sorted(p.name for p in loaded.path.iterdir()) == [
        "bstars.csv",
        "meta.json",
        "targets.csv",
    ]
    meta = json.loads((loaded.path / "meta.json").read_text())
    assert meta["lists"]["targets"]["rows"] == 12 and SHEET not in json.dumps(meta)
    assert len(loaded.groups()) == 5 and len(loaded.refused()) == 7
    if os.name == "posix":
        assert loaded.path.stat().st_mode & 0o077 == 0
    # The snapshot reads back to the same requests.
    fetched, tables = ms.read_snapshot(loaded.path)
    assert fetched == NOW
    again = ms.convert_tables(tables, cfg.enabled())
    assert [g for sl in again.values() for g in sl.groups] == loaded.groups()


def test_unreachable_sheet_falls_back_to_the_newest_snapshot(tmp_path, caplog):
    cfg = config(tmp_path)
    ms.load(cfg, offline=False, client=live_client(), now=NOW - timedelta(days=3))
    newest = ms.load(cfg, offline=False, client=live_client(), now=NOW - timedelta(hours=5))
    down = FakeClient({}, fail=ConnectionError("network unreachable"))
    with caplog.at_level(logging.INFO, logger=ms.__name__):
        loaded = ms.load(cfg, offline=False, client=down, now=NOW)
    assert not loaded.live and loaded.path == newest.path
    assert "could not be read (ConnectionError: network unreachable)" in caplog.text
    assert "(5.0 h old)" in caplog.text
    assert f"MINERVA target lists from {newest.path}" in caplog.text
    assert loaded.groups() == newest.groups()
    # Offline: no fetch is even tried.
    loaded = ms.load(cfg, offline=True, client=down, now=NOW + timedelta(days=4))
    assert loaded.path == newest.path and down.opened == ["synthetic-sheet-key-0123456789"]


def test_no_sheet_and_no_snapshot(tmp_path):
    down = FakeClient({}, fail=ConnectionError("down"))
    with pytest.raises(ms.SheetUnavailable, match="no snapshot under"):
        ms.load(config(tmp_path), offline=False, client=down, now=NOW)


def test_snapshots_are_pruned_and_checked(tmp_path):
    cfg = config(tmp_path, keep=2)
    for h in range(4):
        ms.load(cfg, offline=False, client=live_client(), now=NOW + timedelta(hours=h))
    snaps = ms._snapshots(tmp_path / "snaps")
    assert [p.name for p in snaps] == ["20261101T140000Z", "20261101T150000Z"]
    # Same second twice: a suffix, still newest.
    p = ms.write_snapshot(
        {"targets": [["name"]]}, cfg.lists, tmp_path / "snaps", now=NOW + timedelta(hours=3), keep=5
    )
    assert p.name == "20261101T150000Z-1" and ms.newest_snapshot(tmp_path / "snaps") == p
    (p / "targets.csv").write_text("tampered\n")
    with pytest.raises(ms.SheetError, match="does not match its digest"):
        ms.read_snapshot(p)
    # A half-written snapshot (no meta.json) is never picked.
    (tmp_path / "snaps" / "20991231T000000Z").mkdir()
    assert ms.newest_snapshot(tmp_path / "snaps") == p


# -- command line ------------------------------------------------------------------


def test_cli_check_and_export_from_csv(tmp_path, capsys):
    t, b = f"targets={DATA / 'targets.csv'}", f"bstars={DATA / 'bstars.csv'}"
    assert ms.main(["check", "--csv", b]) == 2  # no instrument: a site value
    assert "no instrument" in capsys.readouterr().err
    inst = ["--instrument", "spectrograph"]
    inst += ["--no-offset-check"]
    assert ms.main(["check", *inst, "--csv", t, "--csv", b]) == 2  # refused rows
    err = capsys.readouterr().err
    assert "refused: targets row 6: pmra is blank" in err
    assert "targets: 3 requests, 1 inactive, 7 refused" in err
    assert "bstars: 2 requests, 1 inactive, 0 refused" in err
    out = tmp_path / "requests.json"
    assert ms.main(["export", *inst, "--csv", t, "--csv", b, "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert [g["id"] for g in doc["groups"]] == [
        "RV-SYN-ALPHA",
        "RV-SYN_BETA",
        "RV-SYN-ZETA",
        "BSTAR-SYN-B1",
        "BSTAR-SYN-B2",
    ]
    assert len(load_request_groups(str(out))) == 5
    assert ms.main(["export", *inst, "--csv", t, "--strict"]) == 2
    assert ms.main(["check", *inst, "--csv", b]) == 0
    assert ms.main(["check", *inst, "--csv", f"nope={DATA / 'bstars.csv'}"]) == 2
    assert ms.main(["check"]) == 2


def test_cli_offline_and_snapshot(tmp_path, capsys, monkeypatch):
    cfg = config(tmp_path)
    loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW)
    cfile = tmp_path / "sheet.json"
    raw = {
        "snapshot_dir": str(tmp_path / "snaps"),
        "lists": {
            k: {"kind": v.kind, "tab": v.tab, "sheet": SHEET, "id_prefix": v.id_prefix} | INSTRUMENT
            for k, v in cfg.lists.items()
        },
    }
    cfile.write_text(json.dumps(raw))
    assert ms.main(["check", "--config", str(cfile), "--offline"]) == 2
    out = capsys.readouterr().out
    assert f"fallback snapshot {loaded.path}" in out
    snap = ["--instrument", "spectrograph", "--no-offset-check", "--snapshot", str(loaded.path)]
    assert ms.main(["check", *snap]) == 2
    assert f"snapshot {loaded.path}" in capsys.readouterr().out
    # fetch with no key file: an error, no fallback, exit 2.
    monkeypatch.delenv(ms.KEY_ENV, raising=False)
    monkeypatch.delenv(ms.GOOGLE_KEY_ENV, raising=False)
    assert ms.main(["fetch", "--config", str(cfile)]) == 2
    assert "no service-account key" in capsys.readouterr().err


# -- acquisition offsets against the catalog ----------------------------------------

from robofast_core import catalog as cat  # noqa: E402
from robofast_core.guider.offset import OffsetCheck, TargetPosition  # noqa: E402

#: SYN-ZETA (row 8): its guide star is 5" south of it.
ZETA = TargetPosition(105.0, 32.0, 10.5, -3.25, 2000.0)


def star_catalog(tmp: Path, stars: list[tuple[float, float, float]]) -> cat.Catalog:
    """A catalog of stars at (east, north) arcsec from SYN-ZETA at J2016,
    each of the given G and moving with it, on tiles in ``tmp``."""
    ra0, dec0 = ZETA.at(cat.GAIA_EPOCH)
    rec = np.zeros(len(stars), cat.TILE_DTYPE)
    for i, (e, n, g) in enumerate(stars):
        ra, dec = cat._untan(ra0, dec0, np.radians(e / 3600.0), np.radians(n / 3600.0))
        rec[i] = (i + 1, ra, dec, ZETA.pmra, ZETA.pmdec, g, 0.8)
    tiles = cat.TileCache(tmp, fetch=None)
    pix = cat.ang2pix(tiles.level, rec["ra"], rec["dec"])
    for p in cat.tiles_in_cone(tiles.level, ra0, dec0, 0.1):
        tiles.save(p, rec[pix == p])
    return cat.Catalog(tiles, bright=None)


def checked(catalog, check=None) -> ms.SheetList:
    lists = {"targets": ms.convert(table("targets"), spec("targets"), "targets.csv")}
    ms.verify_offsets(lists, catalog, NOW, check or OffsetCheck())
    return lists["targets"]


def zeta_star(sl: ms.SheetList) -> dict | None:
    g = next((g for g in sl.groups if g["name"] == "SYN-ZETA"), None)
    if g is None:
        return None
    return g["requests"][0]["configurations"][0]["guiding_config"]["extra_params"]["offset_star"]


def test_offset_with_its_star_in_the_catalog_is_verified(tmp_path):
    # The star 0.4" from where the sheet says (a legacy offset is rounded),
    # with a brighter one 20" away that must not be taken instead.
    sl = checked(star_catalog(tmp_path, [(0.3, -5.3, 11.0), (15.0, 12.0, 9.0)]))
    assert zeta_star(sl) == {"east": 0.0, "north": -5.0, "g": 11.0}
    assert sl.offsets_ok == 1 and sl.offsets_failed == 0
    assert "acquisition offsets: 1 verified, 0 failed, 0 not checked" in sl.summary()
    assert not any("SYN-ZETA" in r for r in sl.refused)


def test_offset_with_no_star_is_refused_and_says_where_the_nearest_is(tmp_path):
    sl = checked(star_catalog(tmp_path, [(12.0, -5.0, 11.0)]))
    assert zeta_star(sl) is None and sl.offsets_failed == 1
    [r] = [r for r in sl.refused if "SYN-ZETA" in r]
    assert r.startswith("targets row 8 (SYN-ZETA): acquisition offset: no catalog star within 3")
    assert 'the nearest (mag 11.0) is 12.0" away, at east +12.0" north -5.0"' in r
    assert "1 failed" in sl.summary()


@pytest.mark.parametrize(
    ("g", "why"), [(15.5, "too faint to guide on"), (6.0, "saturates the guider")]
)
def test_offset_star_of_the_wrong_brightness_is_refused(tmp_path, g, why):
    sl = checked(star_catalog(tmp_path, [(0.0, -5.0, g)]))
    assert zeta_star(sl) is None
    assert any(why in r and "SYN-ZETA" in r for r in sl.refused)


def test_flag_keeps_the_row_with_a_warning(tmp_path):
    sl = checked(star_catalog(tmp_path, []), OffsetCheck(action="flag"))
    assert zeta_star(sl) == {"east": 0.0, "north": -5.0}
    assert any("SYN-ZETA" in w and "kept: offset_check.action flag" in w for w in sl.warnings)
    assert sl.offsets_failed == 1 and not any("SYN-ZETA" in r for r in sl.refused)


def test_proper_motion_is_applied(tmp_path):
    # A guide star that does not share the target's motion: right in 2016,
    # 0.3"/yr off by 2026, so 3" off now: the check uses the date.
    ra0, dec0 = ZETA.at(cat.GAIA_EPOCH)
    ra, dec = cat._untan(ra0, dec0, 0.0, np.radians(-5.0 / 3600.0))
    rec = np.zeros(1, cat.TILE_DTYPE)
    rec[0] = (1, ra, dec, ZETA.pmra + 350.0, ZETA.pmdec, 11.0, 0.8)
    tiles = cat.TileCache(tmp_path, fetch=None)
    for p in cat.tiles_in_cone(tiles.level, ra0, dec0, 0.1):
        tiles.save(p, rec[cat.ang2pix(tiles.level, rec["ra"], rec["dec"]) == p])
    catalog = cat.Catalog(tiles, bright=None)
    assert checked(catalog).offsets_failed == 1
    assert checked(catalog, OffsetCheck(tolerance=5.0)).offsets_ok == 1


def test_a_catalog_that_cannot_answer_is_a_warning_not_a_refusal(tmp_path):
    empty = cat.Catalog(cat.TileCache(tmp_path, fetch=None), bright=None)
    sl = checked(empty)
    assert zeta_star(sl) == {"east": 0.0, "north": -5.0}
    assert sl.offsets_unchecked == 1
    assert any("SYN-ZETA" in w and "not checked" in w for w in sl.warnings)


def test_load_checks_offsets_and_the_config_turns_it_off(tmp_path):
    good = star_catalog(tmp_path / "cat", [(0.0, -5.0, 11.0)])
    cfg = config(tmp_path, offset_check={"tolerance": 2.0, "action": "refuse"})
    assert cfg.offset_check == OffsetCheck(tolerance=2.0)
    loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW, catalog=good)
    assert loaded.lists["targets"].offsets_ok == 1
    empty = cat.Catalog(cat.TileCache(tmp_path / "none", fetch=None), bright=None)
    loaded = ms.load(cfg, offline=False, client=live_client(), now=NOW, catalog=empty)
    assert loaded.lists["targets"].offsets_unchecked == 1
    off = ms.load(config(tmp_path), offline=False, client=live_client(), now=NOW, catalog=empty)
    assert off.lists["targets"].offsets_unchecked == 0
    for bad in ({"action": "ignore"}, {"tolerance": 0}, {"nope": 1}, "yes"):
        with pytest.raises(ms.SheetError, match="offset_check"):
            config(tmp_path, offset_check=bad)


def test_cli_checks_offsets_against_a_catalog_directory(tmp_path, capsys):
    star_catalog(tmp_path, [(0.0, -5.0, 11.0)])
    t = f"targets={DATA / 'targets.csv'}"
    args = ["check", "--instrument", "spectrograph", "--catalog", str(tmp_path), "--csv", t]
    assert ms.main(args) == 2  # the sheet's other refused rows
    assert "acquisition offsets: 1 verified, 0 failed" in capsys.readouterr().err
