"""The atmospheric dispersion corrector as core uses it (docs/adc.md).

An ADC is an Alpaca Switch (robofast's ADC server,
:mod:`robofast.devices.adc_server`). Core finds its switches **by
name**, as the server publishes them (``MaxSwitch``, ``GetSwitchName``),
never by a hardcoded id: the sequencer writes ``tracking`` and waits for
``settled``; the imaging pipeline reads the rest into the FITS header.

The names default to robofast's ADC server's own (:data:`DEFAULT_NAMES`:
robofast's protocol); a telescope whose ADC names them differently says so
in its ``adc`` entry::

    "adc": "127.0.0.1:11180/0"                       # robofast's ADC server
    "adc": {"device": "127.0.0.1:11180/0",
            "switches": {"tracking": "TrackOn", "settled": "InPosition"}}

A role whose name the device does not publish is a :class:`DeviceError`
for the read or command that needs it (the sequencer warns and goes on;
a header value is marked failed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robofast.owner import DeviceError

__all__ = ["DEFAULT_NAMES", "HEADER", "ADCSwitches", "adc_entry"]

#: Role -> the switch name robofast's ADC server publishes.
DEFAULT_NAMES: dict[str, str] = {
    "tracking": "Tracking",
    "settled": "Settled",
    "deflection": "Deflection",
    "motor_a": "PrismA",
    "motor_b": "PrismB",
}

#: The roles read for light frames: role -> (method, registry name,
#: keyword, unit, comment).
HEADER: dict[str, tuple[str, str, str, str, str]] = {
    "tracking": ("GetSwitch", "adc.tracking", "ADCTRACK", "", "ADC tracking"),
    "settled": ("GetSwitch", "adc.settled", "ADCSETTL", "", "ADC settled on this target"),
    "deflection": (
        "GetSwitchValue",
        "adc.deflection",
        "ADCDEFL",
        "deg",
        "ADC prism deflection from null",
    ),
    "motor_a": ("GetSwitchValue", "adc.motor_a", "ADCANGA", "deg", "ADC motor A angle from home"),
    "motor_b": ("GetSwitchValue", "adc.motor_b", "ADCANGB", "deg", "ADC motor B angle from home"),
}


class ADCSwitches:
    """The switch ids of one ADC, looked up by name on first use and kept
    (dropped again when a name is missing, so a restarted server is
    re-read)."""

    def __init__(self, names: Mapping[str, str] | None = None) -> None:
        unknown = set(names or {}) - set(DEFAULT_NAMES)
        if unknown:
            raise ValueError(
                f"adc.switches: unknown roles {sorted(unknown)}; expected some of "
                f"{sorted(DEFAULT_NAMES)}"
            )
        for role, name in (names or {}).items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"adc.switches.{role}: a switch name, not {name!r}")
        self.names = {**DEFAULT_NAMES, **(names or {})}
        self._ids: dict[str, int] | None = None

    async def _published(self, grant: Any) -> dict[str, int]:
        n = int(await grant.get("MaxSwitch"))
        out: dict[str, int] = {}
        for i in range(n):
            out.setdefault(str(await grant.call("GetSwitchName", i)), i)
        return out

    async def id(self, grant: Any, role: str) -> int:
        """The switch id of ``role`` on the device behind ``grant``."""
        if self._ids is None:
            self._ids = await self._published(grant)
        name = self.names[role]
        if name not in self._ids:
            published, self._ids = sorted(self._ids), None
            raise DeviceError(f"ADC: no switch named {name!r} ({role}); it has {published}")
        return self._ids[name]


def adc_entry(entry: Any, where: str) -> tuple[str, ADCSwitches]:
    """A telescope's ``adc`` entry (module doc): its device address and
    its :class:`ADCSwitches`."""
    if isinstance(entry, str):
        return entry, ADCSwitches()
    if not isinstance(entry, dict) or not isinstance(entry.get("device"), str):
        raise ValueError(f"{where}: an Alpaca address, or {{'device': address, 'switches': ...}}")
    extra = set(entry) - {"device", "switches"} - {k for k in entry if k.startswith("_")}
    if extra:
        raise ValueError(f"{where}: unknown keys {sorted(extra)}")
    try:
        return entry["device"], ADCSwitches(entry.get("switches"))
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc
