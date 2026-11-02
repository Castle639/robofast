"""Instrument handlers: what each type of instrument takes, in a registry.

A request names an instrument (OCS ``instrument_type``: ``"imager"``,
``"spectrograph"``, ``"tres"``, ... -- the names a site's dispatch config
lists for each telescope), and the dispatch config declares each name's
**type** (``"instruments": {"spectrograph": {"type": "kiwispec"}}``;
a name it does not declare is its own type, so ``"imager"`` needs no
declaration). The type is an :class:`InstrumentType` in
:data:`INSTRUMENT_TYPES`, which says

* which OCS configuration types it takes (:attr:`~InstrumentType.accepts`)
  and which ``/instruments`` advertises (:attr:`~InstrumentType.advertised`;
  the lamp types only where a telescope with it has lamps);
* the instrument's own ``extra_params`` of an instrument config
  (:attr:`~InstrumentType.params`: e.g. KiwiSpec's iodine cell), and how
  each goes to the instrument with every frame;
* for a spectrograph service the sequencer drives: the calibration frames
  it takes (:attr:`~InstrumentType.calibration_kinds`) and how its client
  is built from an enclosure config's ``spectrograph`` entry
  (:meth:`~InstrumentType.client`; ``"type"`` names the handler).

Built-in types: ``imager`` (the telescope's camera; lamp frames too, where
the telescope has lamps) and
``camera_spectrograph`` (a spectrograph read out by the telescope's own
camera, with its lamp calibrations: e.g. FLWO's TRES). Hardware drivers add
theirs as built-in plug-ins (``robofast_core.devices``: KiwiSpec); a site
adds its own as a plug-in (docs/plugins.md).

The spectrograph service's errors the sequencer acts on are neutral
(:class:`SpectrographBusy`, any other :class:`SpectrographError`): a
client raises them (or subclasses) so the sequencer never imports a
particular spectrograph's code.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from robofast_core.kinds import LAMP_TYPES
from robofast_core.plugins import Registry

__all__ = [
    "INSTRUMENT_TYPES",
    "InstrumentType",
    "Param",
    "SpectrographBusy",
    "SpectrographError",
    "SpectrographFault",
    "calibration_kinds",
    "flag",
    "instrument_param",
    "instrument_params",
    "number",
    "resolve",
]


class SpectrographError(Exception):
    """The spectrograph could not take a spectrum (sequencer module doc,
    "Spectrograph health"): never the telescope's error."""


class SpectrographBusy(SpectrographError):
    """The spectrograph is busy with another job (this sequencer's, or
    another enclosure's after the service's bounded queue wait): the
    observation is reported ``interrupted`` and re-queued; nothing is
    wrong with the telescope or the spectrograph."""


class SpectrographFault(SpectrographError):
    """The spectrograph service refused or failed a job, did not finish it
    in time, or did not answer: counted towards marking the spectrograph
    down, not towards the telescope's give-up."""


def flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what}: expected true or false")
    return value


