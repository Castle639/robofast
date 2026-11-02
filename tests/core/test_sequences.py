"""Config-defined sequences (robofast_core/sequences.py, docs/sequences.md):
the checks when a config is loaded, and full TRES calibration sequences on
the simulators -- the TRES front end's server (calibration stage), the lamp
server on its simulated Pulizzi, and the rig's camera -- including the
cleanup after a failure, a lease revoked and a cancellation."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from robofast_core.alpaca import alpaca_owner
from robofast_core.config import load_config
from robofast_core.devices.pulizzi_server import PulizziServer
from robofast_core.devices.tres_frontend_server import TRESFrontEndConfig, TRESFrontEndServer
from robofast_core.lamps import LampSetup
from robofast_core.plan import CalibrationPlan, Observation, StaticPlan, plan_from_config
from robofast_core.sequencer_service import check_plan_sequences
from robofast_core.sequences import SEQUENCE_STEPS, StepType, sequences_from_config
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT
from tests.core.test_pulizzi import LAMPS, lamp_config, switching

THAR = {
    "instrument": "camera_spectrograph",
    "steps": [
        {"calstage": "in"},
        {"lamp": "ThAr1", "on": True, "warmup": 0.2},
        {"expose": {"type": "ARC", "exptime": 0.2, "n": 3}},
        {"lamp": "ThAr1", "on": False},
        {"calstage": "out"},
    ],
    "cleanup": [{"calstage": "out"}],
}


def config(sequences: dict, **tel) -> dict:
    entry = {
        "telescope": "127.0.0.1:1/0",
        "camera": "127.0.0.1:1/0",
        "calstage": "127.0.0.1:1/0",
        "lamps": {
            "switch": "127.0.0.1:1/0",
            "frame_overhead": 1.0,
            "lamps": {"ThAr1": {"warmup": 0.2, "max_on": 600}, "Incand": {"warmup": 0}},
        },
        **tel,
    }
    return {"name": "e", "telescopes": {"t1": entry}, "sequences": sequences}


# -- the checks when the config is loaded --------------------------------------------


def test_a_sequence_loads_with_what_it_does():
    (seq,) = sequences_from_config(config({"thar": THAR})).values()
    assert [s.type for s in seq.steps] == ["calstage", "lamp", "expose", "lamp", "calstage"]
    assert not seq.light and seq.lamps == ("ThAr1",) and seq.runs_on("t1")
    assert seq.lamp_time["t1"]["ThAr1"] == pytest.approx(0.2 + 3 * 1.2)
    # A bare list is its steps; an EXPOSE step makes it light.
    (light,) = sequences_from_config(
        config({"sky": [{"calstage": "sky"}, {"expose": {"type": "EXPOSE", "exptime": 5}}]})
    ).values()
    assert light.light and light.cleanup == ()
    assert sequences_from_config({"telescopes": {}}) == {}


def _with(change):
    d = copy.deepcopy(THAR)
    change(d)
    return d


@pytest.mark.parametrize(
    ("seq", "tel", "match"),
    [
        (
            _with(lambda d: d["steps"].append({"lmap": 1})),
            {},
            r"steps\[5\]: unknown step \['lmap'\]",
        ),
        (
            _with(lambda d: d["steps"].insert(0, {"wait": 1, "calstage": "x"})),
            {},
            r"steps\[0\]: one step type per step",
        ),
        (
            _with(lambda d: d["steps"][1].update(on="yes")),
            {},
            r"steps\[1\]: lamp ThAr1: say \"on\"",
        ),
        (
            _with(lambda d: d["steps"][1].update(lamp="ThAr9")),
            {},
            r"steps\[1\] \(on t1\): no lamp 'ThAr9' on t1",
        ),
        (
            _with(lambda d: d["steps"].pop(3)),
            {},
            r"tres\.steps: leaves lamp\(s\) \['ThAr1'\] on",
        ),
        (
            _with(lambda d: d["steps"].pop(1)),
            {},
            r"steps\[1\] \(on t1\): a ARC frame needs a lamp on",
        ),
        (
            _with(lambda d: d["steps"][2]["expose"].update(n=600)),
            {},
            r"keeps lamp ThAr1 on for about 720 s, more than its max_on 600 s",
        ),
        (
            _with(lambda d: d.update(instrument="imager"))
            | {"steps": [{"expose": {"type": "SPECTRUM"}}]},
            {},
            r"steps\[0\]: expose\.type: one of",
        ),
        (
            _with(lambda d: d["steps"].insert(0, {"expose": {"type": "EXPOSE", "exptime": 1}}))
            | {"instrument": "camera_spectrograph"},
            {"calstage": "127.0.0.1:1/0"},
            None,  # camera_spectrograph takes EXPOSE: fine
        ),
        (THAR, {"calstage": None}, r"steps\[0\] \(on t1\): telescope 't1' has no calibration"),
        (
            _with(lambda d: d["steps"].insert(0, {"focus": 100})),
            {"focuser": "127.0.0.1:1/0"},
            r"has no autofocus entry",
        ),
        (
            _with(lambda d: d["cleanup"].append({"lamp": "ThAr1", "on": True})),
            {},
            r"cleanup\[1\]: not a cleanup step",
        ),
        (
            _with(lambda d: d["cleanup"].append({"expose": {"type": "DARK", "exptime": 1}})),
            {},
            r"cleanup\[1\]: not a cleanup step",
        ),
        (_with(lambda d: d.update(telescopes=["t9"])), {}, r"tres\.telescopes: unknown \['t9'\]"),
        (_with(lambda d: d.update(instrument="nope")), {}, r"tres\.instrument"),
        (_with(lambda d: d.update(colour="red")), {}, r"tres: unknown keys \['colour'\]"),
        (
            _with(lambda d: d["steps"].append({"spectrograph": [{"kind": "dark", "exptime": 1}]})),
            {},
            r"the enclosure has no spectrograph",
        ),
    ],
)
def test_bad_sequences_are_refused_with_the_steps_path(seq, tel, match):
    cfg = config({"tres": seq}, **tel)
    cfg["telescopes"]["t1"] = {k: v for k, v in cfg["telescopes"]["t1"].items() if v is not None}
    if match is None:
        sequences_from_config(cfg, where="e.json")
        return
    with pytest.raises(ValueError, match=match):
        sequences_from_config(cfg, where="e.json")


def test_the_shipped_tres_sequences_and_calibration_plan():
    cfg = load_config(ROOT / "sites/flwo60/flwo60.json")
    seqs = sequences_from_config(cfg)
    assert set(seqs) == {"tres_thar", "tres_flat"}
    for seq in seqs.values():
        assert not seq.light and seq.instrument == "camera_spectrograph"
        assert seq.steps[0].params == "in" and seq.steps[-1].params == "out"
        assert [s.params for s in seq.cleanup] == ["out"]
    entry = json.loads((ROOT / "sites/flwo60/calibration.example.json").read_text())
    plan = plan_from_config(entry, ["tillinghast"])
    check_plan_sequences(plan, seqs)
    assert [o.name for o in plan.queues["tillinghast"]] == [
        "bias",
        "dark60",
        "tres_thar",
        "tres_flat",
    ]
    # MINERVA defines none: its calibrations are the calibration plan's, unchanged.
    assert sequences_from_config(load_config(ROOT / "sites/minerva/aqawan1.json")) == {}


def test_plans_naming_sequences_are_checked():
    seqs = sequences_from_config(
        config({"thar": THAR, "sky": [{"expose": {"type": "EXPOSE", "exptime": 1}}]})
    )
    with pytest.raises(ValueError, match="no sequence 'nope'"):
        check_plan_sequences(CalibrationPlan(["t1"], sequences=["nope"]), seqs)
    with pytest.raises(ValueError, match="takes sky light; a calibration plan never opens"):
        check_plan_sequences(CalibrationPlan(["t1"], sequences=["sky"]), seqs)
    check_plan_sequences(
        StaticPlan([{"name": "s", "kind": "sequence", "sequence": "sky"}], ["t1"]), seqs
    )
    with pytest.raises(ValueError, match="unknown telescope 't2'"):
        CalibrationPlan(["t1"], sequences=[{"sequence": "thar", "telescopes": ["t2"]}])
    with pytest.raises(ValueError, match="a sequence names its definition"):
        Observation("x", kind="sequence")


def test_a_plug_in_adds_a_step_type():
    class Beep(StepType):
        name = "beep"
        cleanup = True

        def parse(self, value, options, where):
            if not isinstance(value, int):
                raise ValueError(f"{where}: beep: a count")
            return value

        async def run(self, params, r):
            r.event("beeped", n=params)

    SEQUENCE_STEPS.register("beep", Beep())
    try:
        (seq,) = sequences_from_config(
            config(
                {
                    "b": [{"beep": 2}],
                }
            )
        ).values()
        assert seq.steps[0].type == "beep"
        with pytest.raises(ValueError, match=r"steps\[0\]: beep: a count"):
            sequences_from_config(config({"b": [{"beep": "x"}]}))
    finally:
        SEQUENCE_STEPS.unregister("beep")


# -- on the simulators ---------------------------------------------------------------


@pytest.fixture
def servers():
    made: list = []

    def start():
        lamps = PulizziServer(lamp_config()).start()
        stage = TRESFrontEndServer(
            TRESFrontEndConfig.from_dict(
                {
                    "name": "tres",
                    "listen": "127.0.0.1:0",
                    "port": {"type": "sim", "sim": {"velocity": 100.0}},
                    "positions": {"out": 0.0, "sky": 10.0, "science": 20.0, "in": 25.0},
                    "poll_interval": 0.02,
                    "status_max_age": 0.02,
                    "request_timeout": 3.0,
                    "start_grace": 0.2,
                }
            )
        ).start()
        made.extend([lamps, stage])
        return lamps, stage

    yield start
    for s in made:
        s.stop()


def tres_rig(make_rig, servers, plan, sequences: dict, **kw):
    lamps, stage = servers()
    rig = make_rig(plan=plan, **kw)
    unit = rig.sequencer.units["t1"]
    unit.lamps = alpaca_owner("t1.lamps", "switch", lamps.address, rig.timeline, default_timeout=5)
    unit.calstage = alpaca_owner(
        "t1.calstage", "filterwheel", stage.address, rig.timeline, default_timeout=5
    )
    rig.owners["t1.lamps"], rig.owners["t1.calstage"] = unit.lamps, unit.calstage
    cfg = config(sequences)
    unit.lamp_setup = LampSetup.from_dict(cfg["telescopes"]["t1"]["lamps"])
    rig.sequencer.sequences = sequences_from_config(cfg)
    return rig, lamps, stage


def _events(rig, what: str) -> list:
    return rig.timeline.events(what, "seq")


def stage_at(stage: TRESFrontEndServer, mm: float) -> bool:
    return not stage.sim.moving and stage.sim.position == pytest.approx(mm)


async def test_a_full_tres_calibration_sequence(servers, make_rig):
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"tres_thar": THAR})
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 30)
    assert plan.history == [("t1", "thar", "complete", 3)]
    t = {
        w: _events(rig, w)[0].t
        for w in ("calstage_selected", "lamp_on", "exposure_started", "lamp_off", "sequence_done")
    }
    assert t["calstage_selected"] < t["lamp_on"] < t["exposure_started"] < t["lamp_off"]
    assert t["exposure_started"] - t["lamp_on"] >= 0.2  # the warm-up
    assert _events(rig, "lamp_off")[0].t > _events(rig, "exposure_complete")[-1].t
    stages = [e.detail["calstage"] for e in _events(rig, "calstage_selected")]
    assert stages == ["in", "out", "out"]  # the last: the cleanup, which always runs
    assert switching(lamps.sim)[:2] == ["N02", "F02"] and lamps.sim.lit() == [3, 5]
    assert stage_at(stage, 0.0) and rig.cams[0].exposures_started == 3
    assert rig.dome.opens == 0  # a dark sequence: no lease, the dome stays shut
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True


async def test_cancelled_mid_sequence_the_lamps_go_off_and_the_stage_out(servers, make_rig):
    slow = _with(lambda d: d["steps"][2]["expose"].update(exptime=2.0, n=5))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"tres_thar": slow})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 10.0, what="exposing")
    assert 2 in lamps.sim.lit() and stage_at(stage, 25.0)
    rig.tasks[-1].cancel()  # the sequencer stopping (SIGTERM)
    await asyncio.wait([rig.tasks[-1]], timeout=20)
    assert plan.history[0][2] == "interrupted"
    assert lamps.sim.lit() == [3, 5]
    await wait_until(lambda: stage_at(stage, 0.0), 5.0, what="stage out")
    assert _events(rig, "sequence_cleanup_done")[0].detail["ok"] is True
    assert any(c.parked for c in rig.sequencer.cleanups) or rig.parked()


async def test_a_failure_mid_sequence_still_cleans_up(servers, make_rig):
    slow = _with(lambda d: d["steps"][2]["expose"].update(exptime=0.5, n=4))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(
        make_rig, servers, plan, {"tres_thar": slow}, seq=dict(max_consecutive_errors=1)
    )
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 10.0, what="exposing")
    rig.cams[0].fail("camera gone")
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 30)
    assert plan.history[0][2] == "failed"
    assert lamps.sim.lit() == [3, 5] and stage_at(stage, 0.0)


async def test_a_lease_revoked_mid_sequence_cleans_up_as_it_closes(servers, make_rig):
    sky = {
        "steps": [{"calstage": "science"}, {"expose": {"type": "EXPOSE", "exptime": 1.0, "n": 20}}],
        "cleanup": [{"calstage": "out"}],
    }
    plan = StaticPlan([{"name": "sky", "kind": "sequence", "sequence": "sky"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {"sky": sky})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 10.0, what="exposing")
    assert rig.dome_open() and stage_at(stage, 20.0)
    rig.weather.safe = False
    await wait_until(lambda: rig.dome_closed() and rig.parked(), 10.0, what="closed, parked")
    await wait_until(lambda: stage_at(stage, 0.0), 5.0, what="stage out")
    assert plan.history[0][2] == "interrupted"
    assert lamps.sim.lit() == [3, 5]


async def test_a_sequence_that_cannot_fit_in_max_on_is_refused_before_any_lamp(servers, make_rig):
    long = _with(lambda d: d["steps"][2]["expose"].update(exptime=20.0, n=40))
    plan = StaticPlan([{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["t1"])
    rig, lamps, stage = tres_rig(make_rig, servers, plan, {})
    cfg = config({"tres_thar": long})
    cfg["telescopes"]["t1"]["lamps"]["lamps"]["ThAr1"].pop("max_on")  # only the switch knows
    rig.sequencer.sequences = sequences_from_config(cfg)
    await rig.start()
    await wait_until(lambda: plan.history, 10.0, what="refused")
    assert plan.history[0][2] == "failed"
    failed = _events(rig, "observation_failed")
    assert failed and "max_on 600 s" in failed[0].detail["error"]
    assert switching(lamps.sim) == [] and rig.cams[0].exposures_started == 0
    assert stage_at(stage, 0.0)  # nothing moved either


async def test_an_unknown_sequence_fails_the_observation(servers, make_rig):
    plan = StaticPlan([{"name": "x", "kind": "sequence", "sequence": "nope"}], ["t1"])
    rig, *_ = tres_rig(make_rig, servers, plan, {"tres_thar": THAR})
    await rig.start()
    await wait_until(lambda: plan.history, 10.0, what="failed")
    assert plan.history[0][2] == "failed"
    assert "no sequence 'nope'" in _events(rig, "observation_failed")[0].detail["error"]


assert LAMPS  # the switch's lamps (tests/core/test_pulizzi.py): ThAr1, Incand


async def test_a_preempted_sequence_cleans_up_before_the_next_observation(servers, make_rig):
    """A preemption ("now": a ToO, the console's stop) cancels the sequence
    alone, with no generation cleanup after it: the next observation (here
    the same, re-queued by the plan) waits for its cleanup."""
    sky = {
        "steps": [{"calstage": "science"}, {"expose": {"type": "EXPOSE", "exptime": 0.3, "n": 50}}],
        "cleanup": [{"calstage": "out"}],
    }
    plan = StaticPlan([{"name": "sky", "kind": "sequence", "sequence": "sky"}], ["t1"])
    rig, *_ = tres_rig(make_rig, servers, plan, {"sky": sky})
    await rig.start()
    await wait_until(lambda: rig.cams[0].exposures_started >= 1, 10.0, what="exposing")
    assert rig.sequencer.preempt("t1", "now", "a ToO")
    await wait_until(lambda: len(_events(rig, "target_start")) >= 2, 20.0, what="the next one")
    done = _events(rig, "sequence_cleanup_done")[0].t
    assert done < _events(rig, "target_start")[1].t
    assert plan.history[0][2] == "interrupted"
