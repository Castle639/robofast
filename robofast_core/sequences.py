"""Config-defined sequences (docs/sequences.md, docs/design.md 3.10 (c)).

A site's or instrument's routine that is only an ordering of operations
robofast already drives -- the calibration stage, a lamp on and off, a
wait, exposures -- is a **sequence**: a named step list in the enclosure
config, run as an observation of kind ``sequence``
(:class:`~robofast_core.kinds.ObservationKind` ``"sequence"``; the
observation names it: ``{"kind": "sequence", "sequence": "tres_thar"}``)::

    "sequences": {
      "tres_thar": {
        "description": "TRES ThAr arcs",
        "instrument": "camera_spectrograph",
        "steps": [
          {"calstage": "in"},
          {"lamp": "ThAr1", "on": true, "warmup": 60},
          {"expose": {"type": "ARC", "exptime": 30, "n": 3}},
          {"lamp": "ThAr1", "on": false},
          {"calstage": "out"}
        ],
        "cleanup": [{"calstage": "out"}]
      }
    }

Each step is one of :data:`SEQUENCE_STEPS` (a registry: a plug-in adds its
own step types, docs/plugins.md), named by the one key of the step that is
a step type; the step's other keys are its options. The built-in steps:

* ``{"calstage": POSITION}`` -- the calibration stage to a named position;
* ``{"filter": NAME}`` -- the filter wheel;
* ``{"port": NAME}`` -- the M3 port (the telescope's ``port_action``);
* ``{"adc": "track" | "off"}`` -- the ADC tracking (settled) or neutral;
* ``{"lamp": NAME, "on": true|false, "warmup": s}`` -- a calibration lamp on
  the telescope's lamp switch (robofast_core/lamps.py; the switch's
  ``max_on`` still applies); ``warmup`` (default: the lamps entry's) is
  waited after switching on;
* ``{"focus": POSITION}`` or ``{"focus": {"delta": STEPS}}`` -- the
  telescope's focuser (only where it has an ``autofocus`` entry, so the
  sequencer may move it);
* ``{"wait": s}``;
* ``{"expose": {"type": T, "exptime": s, "n": N, "object": name}}`` -- ``N``
  frames with the telescope's camera, ``T`` one of ``BIAS``, ``DARK``,
  ``EXPOSE`` (sky light: the sequence is then a light observation, run on
  its target with the enclosure open), ``ARC`` and ``LAMP_FLAT`` (the
  shutter open on a lamp, which must be on);
* ``{"spectrograph": [{"kind": k, "exptime": s, "n": N}, ...]}`` -- the
  enclosure's spectrograph service's own calibration frames as one job.

**Cleanup.** A sequence's ``cleanup`` steps (only steps that cannot let
light in or move the telescope: a lamp off, the calibration stage, the
filter wheel, the ADC off, a wait) always run after its steps -- when they
finish, fail, or are cancelled (a lease revoked, the weather, a mode
switch, a preemption, the sequencer stopping) -- with grants of their own,
since a cancelled generation's grants are fenced. Every lamp the sequence
switched on is switched off first, whatever the cleanup says. On a
cancellation the lamps go off before the task ends and the rest of the
cleanup finishes in the background, waited for (bounded by
``sequencer.sequence_cleanup_timeout``) by the generation's own cleanup
before it parks; and that cleanup switches every lamp off again.

**Checks.** Every sequence is checked when the config is loaded, against
every telescope it may run on (``telescopes``, default all), and refused
with the path of the offending step: an unknown step type or option, a
device or lamp the telescope does not have, an exposure type the
``instrument`` (an instrument type, default ``imager``) does not take, an
``ARC`` or ``LAMP_FLAT`` frame with no lamp on, a lamp left on at the end,
a lamp kept on longer than its ``max_on`` (when the lamps entry gives
one), a cleanup step that is not safe. Before its first step the
sequencer checks it again against the devices themselves: the stage's and
the wheel's position names, the lamp switch's lamps and their ``max_on``.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from robofast_core.plugins import Registry

if TYPE_CHECKING:  # pragma: no cover
    from robofast_core.lamps import LampSetup

log = logging.getLogger(__name__)

__all__ = [
    "EXPOSE_TYPES",
    "SEQUENCE_STEPS",
    "Sequence",
    "SequenceCheck",
    "SequenceRun",
    "Step",
    "StepType",
    "sequences_from_config",
]

#: An ``expose`` step's frame types (the OCS configuration types).
EXPOSE_TYPES = ("BIAS", "DARK", "EXPOSE", "ARC", "LAMP_FLAT")
#: Their frames need a lamp on (the shutter opens on it, no sky light).
LAMP_FRAMES = ("ARC", "LAMP_FLAT")


@dataclass(frozen=True)
class Step:
    """One step: its type's name, its parsed options, where it is."""

    type: str
    params: Any
    where: str


