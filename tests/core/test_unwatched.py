"""Unattended without a hardware watchdog: the site's opt-in
(``safety.allow_unattended_without_watchdog``; docs/modes.md) and its loud
warning -- at start, on every switch into an unattended mode, in the status
(the console's enclosure panel, the sequencer's snapshot, the request API),
periodically in the log, and in the morning report."""

from __future__ import annotations

import asyncio
import logging

import pytest

from robofast_core import SafetyConfig
from robofast_core.console.aggregate import Aggregator
from robofast_core.modes import ModeGate, modes_from_config
from robofast_core.morning import build_report, gather, render_markdown
from robofast_core.safety import UNWATCHED_TEXT
from robofast_core.testing.checks import wait_until

# -- the config ------------------------------------------------------------------


def test_the_opt_in_is_explicit():
    # No heartbeat, no opt-in: attended only (as before), and saying
    # otherwise is refused, pointing at the opt-in.
    assert SafetyConfig().attended_only and not SafetyConfig().unwatched
    with pytest.raises(ValueError, match="allow_unattended_without_watchdog"):
        SafetyConfig(attended_only=False)
    # The opt-in lifts the floor: robotic is offered.
    opted = SafetyConfig(allow_unattended_without_watchdog=True)
    assert opted.attended_only is False and opted.unwatched
    ms = modes_from_config({"mode": "attended"}, attended_floor=bool(opted.attended_only))
    assert "robotic" in ms.modes
    # With a heartbeat it changes nothing; with attended_only true the floor stays.
    hb = dict(heartbeat_interval=10, heartbeat_timeout=60)
    assert not SafetyConfig(allow_unattended_without_watchdog=True, **hb).unwatched
    kept = SafetyConfig(allow_unattended_without_watchdog=True, attended_only=True)
    assert kept.attended_only and not kept.unwatched
    with pytest.raises(ValueError, match="true or false"):
        SafetyConfig(allow_unattended_without_watchdog="yes")
    with pytest.raises(ValueError, match="unwatched_warning_interval"):
        SafetyConfig(allow_unattended_without_watchdog=True, unwatched_warning_interval=0)


# -- the supervisor's warnings -----------------------------------------------------


async def test_the_supervisor_warns_at_start_on_switch_and_periodically(make_rig, caplog):
    # The fakes have no heartbeat and opt in (tests/core/conftest.py FAST_SAFETY).
    caplog.set_level(logging.WARNING, logger="robofast_core.safety")
    rig = make_rig(modes={"mode": "attended"}, safety=dict(unwatched_warning_interval=0.2))
    sup = rig.supervisor
    (start,) = rig.timeline.events("unwatched")
    assert start.detail["why"] == "start" and start.detail["active"] is False
    assert sup.status()["mode"]["unwatched"] == {"active": False, "text": UNWATCHED_TEXT}
    await rig.start(sequencer=False)
    await asyncio.sleep(0.5)  # attended: no repeats
    assert len(rig.timeline.events("unwatched")) == 1

    caplog.clear()
    st = sup.set_mode("robotic", "alice", "unattended tonight")
    assert UNWATCHED_TEXT in st["notice"]  # the console's notice and the notifier
    assert st["unwatched"]["active"] is True
    switched = rig.timeline.events("unwatched")[-1]
    assert switched.detail["active"] and switched.detail["mode"] == "robotic"
    assert any(r.levelno == logging.CRITICAL and "NO WATCHDOG" in r.message for r in caplog.records)
    # Repeated while it lasts.
    await wait_until(
        lambda: sum(1 for e in rig.timeline.events("unwatched") if "still" in e.detail["why"]) >= 2,
        3,
        what="periodic warnings",
    )
    # Back to attended: said once, then quiet.
    sup.set_mode("attended", "bob", "watching")
    assert rig.timeline.events("unwatched")[-1].detail["active"] is False
    n = len(rig.timeline.events("unwatched"))
    await asyncio.sleep(0.5)
    assert len(rig.timeline.events("unwatched")) == n
    assert sup.status()["mode"]["unwatched"]["active"] is False


async def test_a_robotic_start_warns_at_once(make_rig):
    rig = make_rig(modes={"mode": "robotic"})
    (start,) = rig.timeline.events("unwatched")
    assert start.detail["active"] and start.detail["level"] == "critical"


async def test_no_opt_in_no_warning(make_rig):
    # A heartbeat (MINERVA's Aqawan): nothing to warn about.
    rig = make_rig(
        heartbeat_timeout=0.4, safety=dict(heartbeat_interval=0.05, heartbeat_timeout=0.4)
    )
    assert rig.supervisor.status()["mode"]["unwatched"] is None
    assert not rig.timeline.events("unwatched")
    # No heartbeat and no opt-in: attended only, robotic not offered, no warning.
    rig = make_rig(safety=dict(allow_unattended_without_watchdog=False))
    assert rig.supervisor.config.attended_only and "robotic" not in rig.supervisor.modes.modes
    assert rig.supervisor.status()["mode"]["unwatched"] is None


# -- where it shows ----------------------------------------------------------------


class _Source:
    def __init__(self, unwatched):
        self.unwatched = unwatched

    def mode_status(self):
        return {"mode": "robotic", "policy": {"observe": "plan"}, "unwatched": self.unwatched}


async def test_the_sequencer_reads_it():
    class Plan:
        async def next_observation(self, telescope):
            return None

    warning = {"active": True, "text": UNWATCHED_TEXT}
    gate = ModeGate(Plan(), lambda: _Source(warning), lambda *a, **k: None, refresh=0)
    await gate.update()
    assert gate.unwatched == warning
    gate = ModeGate(Plan(), lambda: _Source(None), lambda *a, **k: None, refresh=0)
    await gate.update()
    assert gate.unwatched is None


def test_the_console_alarms_for_as_long_as_it_lasts():
    alarms: list = []

    def alarm(source, text, level="alarm", detail=""):
        alarms.append((source, text, level))

    sup = {"mode": {"mode": "robotic", "unwatched": {"active": True, "text": UNWATCHED_TEXT}}}
    view = Aggregator._enclosure_view(sup, None, alarm)
    assert view["mode"]["unwatched"]["active"]  # the enclosure panel's banner
    assert ("mode", UNWATCHED_TEXT, "warning") in alarms
    alarms.clear()
    sup["mode"]["unwatched"]["active"] = False
    Aggregator._enclosure_view(sup, None, alarm)
    assert not alarms


def test_the_morning_report_says_it_first(tmp_path):
    log = tmp_path / "supervisor.log"
    log.write_text(
        "2026-11-10 01:00:00,000 WARNING robofast_core.timeline: roof: unwatched"
        " {'why': 'switch from attended', 'mode': 'robotic', 'active': True}\n"
        "2026-11-10 01:15:00,000 WARNING robofast_core.timeline: roof: unwatched"
        " {'why': 'still unattended with no watchdog', 'mode': 'robotic', 'active': True}\n"
    )
    rep = build_report(gather("n20261109", tmp_path / "data", plan_records=[], logs=[log]))
    assert rep["unwatched"]["active"] and len(rep["unwatched"]["events"]) == 2
    md = render_markdown(rep)
    assert "NO hardware watchdog" in md.split("## Enclosure")[0]
    quiet = build_report(gather("n20261109", tmp_path / "data", plan_records=[], logs=[]))
    assert quiet["unwatched"] is None and "watchdog" not in render_markdown(quiet)
