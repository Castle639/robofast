"""Spectrograph calibration lamps, as the sequencer uses them (docs/pulizzi.md).

A telescope whose spectrograph has calibration lamps (the FLWO 60-inch's
TRES: ThAr and incandescent lamps on a Pulizzi power switch) has a
``lamps`` entry in the enclosure config::

    "lamps": {
      "switch": "127.0.0.1:11170/0",   # robofast's lamp Switch server
      "frame_overhead": 30,            # s per frame beyond its exposure (readout)
      "status_action": "Status",       # optional: the switch's action names
      "off_action": "LampsOff",        #   (defaults: robofast's lamp server's)
      "lamps": {
        "ThAr1": {"warmup": 60, "calstage": "in", "max_on": 1800},
        "Incand": {"warmup": 30, "calstage": "in"}
      }
    }

The names are the switch's outlet names (``Action("Status")``). ``warmup``
is how long the lamp burns before the first exposure; ``calstage`` the
calibration-stage position the lamp light needs (selected **before** the
lamp is switched on; an observation's own ``calstage`` wins; null: leave
the stage alone). ``max_on`` (optional) repeats the switch's own limit, so a
config-defined sequence that would keep the lamp on longer is refused when
the config is loaded (docs/sequences.md); the switch's limit is checked
again before any lamp is switched on.

The switch is :mod:`robofast.devices.pulizzi_server` (an Alpaca
Switch with the ``Status``, ``LampsOff`` and ``LampReplaced`` actions);
this module reads and switches it through a device owner's grant.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

#: Observation kinds that switch a lamp (request types ARC and LAMP_FLAT).
LAMP_KINDS = ("arc", "lamp_flat")


@dataclass
class LampSpec:
    #: Seconds the lamp burns before the first exposure.
    warmup: float = 0.0
    #: Calibration-stage position for this lamp's light (None: leave it).
    calstage: str | None = None
    #: The switch's max-on time for it, repeated here so sequences are
    #: checked when the config is loaded (None: checked only at run time).
    max_on: float | None = None


@dataclass
class LampSetup:
    switch: str
    lamps: dict[str, LampSpec] = field(default_factory=dict)
    #: Per-frame time beyond the exposure (readout, saving), for checking
    #: that an observation fits in the lamp's max-on time before starting.
    frame_overhead: float = 30.0
    #: Bound on one switch command (the server reads the unit back).
    switch_timeout: float = 15.0
    #: The switch's Alpaca actions: its outlets' state (JSON ``{"outlets":
    #: [{"name", "on", ...}]}``) and every lamp off (JSON ``{"off":
    #: [names]}``). The defaults are robofast's lamp server's.
    status_action: str = "Status"
    off_action: str = "LampsOff"

    @classmethod
    def from_dict(cls, d: Any) -> LampSetup:
        if not isinstance(d, dict):
            raise ValueError("lamps: expected an object")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = {
            "switch",
            "lamps",
            "frame_overhead",
            "switch_timeout",
            "status_action",
            "off_action",
        }
        if set(d) - known:
            raise ValueError(f"lamps: unknown keys {sorted(set(d) - known)}")
        if not isinstance(d.get("switch"), str) or not d["switch"]:
            raise ValueError("lamps.switch: the lamp switch's Alpaca address")
        raw = d.get("lamps") or {}
        if not isinstance(raw, dict) or not raw:
            raise ValueError("lamps.lamps: at least one lamp")
        lamps = {}
        for name, spec in raw.items():
            if name.startswith("_"):
                continue
            spec = {k: v for k, v in dict(spec or {}).items() if not k.startswith("_")}
            keys = {"warmup", "calstage", "max_on"}
            if set(spec) - keys:
                raise ValueError(f"lamps.lamps.{name}: unknown keys {sorted(set(spec) - keys)}")
            ls = LampSpec(**spec)
            if not isinstance(ls.warmup, int | float) or not 0 <= ls.warmup <= 3600:
                raise ValueError(f"lamps.lamps.{name}.warmup: 0 to 3600 s")
            if ls.max_on is not None and (
                isinstance(ls.max_on, bool)
                or not isinstance(ls.max_on, int | float)
                or not ls.max_on > 0
            ):
                raise ValueError(f"lamps.lamps.{name}.max_on: seconds, > 0")
            if ls.calstage is not None and not isinstance(ls.calstage, str):
                raise ValueError(f"lamps.lamps.{name}.calstage: a position name or null")
            lamps[str(name)] = ls
        setup = cls(
            d["switch"],
            lamps,
            float(d.get("frame_overhead", 30.0)),
            float(d.get("switch_timeout", 15.0)),
            d.get("status_action", "Status"),
            d.get("off_action", "LampsOff"),
        )
        for k in ("frame_overhead", "switch_timeout"):
            v = getattr(setup, k)
            if not math.isfinite(v) or v <= 0:
                raise ValueError(f"lamps.{k} must be positive")
        for k in ("status_action", "off_action"):
            if not isinstance(getattr(setup, k), str) or not getattr(setup, k):
                raise ValueError(f"lamps.{k}: an Alpaca action name")
        return setup

    def duration(self, name: str, exptime: float, count: int) -> float:
        """How long ``count`` frames with lamp ``name`` keep it on (s)."""
        return self.lamps[name].warmup + count * (exptime + self.frame_overhead)


async def switch_status(
    grant: Any, timeout: float, action: str = "Status"
) -> dict[str, dict[str, Any]]:
    """The switch's outlets by name (its status action, ``Action("Status")``
    on robofast's lamp server)."""
    raw = await grant.call("Action", action, "", timeout=timeout)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return {o["name"]: o for o in data["outlets"]}


async def lamps_off(grant: Any, timeout: float, action: str = "LampsOff") -> list[str]:
    """Every lamp off (the off action, ``Action("LampsOff")`` on robofast's
    lamp server); the names switched off."""
    raw = await grant.call("Action", action, "", timeout=timeout)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return list(data.get("off", []))