@dataclass(frozen=True)
class Sequence:
    name: str
    steps: tuple[Step, ...]
    cleanup: tuple[Step, ...]
    #: An ``expose`` step takes sky light: a light observation.
    light: bool
    #: Lamps it switches on.
    lamps: tuple[str, ...]
    #: Telescopes it may run on (None: every one).
    telescopes: tuple[str, ...] | None
    instrument: str = "imager"
    description: str = ""
    #: Roughly how long it takes (s): waits, warm-ups, frames with their
    #: overhead; stage and wheel moves not counted.
    estimate: float = 0.0
    #: Telescope -> lamp -> about how long the steps keep it on (s): checked
    #: against the switch's ``max_on`` before the first step.
    lamp_time: dict[str, dict[str, float]] = field(default_factory=dict)

    def runs_on(self, telescope: str) -> bool:
        return self.telescopes is None or telescope in self.telescopes


@dataclass
class SequenceCheck:
    """The static check of one sequence on one telescope (its config
    entry), step by step: which lamps are on, for how long, light or not."""

    telescope: str
    entry: dict[str, Any]
    lamp_setup: LampSetup | None
    instrument: Any
    config: dict[str, Any]
    lamps_on: dict[str, float] = field(default_factory=dict)
    light: bool = False
    duration: float = 0.0
    #: Lamp -> seconds it stays on (the longest stretch).
    lamp_time: dict[str, float] = field(default_factory=dict)

    def frame_overhead(self) -> float:
        return self.lamp_setup.frame_overhead if self.lamp_setup is not None else 30.0

    def need(self, key: str, what: str, where: str) -> None:
        if self.entry.get(key) is None:
            raise ValueError(f"{where}: telescope {self.telescope!r} has no {what} ({key!r})")

    def elapse(self, seconds: float) -> None:
        self.duration += seconds
        for lamp in self.lamps_on:
            self.lamps_on[lamp] += seconds
            self.lamp_time[lamp] = max(self.lamp_time.get(lamp, 0.0), self.lamps_on[lamp])


class StepType:
    """One kind of step (module doc). Subclass it and register an instance
    in :data:`SEQUENCE_STEPS` under its :attr:`name`."""

    name: ClassVar[str] = ""
    #: The step's other keys, besides its own (``lamp``: ``on``, ``warmup``).
    options: ClassVar[tuple[str, ...]] = ()
    #: May it be a cleanup step (never lets light in, never moves the
    #: telescope)? A step may refine this per value (:meth:`safe`).
    cleanup: ClassVar[bool] = False

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        """The step's value and options, checked and normalized; ValueError
        naming ``where``."""
        if options:
            raise ValueError(f"{where}: unknown options {sorted(options)}")
        return value

    def safe(self, params: Any) -> bool:
        return self.cleanup

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        """Check the step on one telescope (its devices; the lamps on)."""

    async def preflight(self, params: Any, r: SequenceRun) -> None:
        """Before the sequence's first step: check it against the devices
        (reads only)."""

    async def run(self, params: Any, r: SequenceRun) -> None:
        raise NotImplementedError


