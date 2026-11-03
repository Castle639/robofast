"""KiwiSpec's auxiliary hardware that is not on the legacy protocol.

**The chiller** (a Solid State Cooling Systems Oasis 3 at MINERVA) cools
the spectrograph's camera. robofast reads it for telemetry only: it never
sets it. Its interface to the spectrograph PC is not yet known (legacy's
chiller logging was disabled), so the only backend here is a simulator;
a driver registers its ``kind`` in :data:`CHILLERS` (a plug-in can too,
docs/plugins.md) and returns a :class:`Chiller`. ``{"kind": "oasis3"}`` is
accepted in a config so a site can name it, but it reads nothing: each read
says the driver is missing (``/status`` shows that as a warning, not an
alarm).

A reading is a dict: ``temp`` (C, the coolant), ``setpoint`` (C), ``flow``
(l/min, if the unit reports it), ``fault`` (the unit's own alarm text or
None). The KiwiSpec service warns when ``temp`` is outside ``setpoint +-
tolerance`` and alarms on a ``fault``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Chiller(Protocol):
    #: Degrees C either side of the set point before a warning.
    tolerance: float

    async def read(self) -> dict[str, Any]: ...


@dataclass
class SimChiller:
    """A chiller holding its set point; ``fault`` and ``temp`` can be set by a test."""

    setpoint: float = 15.0
    temp: float = 15.0
    flow: float | None = 2.0
    fault: str | None = None
    tolerance: float = 1.0

    async def read(self) -> dict[str, Any]:
        return {
            "temp": self.temp,
            "setpoint": self.setpoint,
            "flow": self.flow,
            "fault": self.fault,
        }


@dataclass
class MissingChiller:
    """A chiller named in the config whose driver robofast does not have yet."""

    kind: str
    tolerance: float = 1.0

    async def read(self) -> dict[str, Any]:
        raise NotImplementedError(
            f"no {self.kind} chiller driver yet (its interface is not known; docs/kiwispec.md)"
        )


#: kind -> factory(spec without "kind").
CHILLERS: dict[str, Callable[[dict[str, Any]], Chiller]] = {
    "sim": lambda spec: SimChiller(**spec),
    # Connection settings are ignored until there is a driver.
    "oasis3": lambda spec: MissingChiller("oasis3", float(spec.get("tolerance", 1.0))),
}


def build_chiller(spec: dict[str, Any] | None) -> Chiller | None:
    if spec is None:
        return None
    if not isinstance(spec, dict) or "kind" not in spec:
        raise ValueError(f"chiller: an object with a 'kind', not {spec!r}")
    s = {k: v for k, v in spec.items() if not k.startswith("_")}
    kind = str(s.pop("kind"))
    if kind not in CHILLERS:
        raise ValueError(f"chiller kind {kind!r}: one of {sorted(CHILLERS)}")
    return CHILLERS[kind](s)