def number(lo: float = -math.inf, hi: float = math.inf) -> Callable[[Any, str], float]:
    def parse(value: Any, what: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{what}: expected a number, got {value!r}")
        x = float(value)
        if not (lo <= x <= hi):  # also rejects NaN
            raise ValueError(f"{what}: {value!r} is outside [{lo:g}, {hi:g}]")
        return x

    return parse


def _same(value: Any) -> Any:
    return value


@dataclass(frozen=True)
class Param:
    """One of an instrument's own ``extra_params`` of an instrument config."""

    #: ``parse(value, what) -> value``; raises ValueError naming ``what``.
    parse: Callable[[Any, str], Any]
    #: The configuration types it is for (the request is refused otherwise).
    types: tuple[str, ...]
    #: The value as the instrument takes it with each frame.
    frame: Callable[[Any], Any] = _same
    doc: str = ""


@dataclass(frozen=True)
class InstrumentType:
    """One type of instrument (module doc)."""

    name: str
    #: OCS instrument type: ``"IMAGE"`` or ``"SPECTRA"``.
    ocs_type: str
    #: OCS configuration types a request may ask of it.
    accepts: tuple[str, ...]
    #: Those ``/instruments`` lists (default: :attr:`accepts`; the lamp
    #: types only where a telescope with it has lamps).
    advertised: tuple[str, ...] | None = None
    #: ``/instruments`` lists the telescopes' filters as its optical elements.
    filters: bool = False
    #: Always guided (``/instruments``: guiding ON, OFF not schedulable).
    guided: bool = False
    #: Its own ``extra_params`` of an instrument config, by name.
    params: Mapping[str, Param] = field(default_factory=dict)
    #: The calibration frames its service takes (a calibration plan's
    #: ``spectrograph`` frames: ``{"kind": ..., "exptime": s, "n": N}``).
    calibration_kinds: tuple[str, ...] = ()
    #: ``client(entry, *, name, queue_wait)``: the sequencer's client for an
    #: enclosure config's ``spectrograph`` entry (its keys beyond the
    #: generic ones, :attr:`client_keys`); None: not a service.
    client_factory: Callable[..., Any] | None = None
    client_keys: frozenset[str] = frozenset()

    def listed(self, lamps: bool) -> list[str]:
        """The configuration types ``/instruments`` lists."""
        types = self.advertised if self.advertised is not None else self.accepts
        return [t for t in types if lamps or t not in LAMP_TYPES]

    def frame_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """An instrument config's parameters as the instrument takes them."""
        return {k: self.params[k].frame(v) for k, v in params.items() if k in self.params}

    def client(self, entry: dict[str, Any], *, name: str, queue_wait: float) -> Any:
        if self.client_factory is None:
            raise ValueError(f"spectrograph.type: {self.name!r} is not a spectrograph service")
        return self.client_factory(entry, name=name, queue_wait=queue_wait)


#: Every instrument type, by name.
INSTRUMENT_TYPES: Registry[InstrumentType] = Registry("instrument type")


def instrument_params() -> dict[str, Param]:
    """Every registered instrument's own parameters, by name (the first
    registered wins a name two declare; they must agree on its meaning)."""
    out: dict[str, Param] = {}
    for t in INSTRUMENT_TYPES.values():
        for k, p in t.params.items():
            out.setdefault(k, p)
    return out


def instrument_param(name: str) -> Param | None:
    return instrument_params().get(name)


def calibration_kinds() -> tuple[str, ...]:
    """The spectrograph calibration frame kinds any registered type takes."""
    out: list[str] = []
    for t in INSTRUMENT_TYPES.values():
        out += [k for k in t.calibration_kinds if k not in out]
    return tuple(out)


def resolve(
    declared: Mapping[str, Any], names: Iterable[str], where: str
) -> dict[str, InstrumentType]:
    """Each instrument name's type: ``declared[name]["type"]`` (a config's
    ``instruments`` entry), else the type of the same name; a name with
    neither is an error naming ``where``."""
    out: dict[str, InstrumentType] = {}
    for name in names:
        d = declared.get(name)
        if d is not None:
            if not isinstance(d, dict) or "type" not in d:
                raise ValueError(f"{where}.{name}: expected {{'type': <instrument type>}}")
            out[name] = INSTRUMENT_TYPES.lookup(d["type"], f"{where}.{name}.type")
        elif name in INSTRUMENT_TYPES:
            out[name] = INSTRUMENT_TYPES[name]
        else:
            raise ValueError(
                f"{where}: instrument {name!r} has no type: declare it, e.g."
                f" {{{name!r}: {{'type': ...}}}}, with a type of {INSTRUMENT_TYPES.names()}"
            )
    return out


INSTRUMENT_TYPES.register(
    "imager",
    InstrumentType(
        "imager",
        "IMAGE",
        accepts=(
            "EXPOSE",
            "DARK",
            "BIAS",
            "AUTO_FOCUS",
            "POINTING_MODEL",
            "ROTATOR_CALIBRATION",
            "SKY_FLAT",
            *LAMP_TYPES,
        ),
        advertised=("EXPOSE", "AUTO_FOCUS", "DARK", "BIAS", *LAMP_TYPES),
        filters=True,
    ),
)
INSTRUMENT_TYPES.register(
    "camera_spectrograph",
    InstrumentType(
        "camera_spectrograph",
        "SPECTRA",
        accepts=("EXPOSE", "DARK", "BIAS", *LAMP_TYPES),
    ),
)