#: Every sequence step type, by name.
SEQUENCE_STEPS: Registry[StepType] = Registry("sequence step")


@dataclass(eq=False)
class SequenceRun:
    """One run of a sequence in the sequencer: what a step's
    :meth:`StepType.run` gets."""

    sequencer: Any
    state: Any
    unit: Any
    tel: Any
    cam: Any
    obs: Any
    taken: list[Any]
    sequence: Sequence
    #: In the cleanup: grants of its own (the generation's may be fenced).
    cleaning: bool = False
    #: Lamps switched on by this run and not (yet) confirmed off: name -> switch id.
    lamps_on: dict[str, int] = field(default_factory=dict)
    #: The lamp switch's outlets by name (read at the preflight).
    outlets: dict[str, dict[str, Any]] = field(default_factory=dict)
    _grants: dict[str, Any] = field(default_factory=dict)

    @property
    def gen(self) -> int:
        return self.state.gen

    def event(self, what: str, **detail: Any) -> None:
        self.sequencer._event(
            what, gen=self.gen, telescope=self.unit.name, sequence=self.sequence.name, **detail
        )

    def grant(self, device: str) -> Any:
        """The device's grant: the generation's, or in the cleanup one of
        the cleanup's own (``device``: ``calstage``, ``filterwheel``,
        ``lamps``, ``adc``, ``focuser``). None: the telescope has none."""
        if not self.cleaning:
            gen_grants = {
                "calstage": self.state.calstages,
                "filterwheel": self.state.filterwheels,
                "lamps": self.state.lamps,
                "focuser": self.state.focusers,
            }.get(device)
            if gen_grants is not None:
                return gen_grants.get(self.unit.name)
        key = f"{device}/{'cleanup' if self.cleaning else 'run'}"
        g = self._grants.get(key)
        if g is None:
            owner = getattr(self.unit, device, None)
            if owner is None:
                return None
            how = "sequence-cleanup" if self.cleaning else "sequence"
            g = self._grants[key] = owner.grant(
                f"{self.sequencer.name}/{how}#{self.gen}:{self.sequence.name}"
            )
            if not self.cleaning:
                self.state.extra_grants.append(g)  # fenced with the generation's
        return g

    def revoke(self, reason: str) -> None:
        for g in self._grants.values():
            g.revoke(reason)
        self._grants.clear()


# -- loading and checking ------------------------------------------------------------


def _step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where}: a step is {{type: value, ...}} (docs/sequences.md)")
    raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    names = [k for k in raw if k in SEQUENCE_STEPS]
    if len(names) != 1:
        if not names:
            raise ValueError(
                f"{where}: unknown step {sorted(raw)}"
                f" (step types: {', '.join(SEQUENCE_STEPS.names())})"
            )
        raise ValueError(f"{where}: one step type per step, not {names}")
    st = SEQUENCE_STEPS[names[0]]
    options = {k: v for k, v in raw.items() if k != names[0]}
    unknown = set(options) - set(st.options)
    if unknown:
        raise ValueError(
            f"{where}: {names[0]} takes no option {sorted(unknown)}"
            + (f" (its options: {', '.join(st.options)})" if st.options else "")
        )
    return Step(names[0], st.parse(raw[names[0]], options, where), where)


