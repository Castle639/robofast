"""Offset guiding (docs/offset_guiding.md): the geometry (proper motion,
refraction, field rotation), choosing a guide star, and the guide loop and
service holding a faint target on the fiber by a brighter star nearby."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from robofast.catalog import Stars
from robofast.guider import offset as og
from robofast.guider.base import VirtualClock
from robofast.guider.calibration import Calibration, fits_sky_matrix
from robofast.guider.loop import GuideConfig, Guider
from robofast.guider.service import GuiderRemote, GuiderService, GuiderServiceConfig
from robofast.guider.sim import Fiber, Jitter, SimCamera, SimTipTilt, SimWorld
from robofast.registry import ValueRegistry

WHEN = datetime(2026, 11, 15, 7, 0, tzinfo=UTC)
#: A generic site for the tests (core takes no site values; design.md 3.10).
SITE = og.OffsetSite(latitude=31.7, longitude=-110.9, elevation=2300.0)
FIBER = (320.0, 240.0)
SCALE, PA = 0.5, 30.0


# -- geometry -------------------------------------------------------------------------


def test_pixel_offset_inverts_the_sky_matrix():
    for pa, flip in ((0.0, False), (30.0, False), (123.0, True)):
        px = og.pixel_offset(12.0, -7.0, 0.3, pa, flip)
        np.testing.assert_allclose(fits_sky_matrix(0.3, pa, flip) @ px, [12.0, -7.0])
    # PA 0, no flip: north is +y, east is -x.
    np.testing.assert_allclose(og.pixel_offset(3.0, 4.0, 1.0, 0.0, False), [-3.0, 4.0])


def test_sky_offset_is_a_true_angle():
    # 30" east at dec 60: 60" of RA, but 30" on the sky.
    e, n = og.sky_offset(150.0, 60.0, 150.0 + 30.0 / 3600.0 / math.cos(math.radians(60)), 60.0)
    assert e == pytest.approx(30.0, abs=1e-3) and n == pytest.approx(0.0, abs=0.01)


def test_proper_motion_moves_the_offset():
    target = og.TargetPosition(10.0, 20.0, pmra=0.0, pmdec=0.0, epoch=2016.0)
    # A star 40" north with 1"/yr of proper motion north: 10.9 yr later
    # (2026.9) it is ~50.9" north.
    star = og.GuideStar("coordinates", 10.0, 20.0 + 40.0 / 3600.0, 0.0, 1000.0, 2016.0)
    e, n = star.offset_from(target, WHEN)
    assert n == pytest.approx(40.0 + (og.decimal_year(WHEN) - 2016.0), abs=0.01)
    assert e == pytest.approx(0.0, abs=1e-3)
    # An explicit offset moves with the target.
    fixed = og.GuideStar("offset", east=5.0, north=-6.0)
    assert fixed.offset_from(target, WHEN) == (5.0, -6.0)
    ra, dec = fixed.radec(target, WHEN)
    assert og.sky_offset(10.0, 20.0, ra, dec) == pytest.approx((5.0, -6.0), abs=1e-3)


def test_differential_refraction_across_the_offset():
    # At 30 deg altitude, a guide star 60" higher than the target is lifted
    # ~0.1" less (dR/dz = A sec^2 z ~ 57" x 4 per radian ~ 3.4e-4 x 4).
    up = (0.0, 60.0)  # parallactic angle 0: the zenith is north
    de, dn = og.refraction_shift(*up, alt=30.0, parallactic=0.0, site=SITE)
    assert de == pytest.approx(0.0, abs=1e-6)
    assert -0.15 < dn < -0.05
    # Across the vertical the offset shrinks by A (~2e-4 at 2300 m).
    de, dn = og.refraction_shift(60.0, 0.0, alt=30.0, parallactic=0.0, site=SITE)
    assert -0.02 < de < -0.005 and dn == pytest.approx(0.0, abs=1e-6)
    # Near the zenith and for a small offset it is negligible.
    assert max(map(abs, og.refraction_shift(10.0, 10.0, 85.0, 40.0, SITE))) < 0.005
    # Rotating the parallactic angle rotates the shift.
    a = np.array(og.refraction_shift(0.0, 60.0, 30.0, 0.0, SITE))
    b = np.array(og.refraction_shift(60.0, 0.0, 30.0, 90.0, SITE))
    np.testing.assert_allclose(b, [a[1], a[0]], atol=1e-9)


def test_offset_guide_solution_and_field_rotation():
    target = og.TargetPosition(30.0, 25.0)
    star = og.GuideStar("offset", east=20.0, north=10.0)
    fixed = og.OffsetGuide(target, star, SCALE, PA, site=SITE, refraction=False)
    sol = fixed.solve(WHEN)
    np.testing.assert_allclose(sol["pixels"], og.pixel_offset(20.0, 10.0, SCALE, PA, False))
    assert "refraction" in sol and sol["alt"] > 0  # computed and logged even when off
    corrected = og.OffsetGuide(target, star, SCALE, PA, site=SITE).solve(WHEN)
    assert corrected["pixels"] != pytest.approx(sol["pixels"], abs=1e-6)
    # Without a derotator the PA follows the parallactic angle.
    rot = og.OffsetGuide(target, star, SCALE, PA, site=SITE, field_rotation="altaz", sense=-1)
    first = rot.solve(WHEN)
    later = rot.solve(WHEN + timedelta(minutes=30))
    dq = (later["parallactic"] - first["parallactic"] + 180) % 360 - 180
    assert first["pa"] == pytest.approx(PA)
    assert later["pa"] == pytest.approx((PA - dq) % 360.0, abs=1e-3) and abs(dq) > 1.0
    with pytest.raises(ValueError, match="needs the site"):
        og.OffsetGuide(target, star, SCALE, PA, field_rotation="altaz")
    with pytest.raises(ValueError, match="plate scale"):
        og.OffsetGuide(target, star, 0.0, PA)


def test_guide_star_specs():
    assert og.guide_star_from_spec("auto") is None
    s = og.guide_star_from_spec({"east": -3, "north": 4.5})
    assert (s.source, s.east, s.north) == ("offset", -3.0, 4.5)
    s = og.guide_star_from_spec({"ra": 1.5, "dec": -2, "pmra": 10, "epoch": 2016, "g": 9.1})
    assert (s.source, s.ra, s.pmra, s.epoch, s.g) == ("coordinates", 1.5, 10.0, 2016.0, 9.1)
    for bad in ("nearest", {"x": 1}, {"east": 1e5, "north": 0}):
        with pytest.raises(ValueError, match="offset star"):
            og.guide_star_from_spec(bad)


# -- choosing a guide star -----------------------------------------------------------


def _stars(rows) -> Stars:
    """rows: (east", north", G) from the target at (30, 25), Gaia epoch."""
    target = (30.0, 25.0)
    ra, dec = [], []
    from robofast.catalog import _untan

    for e, n, _ in rows:
        r, d = _untan(*target, math.radians(e / 3600), math.radians(n / 3600))
        ra.append(float(r))
        dec.append(float(d))
    k = len(rows)
    return Stars(
        np.array(ra),
        np.array(dec),
        np.zeros(k),
        np.zeros(k),
        np.array([g for _, _, g in rows], float),
        np.full(k, 0.8),
        np.arange(1, k + 1, dtype=np.int64),
        2016.0,
    )


def test_choose_the_best_guide_star():
    target = og.TargetPosition(30.0, 25.0, epoch=2016.0)
    stars = _stars(
        [
            (0.0, 0.0, 15.5),  # the faint target itself
            (20.0, 5.0, 6.0),  # saturates
            (-25.0, 10.0, 10.0),  # crowded: a G=11 star 3" away
            (-27.0, 11.5, 11.0),
            (40.0, -30.0, 10.5),  # good
            (15.0, 15.0, 13.5),  # closer but fainter
            (400.0, 0.0, 9.0),  # off the camera
        ]
    )
    kw = dict(fiber=FIBER, sensor=(640, 480), plate_scale=SCALE, pa=PA, flip=False)
    star, notes = og.choose_guide_star(stars, target, WHEN, **kw)
    assert star is not None and star.source == "catalog" and star.source_id == 5
    assert star.g == 10.5
    assert any("saturates" in n for n in notes) and any("not isolated" in n for n in notes)
    # Raising the bar on brightness picks the closer, fainter star.
    star, _ = og.choose_guide_star(
        stars, target, WHEN, config=og.AutoConfig(g_good=14.0, separation_scale=10.0), **kw
    )
    assert star.source_id == 6
    # Nothing usable: None and the reasons.
    star, notes = og.choose_guide_star(_stars([(20.0, 5.0, 6.0)]), target, WHEN, **kw)
    assert star is None and notes


# -- the guide loop -------------------------------------------------------------------


class _Rotating:
    """An OffsetGuide stand-in whose guide star turns about the fiber at
    ``rate`` deg/s (field rotation), from ``offset`` pixels at t0."""

    def __init__(self, offset, rate, t0, update=1.0):
        self.offset, self.rate, self.t0, self.update = np.asarray(offset, float), rate, t0, update

    def solve(self, when):
        a = math.radians(self.rate * (when - self.t0).total_seconds())
        c, s = math.cos(a), math.sin(a)
        px = [c * self.offset[0] - s * self.offset[1], s * self.offset[0] + c * self.offset[1]]
        return {"east": 0.0, "north": 0.0, "pa": 0.0, "pixels": px}

    def describe(self):
        return {"star": {"source": "offset"}}


def _offset_rig(clock, offset_px, *, drift=(0.2, -0.15), rotation=None, target_flux=300.0):
    """A TRES-like faint target on the fiber (a few hundred e-/s spill
    against the sky: too faint to centroid) and a bright guide star at
    ``offset_px`` from it, the image drifting at ``drift`` px/s."""
    tt = SimTipTilt(clock=clock, limits=((-200.0, 200.0), (-200.0, 200.0)))
    star = (FIBER[0] + offset_px[0], FIBER[1] + offset_px[1])
    world = SimWorld(
        clock=clock,
        star=star,
        flux=2e5,
        others=[(FIBER[0], FIBER[1], target_flux)],
        jitter=Jitter(rms=0.3, knee=1.0, drift=drift, seed=11),
        fiber=Fiber(*FIBER, radius=3.0),
        tiptilt=tt,
        tt_matrix=np.array([[0.08, 0.03], [-0.03, 0.08]]),
        plate_scale=SCALE,
        pa=PA,
        rotation=rotation,
        sky=50.0,
    )
    cfg = GuideConfig(
        target=FIBER,
        acq_roi=(0, 0, 640, 480),
        acq_exposure=0.2,
        exposure=0.05,
        centroider={"kind": "annulus"},
        fwhm=4.0,
        scope="t1",
        plate_scale=SCALE,
    )
    cal = Calibration("simtt", "fiber", world.tt_matrix.tolist())
    g = Guider(SimCamera(world), cfg, tiptilt=tt, tt_cal=cal, clock=clock)
    return g, world


async def _run(g, clock, seconds):
    g.start()
    await g.wait_state(("guiding", "failed"), 60)
    assert g.state == "guiding", g.reason
    t0 = clock.now()
    while clock.now() < t0 + seconds:
        await clock.sleep(0.25)
    return t0


async def test_offset_guiding_holds_a_faint_target_on_the_fiber_under_drift():
    """The simulated e2e: a faint TRES-like target, guided by a star 25"
    east and 15" north of it, stays on the fiber while the image drifts by
    ~4 px (2") over 20 s; the guide star sits at its setpoint, off the
    fiber, and the annulus centroider is not used on it."""
    clock = VirtualClock()
    target = og.TargetPosition(30.0, 25.0)
    star = og.GuideStar("offset", east=25.0, north=15.0)
    offset = og.OffsetGuide(target, star, SCALE, PA, update=5.0)
    px = offset.pixels(WHEN)
    g, world = _offset_rig(clock, px)
    t_start = clock.now()
    sol = g.set_offset(offset, lambda t: WHEN + timedelta(seconds=t - t_start))
    assert sol["setpoint"] == pytest.approx([FIBER[0] + px[0], FIBER[1] + px[1]], abs=1e-3)
    assert g._centroider.name != "annulus" and g.fiber == FIBER
    t0 = await _run(g, clock, 20.0)
    # The image drifted ~5 px; the target is held on the fiber.
    drift = np.hypot(*(world.jitter.at(clock.now() - world.t_origin)))
    assert drift > 3.0
    rms = world.residual_rms(FIBER, t0 + 2.0, t0 + 20.0, dt=5e-3, source=FIBER)
    assert rms < 0.25, rms
    st = g.status()
    assert st["guide_mode"] == "offset" and st["fiber"] == list(FIBER)
    assert st["offset_guide"]["now"]["setpoint"] == pytest.approx(list(g.config.target))
    vals = g.status_offset()
    assert vals["guide_mode"] == "offset" and vals["star"]["east"] == 25.0
    await g.stop()
    # Back to the target at the fiber.
    g.clear_offset()
    assert g.config.target == FIBER and g.offset is None and g._centroider.name == "annulus"


async def test_without_offset_guiding_the_faint_target_is_lost():
    # The same field guided on the target itself: its spill is too faint
    # to centroid, so the guider never gets going.
    clock = VirtualClock()
    g, _ = _offset_rig(clock, (60.0, 20.0))
    g.config.select = "nearest"
    g.config.max_star_distance = 10.0
    g.config.max_reacquire = 1
    g.start()
    st = await g.wait_state(("guiding", "failed"), 60)
    assert st == "failed"
    await g.stop()


async def test_the_setpoint_follows_field_rotation():
    # The field turns about the fiber at 0.05 deg/s (an alt-az port without
    # a derotator); the guide star 40 px away moves ~0.7 px in 20 s.
    for update, limit in ((1.0, 0.25), (1e9, None)):
        clock = VirtualClock()
        t_start = clock.now()
        rot = _Rotating((40.0, 0.0), 0.05, WHEN, update=update)
        g, world = _offset_rig(clock, (40.0, 0.0), drift=(0.0, 0.0), rotation=(*FIBER, 0.05))
        g.set_offset(rot, lambda t, t_start=t_start: WHEN + timedelta(seconds=t - t_start))
        t0 = await _run(g, clock, 20.0)
        rms = world.residual_rms(FIBER, t0 + 15.0, t0 + 20.0, dt=5e-3, source=FIBER)
        if limit is not None:
            assert rms < limit, rms
        else:
            assert rms > 0.5, rms  # a stale setpoint drags the target off the fiber
        await g.stop()


# -- the service ---------------------------------------------------------------------------


def _service(tmp_path, offset_px):
    world = SimWorld(plate_scale=SCALE, pa=PA)
    star = [FIBER[0] + offset_px[0] + 1.5, FIBER[1] + offset_px[1] - 1.0]
    return GuiderServiceConfig.from_dict(
        {
            "name": "t1-guider",
            "scope": "t1",
            "listen": "127.0.0.1:0",
            "port": "fiber",
            "camera": {"kind": "sim"},
            "tiptilt": {"kind": "sim", "limits": [[-200, 200], [-200, 200]]},
            "mount": {"kind": "sim", "tau": 0.05, "delay": 0.01},
            "calibrations": {
                "tiptilt": {"matrix": [[0.08, 0.03], [-0.03, 0.08]]},
                "mount": {"matrix": world.mount_matrix.tolist(), "units": "arcsec"},
            },
            "guide": {
                "target": list(FIBER),
                "exposure": 0.02,
                "acq_exposure": 0.05,
                "coarse_settle": 0.3,
                "plate_scale": SCALE,
                "centroider": {"kind": "annulus"},
                "fwhm": 4.0,
            },
            "offset_guide": {"update": 2.0},
            "sim": {
                "star": star,
                "others": [[FIBER[0], FIBER[1], 300.0]],
                "fiber": {"x": FIBER[0], "y": FIBER[1], "radius": 3.0},
                "jitter": {"rms": 0.1, "knee": 1.0, "seed": 5},
                "tt_matrix": [[0.08, 0.03], [-0.03, 0.08]],
                "plate_scale": SCALE,
                "pa": PA,
            },
        }
    )


async def test_service_offset_guiding_and_fits_values(tmp_path):
    import asyncio

    import httpx

    px = og.pixel_offset(-20.0, 12.0, SCALE, PA, False)
    svc = await GuiderService(_service(tmp_path, px)).start()
    reg = ValueRegistry()
    remote = GuiderRemote(svc.url, scope="t1", registry=reg, acquire_timeout=20, poll_interval=0.2)
    try:
        async with httpx.AsyncClient(base_url=svc.url, trust_env=False, timeout=30) as http:
            bad = {"target": {"ra": 30.0, "dec": 25.0}, "star": "auto"}
            r = await http.post("/start", json={"offset_guide": {**bad, "bogus": 1}})
            assert r.status_code == 400
            r = await http.post("/start", json={"offset_guide": {**bad, "pa": PA}})
            assert r.status_code == 409 and "catalog" in r.json()["error"]
            spec = {
                "target": {"ra": 30.0, "dec": 25.0},
                "star": {"east": -20.0, "north": 12.0},
                "pa": PA,
                "site": {"latitude": 31.7, "longitude": -110.9, "elevation": 2300.0},
            }
            st = await remote.start(dec=25.0, target_radec=[30.0, 25.0], offset_guide=spec)
            assert st["guide_mode"] == "offset" and st["fiber"] == list(FIBER)
            now = st["offset_guide"]["now"]
            assert now["setpoint"] == pytest.approx(
                [FIBER[0] + now["pixels"][0], FIBER[1] + now["pixels"][1]], abs=1e-3
            )
            # The refraction correction is there, and is the one for the
            # target's altitude now (it depends on the hour the test runs:
            # under 0.1 arcsec high up, near 0.9 arcsec at 5 deg altitude).
            site = og.OffsetSite(31.7, -110.9, 2300.0)
            want = og.refraction_shift(
                now["east"], now["north"], now["alt"], now["parallactic"], site
            )
            assert now["refraction"] == pytest.approx(want, abs=2e-3)
            assert 0 < math.hypot(*now["refraction"]) < 1.0
            await asyncio.sleep(0.5)
            st = (await http.get("/status")).json()
            assert st["state"] == "guiding", st["reason"]
            assert st["centroider"] != "annulus"
            assert reg.get("t1.guide_mode").value == "offset"
            assert reg.get("t1.guide_mode").keyword == "GUIDMODE"
            assert reg.get("t1.gstar_source").value == "offset"
            assert reg.get("t1.gstar_east").value == pytest.approx(-20.0)
            assert reg.get("t1.fiber_x").value == FIBER[0]  # the fiber, not the setpoint
            assert await remote.stop("done")
            # A plain start guides on the target at the fiber again.
            r = await http.post("/start", json={"mode": "open"})
            st = r.json()
            assert st["guide_mode"] == "target" and st["target"] == list(FIBER)
            await http.post("/stop", json={})
    finally:
        await remote.stop("teardown")
        await svc.stop()
