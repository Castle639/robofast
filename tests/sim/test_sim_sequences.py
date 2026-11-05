"""A TRES calibration sequence (docs/sequences.md) on the simulators:
OmniSim's telescope and camera, the TRES front end's server on its
simulated controller (the calibration stage) and the lamp server on its
simulated Pulizzi; a dark sequence, so no lease and no supervisor. Run to
the end, and cancelled mid-sequence: the lamps off and the stage out."""

from __future__ import annotations

import asyncio

import pytest

from robofast.alpaca import alpaca_owner
from robofast.devices.pulizzi_server import PulizziServer
from robofast.devices.tres_frontend_server import TRESFrontEndConfig, TRESFrontEndServer
from robofast.lamps import LampSetup
from robofast.plan import StaticPlan
from robofast.sequencer import Sequencer, SequencerConfig, TelescopeUnit
from robofast.sequences import sequences_from_config
from robofast.testing.checks import wait_until
from robofast.timeline import Timeline
from tests.core.test_pulizzi import lamp_config

from .conftest import COMMAND_TIMEOUT, SIM_SEQ
from .test_sim_filterwheel import NoLeases

pytestmark = pytest.mark.alpaca_sim

LAMPS = {
    "switch": "placeholder",
    "frame_overhead": 5.0,
    "lamps": {"ThAr1": {"warmup": 0.5, "max_on": 600}, "Incand": {"warmup": 0}},
}


def tres_thar(exptime: float, n: int) -> dict:
    return {
        "instrument": "camera_spectrograph",
        "steps": [
            {"calstage": "in"},
            {"lamp": "ThAr1", "on": True, "warmup": 0.5},
            {"expose": {"type": "ARC", "exptime": exptime, "n": n}},
            {"lamp": "ThAr1", "on": False},
            {"calstage": "out"},
        ],
        "cleanup": [{"calstage": "out"}],
    }


@pytest.fixture
def tres(omnisim):
    lamps = PulizziServer(lamp_config()).start()
    stage = TRESFrontEndServer(
        TRESFrontEndConfig.from_dict(
            {
                "name": "tres",
                "listen": "127.0.0.1:0",
                "port": {"type": "sim", "sim": {"velocity": 20.0}},
                "positions": {"out": 0.0, "sky": 10.0, "science": 20.0, "in": 25.0},
                "poll_interval": 0.05,
                "status_max_age": 0.05,
                "request_timeout": 5.0,
                "start_grace": 0.5,
            }
        )
    ).start()
    tl = Timeline()
    t = COMMAND_TIMEOUT
    owners = {
        "telescope": alpaca_owner("telescope", "telescope", omnisim, tl, default_timeout=t),
        "camera": alpaca_owner("camera", "camera", omnisim, tl, default_timeout=t),
        "lamps": alpaca_owner("lamps", "switch", lamps.address, tl, default_timeout=5),
        "calstage": alpaca_owner("calstage", "filterwheel", stage.address, tl, default_timeout=5),
    }
    yield tl, owners, lamps, stage
    for o in owners.values():
        o.close()
    lamps.stop()
    stage.stop()


def sequencer(tl, owners, plan, seq: dict) -> Sequencer:
    unit = TelescopeUnit(
        "tillinghast",
        owners["telescope"],
        owners["camera"],
        calstage=owners["calstage"],
        lamps=owners["lamps"],
        lamp_setup=LampSetup.from_dict(LAMPS),
    )
    cfg = {
        "telescopes": {
            "tillinghast": {"telescope": "x", "camera": "x", "calstage": "x", "lamps": LAMPS}
        },
        "sequences": {"tres_thar": seq},
    }
    return Sequencer(
        "seq",
        supervisor=NoLeases(),
        telescopes=[unit],
        plan=plan,
        timeline=tl,
        config=SequencerConfig(**SIM_SEQ),
        sequences=sequences_from_config(cfg),
    )


def at(stage: TRESFrontEndServer, mm: float) -> bool:
    return not stage.sim.moving and stage.sim.position == pytest.approx(mm)


async def test_a_tres_thar_sequence_on_the_simulators(tres, truth):
    tl, owners, lamps, stage = tres
    plan = StaticPlan(
        [{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["tillinghast"]
    )
    seq = sequencer(tl, owners, plan, tres_thar(1.0, 2))
    await asyncio.wait_for(seq.run(), 180)
    assert plan.history == [("tillinghast", "thar", "complete", 2)]
    wanted = ("calstage_selected", "lamp_on", "exposure_started", "lamp_off")
    order = [e.what for e in tl.entries() if e.kind == "event" and e.what in wanted]
    assert order[:2] == ["calstage_selected", "lamp_on"] and "exposure_started" in order
    assert order.index("lamp_off") > max(i for i, w in enumerate(order) if w == "exposure_started")
    assert lamps.sim.lit() == [3, 5] and at(stage, 0.0)
    assert truth.parked() and truth.closed() and truth.camera_state() == 0


async def test_cancelled_on_the_simulators_lamps_off_and_stage_out(tres, truth):
    tl, owners, lamps, stage = tres
    plan = StaticPlan(
        [{"name": "thar", "kind": "sequence", "sequence": "tres_thar"}], ["tillinghast"]
    )
    seq = sequencer(tl, owners, plan, tres_thar(5.0, 10))
    task = asyncio.create_task(seq.run())
    await wait_until(lambda: tl.events("exposure_started"), 60.0, what="exposing")
    assert 2 in lamps.sim.lit() and at(stage, 25.0)
    task.cancel()
    await asyncio.wait([task], timeout=60)
    assert plan.history[0][2] == "interrupted"
    assert lamps.sim.lit() == [3, 5]
    await wait_until(lambda: at(stage, 0.0), 30.0, what="stage out")
    await wait_until(lambda: truth.camera_state() == 0, 30.0, what="camera idle")
    assert truth.parked()