def sequences_from_config(config: dict[str, Any], *, where: str = "") -> dict[str, Sequence]:
    """The enclosure config's ``sequences`` (module doc), each checked on
    every telescope it may run on. ValueError naming the step's path."""
    from robofast_core.config import telescopes
    from robofast_core.instruments import INSTRUMENT_TYPES
    from robofast_core.lamps import LampSetup

    pre = f"{where}: " if where else ""
    raw = config.get("sequences")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{pre}sequences: {{name: {{'steps': [...]}}}} (docs/sequences.md)")
    tels = telescopes(config)
    setups: dict[str, LampSetup | None] = {}
    for tname, entry in tels.items():
        try:
            setups[tname] = (
                None if entry.get("lamps") is None else LampSetup.from_dict(entry["lamps"])
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{pre}telescopes.{tname}: {exc}") from exc
    out: dict[str, Sequence] = {}
    for name, d in raw.items():
        if str(name).startswith("_"):
            continue
        at = f"{pre}sequences.{name}"
        if not isinstance(name, str) or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"{at}: a sequence name is letters, digits, '_' and '-'")
        if isinstance(d, list):
            d = {"steps": d}
        if not isinstance(d, dict):
            raise ValueError(f"{at}: {{'steps': [...], 'cleanup': [...]}} or a list of steps")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        unknown = set(d) - {"steps", "cleanup", "telescopes", "instrument", "description"}
        if unknown:
            raise ValueError(f"{at}: unknown keys {sorted(unknown)}")
        steps_raw, cleanup_raw = d.get("steps"), d.get("cleanup", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValueError(f"{at}.steps: a list of steps")
        if not isinstance(cleanup_raw, list):
            raise ValueError(f"{at}.cleanup: a list of steps")
        steps = tuple(_step(s, f"{at}.steps[{i}]") for i, s in enumerate(steps_raw))
        cleanup = tuple(_step(s, f"{at}.cleanup[{i}]") for i, s in enumerate(cleanup_raw))
        for s in cleanup:
            if not SEQUENCE_STEPS[s.type].safe(s.params):
                raise ValueError(
                    f"{s.where}: not a cleanup step (a cleanup never lets light in or moves"
                    " the telescope: lamps off, calstage, filter, adc off, wait)"
                )
        on_tels = d.get("telescopes")
        if on_tels is not None:
            if not isinstance(on_tels, list) or not all(isinstance(t, str) for t in on_tels):
                raise ValueError(f"{at}.telescopes: a list of telescope names")
            bad = set(on_tels) - set(tels)
            if bad:
                raise ValueError(
                    f"{at}.telescopes: unknown {sorted(bad)} (the config has {sorted(tels)})"
                )
        inst_name = d.get("instrument", "imager")
        if not isinstance(inst_name, str):
            raise ValueError(f"{at}.instrument: an instrument type")
        instrument = INSTRUMENT_TYPES.lookup(inst_name, f"{at}.instrument")
        description = d.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"{at}.description: text")
        checks = []
        for tname in on_tels or list(tels):
            c = SequenceCheck(tname, tels[tname], setups[tname], instrument, config)
            for s in steps:
                SEQUENCE_STEPS[s.type].check(s.params, c, f"{s.where} (on {tname})")
            if c.lamps_on:
                raise ValueError(
                    f"{at}.steps: leaves lamp(s) {sorted(c.lamps_on)} on (on {tname}): end with"
                    ' {"lamp": NAME, "on": false}'
                )
            for lamp, seconds in c.lamp_time.items():
                spec = c.lamp_setup.lamps.get(lamp) if c.lamp_setup is not None else None
                limit = getattr(spec, "max_on", None)
                if limit is not None and seconds > limit:
                    raise ValueError(
                        f"{at}.steps: keeps lamp {lamp} on for about {seconds:.0f} s, more than"
                        f" its max_on {limit:g} s (on {tname}): split it"
                    )
            cc = SequenceCheck(tname, tels[tname], setups[tname], instrument, config)
            for s in cleanup:
                SEQUENCE_STEPS[s.type].check(s.params, cc, f"{s.where} (on {tname})")
            checks.append(c)
        lamps = sorted({s.params["lamp"] for s in steps if s.type == "lamp" and s.params["on"]})
        out[name] = Sequence(
            name,
            steps,
            cleanup,
            light=any(c.light for c in checks),
            lamps=tuple(lamps),
            telescopes=None if on_tels is None else tuple(on_tels),
            instrument=inst_name,
            description=description,
            estimate=max((c.duration for c in checks), default=0.0),
            lamp_time={c.telescope: dict(c.lamp_time) for c in checks},
        )
    return out


# -- the built-in steps ----------------------------------------------------------------


