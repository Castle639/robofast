"""When to offset guide (robofast_core.guider.recommend): the photon-noise
model against the guide loop itself on simulated frames, across a
magnitude grid, and the choice it makes."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from robofast_core.guider import offset as og
from robofast_core.guider import recommend as rc
from robofast_core.guider.base import VirtualClock
from robofast_core.guider.calibration import Calibration
from robofast_core.guider.loop import GuideConfig, Guider
from robofast_core.guider.sim import Fiber, SimCamera, SimTipTilt, SimWorld

SITES = Path(__file__).resolve().parents[2] / "sites"
FIBER = (320.0, 240.0)

#: A generic 1.5 m fiber-fed telescope (test values, not a site's).
MODEL = rc.GuideModel(
    aperture=1.5,
    obscuration=0.1,
    fiber_diameter=2.3,
    plate_scale=0.3,
    read_noise=1.5,
    throughput=0.3,
    exposure=1.0,
    seeing=1.5,
    sky=21.0,
)


def _world(clock, model, mag, *, offset_px=None, star_g=None):
    """The target on the fiber hole; with ``offset_px``, a guide star of
    ``star_g`` that far from it. No image motion: what is left of the
    guiding error is the centroid noise the model predicts."""
    ps = model.plate_scale
    sky_e = model.zero_point * 10 ** (-0.4 * model.sky) * model.area * model.throughput * ps**2
    tt = SimTipTilt(clock=clock, limits=((-500.0, 500.0), (-500.0, 500.0)))
    target_flux = model.electrons(mag, 1.0)
    if offset_px is None:
        star, flux, others = FIBER, target_flux, []
    else:
        star = (FIBER[0] + offset_px[0], FIBER[1] + offset_px[1])
        flux, others = model.electrons(star_g, 1.0), [(*FIBER, target_flux)]
    world = SimWorld(
        clock=clock,
        star=star,
        flux=flux,
        others=others,
        fwhm=model.seeing / ps,
        sky=sky_e,
        read_noise=model.read_noise,
        fiber=Fiber(*FIBER, radius=model.fiber_diameter / 2 / ps),
        tiptilt=tt,
        tt_matrix=np.array([[0.1, 0.0], [0.0, 0.1]]),
        plate_scale=ps,
        pa=0.0,
        seed=int(mag * 10),
    )
    cfg = GuideConfig(
        target=FIBER,
        exposure=model.exposure,
        acq_exposure=model.exposure,
        acq_roi=(FIBER[0] - 80, FIBER[1] - 80, 160, 160) if offset_px is None else None,
        roi_size=48,
        fiber_radius=model.fiber_diameter / 2 / ps,
        centroider={"kind": "annulus"},
        fwhm=model.seeing / ps,
        gains={"kp": model.kp},
        max_reacquire=0,
        lost_frames=3,
        scope="sim",
        plate_scale=ps,
    )
    g = Guider(
        SimCamera(world),
        cfg,
        tiptilt=tt,
        tt_cal=Calibration("simtt", "fiber", world.tt_matrix.tolist()),
        clock=clock,
    )
    return g, world


async def _simulated_error(model, mag, method, star_g=None, frames=60):
    """The true rms distance (arcsec) of the target from the fiber over
    ``frames`` guide frames; inf if the guider cannot acquire or loses it."""
    clock = VirtualClock()
    if method == "offset":
        target = og.TargetPosition(30.0, 25.0)
        star = og.GuideStar("offset", east=30.0, north=20.0)
        off = og.OffsetGuide(target, star, model.plate_scale, 0.0)
        from datetime import UTC, datetime

        when = datetime(2026, 11, 15, 7, tzinfo=UTC)
        g, world = _world(clock, model, mag, offset_px=off.pixels(when), star_g=star_g)
        g.set_offset(off, lambda t: when)
    else:
        g, world = _world(clock, model, mag)
    g.start()
    st = await g.wait_state(("guiding", "failed"), 30 * model.exposure)
    if st != "guiding":
        await g.stop()
        return math.inf
    t0 = clock.now()
    await clock.sleep(frames * model.exposure)
    lost = g.state != "guiding"
    await g.stop()
    if lost:
        return math.inf
    rms = world.residual_rms(FIBER, t0 + 5 * model.exposure, clock.now(), dt=0.05, source=FIBER)
    return rms * model.plate_scale


GRID = (12.0, 15.0, 16.5, 17.5, 18.5)


async def test_the_model_agrees_with_the_simulated_guide_loop():
    """Across the magnitude grid, the predicted tracking error of each method
    is within a factor of two of the guide loop's true error on simulated
    frames, and the model says "not found" exactly where the guider cannot
    hold the star."""
    rows = []
    for mag in GRID:
        p = rc.predict(MODEL, mag, star_g=mag - 4.0)
        sim_a = await _simulated_error(MODEL, mag, "annulus")
        sim_o = await _simulated_error(MODEL, mag, "offset", star_g=mag - 4.0)
        rows.append((mag, p["annulus"], sim_a, p["offset"], sim_o))
    table = "\n".join(
        f"{m:5.1f} annulus model {a.tracking if a.found else math.inf:.4f} sim {sa:.4f}"
        f" | offset model {o.tracking:.4f} sim {so:.4f}"
        for m, a, sa, o, so in rows
    )
    for _mag, a, sim_a, o, sim_o in rows:
        assert a.found == math.isfinite(sim_a), table
        if a.found:
            assert 0.5 < sim_a / a.tracking < 2.0, table
        assert o.found and math.isfinite(sim_o), table
        assert 0.5 < sim_o / o.tracking < 2.0, table
    # The faint end: the spill is lost, offset guiding still holds.
    assert not rows[-1][1].found and math.isfinite(rows[-1][4])


async def test_the_recommendation_follows_the_simulated_errors():
    """The recommendation's choice is the method the simulated guide loop
    does better with (allowing for its preference for annulus guiding,
    which needs no second star and has no systematic error)."""
    for mag in (12.0, 18.5):
        rec = rc.recommend(MODEL, mag, star_g=12.0, separation=50.0)
        sim_a = await _simulated_error(MODEL, mag, "annulus")
        sim_o = await _simulated_error(MODEL, mag, "offset", star_g=12.0)
        sim_o_total = math.hypot(sim_o, rec.offset.systematic)
        if rec.method == "annulus":
            assert sim_a <= MODEL.prefer_annulus * sim_o_total, rec.reason
        else:
            assert sim_o_total < sim_a, rec.reason
    assert rc.recommend(MODEL, 12.0, star_g=12.0).method == "annulus"
    assert rc.recommend(MODEL, 18.5, star_g=12.0).method == "offset"


def test_choices_and_reasons():
    bright = rc.recommend(MODEL, 10.0)
    assert bright.method == "annulus" and "within" in bright.reason
    faint = rc.recommend(MODEL, 19.0)
    assert faint.method == "offset" and "too faint to centroid" in faint.reason
    # Bad seeing spills more light: annulus works fainter.
    assert rc.recommend(MODEL, 18.0, seeing=3.0).annulus.snr > rc.recommend(MODEL, 18.0).annulus.snr
    # A bright sky hurts the faint spill first.
    assert rc.recommend(MODEL, 17.5, sky=17.0).method == "offset"
    # Nothing works: the lesser evil, flagged.
    worst = rc.recommend(MODEL, 22.0, star_g=21.0)
    assert "neither" in worst.reason
    # The offset's systematics grow with the separation.
    near = rc.predict(MODEL, 15.0, separation=20.0)["offset"].systematic
    far = rc.predict(MODEL, 15.0, separation=200.0)["offset"].systematic
    assert far > 3 * near
    j = faint.as_json()
    assert j["method"] == "offset" and j["annulus"]["error"] is None and j["offset"]["error"] > 0
    with pytest.raises(ValueError, match="unknown keys"):
        rc.GuideModel.from_dict({**json.loads(json.dumps(MODEL.__dict__)), "bogus": 1})
    with pytest.raises(ValueError, match="aperture"):
        rc.GuideModel.from_dict({**MODEL.__dict__, "aperture": 0})


@pytest.mark.parametrize("site, instrument", [("minerva", "spectrograph"), ("flwo60", "tres")])
def test_site_models_load_and_cross_over(site, instrument, capsys):
    """Each fiber instrument's guiding model (its dispatch example) loads,
    prefers annulus guiding for a bright target and offset guiding for one
    too faint to centroid, and the CLI prints the table."""
    path = SITES / site / "dispatch.example.json"
    model = rc.load_model(path, instrument)
    assert rc.recommend(model, 8.0).method == "annulus"
    assert rc.recommend(model, 20.0).method == "offset"
    assert rc.main(["--config", str(path), "--instrument", instrument, "--grid", "8:20:2"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 8 and "offset" in out and "annulus" in out
    assert rc.main(["--config", str(path), "--instrument", "nope", "--mag", "10"]) == 2