def _name(value: Any, what: str, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {what}: a name")
    return value


def _seconds(value: Any, where: str, hi: float = 86400.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where}: seconds, a number")
    if not 0 <= value <= hi:
        raise ValueError(f"{where}: 0 to {hi:g} s")
    return float(value)


class _Named(StepType):
    """Move an Alpaca FilterWheel (the calibration stage, the filter wheel)
    to a named position."""

    device: ClassVar[str] = ""
    what: ClassVar[str] = ""
    cleanup = True

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        return _name(value, self.what, where)

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.need(self.device, self.what, where)

    async def preflight(self, params: Any, r: SequenceRun) -> None:
        g = r.grant(self.device)
        if g is None:
            raise _unsupported(f"{r.unit.name} has no {self.what} ({params!r})")
        names = list(await g.get("Names"))
        if params not in names:
            raise _unsupported(f"{r.unit.name}: no {self.what} position {params!r} in {names}")

    def timeout(self, r: SequenceRun) -> float:
        raise NotImplementedError

    async def run(self, params: Any, r: SequenceRun) -> None:
        g = r.grant(self.device)
        if g is None:
            raise _unsupported(f"{r.unit.name} has no {self.what} ({params!r})")
        position = await r.sequencer._select_named(g, r.unit, params, self.what, self.timeout(r))
        r.event(f"{self.name}_selected", **{self.name: params})
        if self.device == "filterwheel" and r.sequencer.imaging is not None:
            r.sequencer.imaging.filter_selected(r.unit.name, params, position)


class Calstage(_Named):
    name = "calstage"
    device = "calstage"
    what = "calibration stage"

    def timeout(self, r: SequenceRun) -> float:
        return r.sequencer.config.calstage_timeout


class Filter(_Named):
    name = "filter"
    device = "filterwheel"
    what = "filter wheel"

    def timeout(self, r: SequenceRun) -> float:
        return r.sequencer.config.filter_timeout


class Port(StepType):
    name = "port"

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        return _name(value, "port", where)

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.need("port_action", "M3 port action", where)

    async def run(self, params: Any, r: SequenceRun) -> None:
        await r.sequencer._select_port(r.state, r.unit, r.tel, params)


class ADC(StepType):
    name = "adc"
    cleanup = True

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        if value not in ("track", "off"):
            raise ValueError(f"{where}: adc: 'track' or 'off'")
        return value

    def safe(self, params: Any) -> bool:
        return params == "off"

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.need("adc", "ADC", where)

    async def run(self, params: Any, r: SequenceRun) -> None:
        if params == "track":
            await r.sequencer._adc_track(r.state, r.unit)
            return
        g = r.grant("adc")
        if g is None:
            raise _unsupported(f"{r.unit.name} has no ADC")
        await g.call("SetSwitch", await r.unit.adc_switches.id(g, "tracking"), False)
        r.event("adc_off")


class Lamp(StepType):
    """A lamp on (then its warm-up) or off."""

    name = "lamp"
    options = ("on", "warmup")
    cleanup = True

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        lamp = _name(value, "lamp", where)
        on = options.get("on")
        if not isinstance(on, bool):
            raise ValueError(f'{where}: lamp {lamp}: say "on": true or false')
        warmup = options.get("warmup")
        if warmup is not None:
            if not on:
                raise ValueError(f"{where}: lamp {lamp}: a warm-up is for switching on")
            warmup = _seconds(warmup, f"{where}.warmup", 3600.0)
        return {"lamp": lamp, "on": on, "warmup": warmup}

    def safe(self, params: Any) -> bool:
        return not params["on"]

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.need("lamps", "lamp switch", where)
        assert c.lamp_setup is not None
        lamp = params["lamp"]
        spec = c.lamp_setup.lamps.get(lamp)
        if spec is None:
            raise ValueError(
                f"{where}: no lamp {lamp!r} on {c.telescope} (it has {sorted(c.lamp_setup.lamps)})"
            )
        if params["on"]:
            if lamp in c.lamps_on:
                raise ValueError(f"{where}: lamp {lamp} is on already")
            c.lamps_on[lamp] = 0.0
            c.elapse(spec.warmup if params["warmup"] is None else params["warmup"])
        else:
            c.lamps_on.pop(lamp, None)

    async def preflight(self, params: Any, r: SequenceRun) -> None:
        if r.unit.lamps is None or r.unit.lamp_setup is None:
            raise _unsupported(f"{r.unit.name} has no lamps (lamp {params['lamp']!r})")
        outlet = r.outlets.get(params["lamp"])
        if outlet is None or not outlet.get("lamp"):
            raise _unsupported(f"{r.unit.name}: its lamp switch has no lamp {params['lamp']!r}")

    async def run(self, params: Any, r: SequenceRun) -> None:
        setup, lamp = r.unit.lamp_setup, params["lamp"]
        outlet = r.outlets[lamp]
        if not params["on"]:
            await r.sequencer._sequence_lamp_off(r, lamp, int(outlet["id"]))
            return
        g = r.grant("lamps")
        r.lamps_on[lamp] = int(outlet["id"])  # before the command: the cleanup tries too
        await g.call("SetSwitch", outlet["id"], True, timeout=setup.switch_timeout)
        r.event("lamp_on", lamp=lamp)
        warmup = setup.lamps[lamp].warmup if params["warmup"] is None else params["warmup"]
        if warmup > 0:
            r.event("lamp_warmup", lamp=lamp, seconds=warmup)
            await asyncio.sleep(warmup)


class Focus(StepType):
    name = "focus"

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        if isinstance(value, dict):
            if set(value) != {"delta"} or isinstance(value["delta"], bool):
                raise ValueError(f"{where}: focus: a position, or {{'delta': steps}}")
            if not isinstance(value["delta"], int | float):
                raise ValueError(f"{where}: focus.delta: a number")
            return {"delta": float(value["delta"])}
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{where}: focus: a position, or {{'delta': steps}}")
        return {"position": float(value)}

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.need("focuser", "focuser", where)
        c.need("autofocus", "autofocus entry (the sequencer moves a focuser only then)", where)

    async def run(self, params: Any, r: SequenceRun) -> None:
        g = r.grant("focuser")
        if g is None:
            raise _unsupported(f"{r.unit.name}: the sequencer moves no focuser here")
        target = params.get("position")
        if target is None:
            target = float(await g.get("Position")) + params["delta"]
        from robofast_core.sequencer import _no_exposures

        af = r.sequencer._focus_runner(r.state, r.unit, None, None, None, exposer=_no_exposures)
        await af.move(target)
        r.event("focus_moved", position=target)


class Wait(StepType):
    name = "wait"
    cleanup = True

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        return _seconds(value, where)

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        c.elapse(params)

    async def run(self, params: Any, r: SequenceRun) -> None:
        await asyncio.sleep(params)


class Expose(StepType):
    """Frames with the telescope's camera (module doc)."""

    name = "expose"

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expose: {{'type': ..., 'exptime': s, 'n': N}}")
        unknown = set(value) - {"type", "exptime", "n", "object"}
        if unknown:
            raise ValueError(f"{where}: expose: unknown keys {sorted(unknown)}")
        t = value.get("type")
        if t not in EXPOSE_TYPES:
            raise ValueError(f"{where}: expose.type: one of {', '.join(EXPOSE_TYPES)}")
        exptime = 0.0 if t == "BIAS" else _seconds(value.get("exptime"), f"{where}.exptime")
        if t == "BIAS" and value.get("exptime", 0) not in (0, 0.0):
            raise ValueError(f"{where}: a BIAS has no exposure time")
        n = value.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 1000:
            raise ValueError(f"{where}: expose.n: 1 to 1000 frames")
        obj = value.get("object")
        if obj is not None and (not isinstance(obj, str) or not obj):
            raise ValueError(f"{where}: expose.object: a name")
        return {"type": t, "exptime": exptime, "n": n, "object": obj}

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        t = params["type"]
        if t not in c.instrument.accepts:
            raise ValueError(
                f"{where}: instrument {c.instrument.name!r} takes no {t} frames"
                f" (it takes {', '.join(c.instrument.accepts)})"
            )
        if t in LAMP_FRAMES and not c.lamps_on:
            raise ValueError(f"{where}: a {t} frame needs a lamp on (a lamp step before it)")
        if t == "EXPOSE":
            c.light = True
        c.elapse(params["n"] * (params["exptime"] + c.frame_overhead()))

    async def run(self, params: Any, r: SequenceRun) -> None:
        from robofast_core.plan import Observation

        t = params["type"]
        obs = r.obs
        base = dict(
            exptime=params["exptime"],
            count=params["n"],
            object=params["object"] or obs.object or obs.name,
        )
        name = f"{obs.name}:{r.sequence.name}"
        lamp_names = list(r.lamps_on)
        if t in LAMP_FRAMES:
            if not lamp_names:
                raise _unsupported(f"{obs.name}: a {t} frame with no lamp on")
            kind = "arc" if t == "ARC" else "lamp_flat"
            frame = Observation(name, light=False, kind=kind, lamp=lamp_names[0], **base)
        else:
            frame = Observation(
                name, light=t == "EXPOSE", guide=obs.guide and t == "EXPOSE", **base
            )
        check = r.sequencer._lamps_still_on(r) if t in LAMP_FRAMES else None
        await r.sequencer._expose_frames(r.state, r.unit, r.tel, r.cam, frame, r.taken, check=check)
        if check is not None:
            await check()  # the last frame had its lamp throughout


class Spectrograph(StepType):
    """The spectrograph service's own calibration frames, as one job."""

    name = "spectrograph"

    def parse(self, value: Any, options: dict[str, Any], where: str) -> Any:
        if not isinstance(value, list) or not value:
            raise ValueError(f"{where}: spectrograph: [{{'kind': ..., 'exptime': s, 'n': N}}]")
        frames = []
        for i, f in enumerate(value):
            at = f"{where}.spectrograph[{i}]"
            if not isinstance(f, dict) or set(f) - {"kind", "exptime", "n"} or "kind" not in f:
                raise ValueError(f"{at}: {{'kind': ..., 'exptime': s, 'n': N}}")
            n = f.get("n", 1)
            if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                raise ValueError(f"{at}.n: a positive number of frames")
            frames.append(
                {
                    "kind": f["kind"],
                    "exptime": _seconds(f.get("exptime", 0), f"{at}.exptime"),
                    "n": n,
                }
            )
        return frames

    def check(self, params: Any, c: SequenceCheck, where: str) -> None:
        from robofast_core.instruments import calibration_kinds

        if c.config.get("spectrograph") is None:
            raise ValueError(f"{where}: the enclosure has no spectrograph")
        kinds = calibration_kinds()
        for f in params:
            if f["kind"] not in kinds:
                raise ValueError(f"{where}: spectrograph frame kind {f['kind']!r} not in {kinds}")
            c.elapse(f["n"] * f["exptime"])

    async def run(self, params: Any, r: SequenceRun) -> None:
        from robofast_core.plan import Observation

        obs = Observation(
            f"{r.obs.name}:{r.sequence.name}",
            exptime=0.0,
            light=False,
            kind="spectrograph_calibration",
            calibration=[dict(f) for f in params],
        )
        await r.sequencer._spectrograph_calibration(r.state, r.unit, obs, r.taken)


def _unsupported(text: str) -> Exception:
    from robofast_core.sequencer import Unsupported

    return Unsupported(text)


for _st in (Calstage(), Filter(), Port(), ADC(), Lamp(), Focus(), Wait(), Expose(), Spectrograph()):
    SEQUENCE_STEPS.register(_st.name, _st)
del _st

#: How the sequencer turns an observation's sequence name into its definition.
SequenceLookup = Callable[[str], Sequence | None]
