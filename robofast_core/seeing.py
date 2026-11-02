"""Where the seeing comes from (docs/seeing.md).

The seeing (FWHM, arcsec) feeds the guiding recommendation (``guide:
"auto"``, :mod:`robofast_core.guider.recommend`) and the dispatch
scheduler's ``max_seeing`` constraint. A **seeing source** says what it is
now; which source is configuration (a dispatch plan's ``seeing`` entry)::

    "seeing": {
      "source": "guider",       # a SEEING_SOURCES key (default: guider)
      "window": 1800,           # ...the source's own options
      "max_age": 900,
      "min_samples": 3,
      "fallback": null,         # arcsec when the source has nothing recent
      "unknown": "pass"         # max_seeing with no seeing: pass or fail
    }

Built-in sources (:data:`SEEING_SOURCES`; a plug-in adds its own, e.g. a
DIMM or a weather feed, docs/plugins.md):

* ``guider`` (the default) -- the guiders' live FWHM: the ``guide_seeing``
  values the guide loops publish while guiding (one per telescope, from a
  whole star, never the spill round a fiber), collected from the
  sequencer's value registry; at most one sample per telescope every
  ``sample_interval`` s, the **median** of those in the last ``window`` s
  (all telescopes together: the site's seeing), times ``factor`` (the
  guide optics' own blur, if a site calibrates it). None (so the fallback)
  with fewer than ``min_samples`` or when the newest is older than
  ``max_age``.
* ``fixed`` -- a constant ``value`` (arcsec).

With no recent value the **fallback** answers: for ``guide: "auto"`` the
instrument's guiding model's ``seeing``; for ``max_seeing`` the entry's
``fallback`` (default none, so ``unknown`` decides).
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from robofast_core.plugins import Registry

log = logging.getLogger(__name__)

__all__ = [
    "SEEING_SOURCES",
    "FixedSeeing",
    "GuiderSeeing",
    "Seeing",
    "SeeingEstimator",
    "SeeingSource",
    "seeing_from_config",
]

#: The registry value the guide loops publish (robofast_core.guider.loop).
GUIDER_VALUE = "guide_seeing"
UNKNOWN = ("pass", "fail")


@dataclass(frozen=True)
class Seeing:
    """A seeing estimate: ``value`` (FWHM, arcsec), the ``source`` it came
    from, how many samples and the newest one's age (s), if it has any."""

    value: float
    source: str
    samples: int = 0
    age: float | None = None
    note: str = ""

    def as_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"value": round(self.value, 3), "source": self.source}
        if self.samples:
            d["samples"] = self.samples
        if self.age is not None:
            d["age"] = round(self.age, 1)
        if self.note:
            d["note"] = self.note
        return d


class SeeingSource(Protocol):
    """What a source provides. ``bind`` (optional) gets the sequencer's
    value registry (:class:`~robofast_core.registry.ValueRegistry`)."""

    name: str

    def current(self, now: datetime) -> Seeing | None: ...


#: ``factory(options, clock) -> SeeingSource``; ``options`` is the
#: ``seeing`` entry without ``source``, ``fallback`` and ``unknown``.
SeeingFactory = Callable[[dict[str, Any], Callable[[], datetime]], SeeingSource]
SEEING_SOURCES: Registry[SeeingFactory] = Registry("seeing source")


def _positive(d: dict[str, Any], key: str, default: float, what: str) -> float:
    v = d.pop(key, default)
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"{what}.{key}: a number > 0")
    return float(v)


def _no_more(d: dict[str, Any], what: str) -> None:
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    if d:
        raise ValueError(f"{what}: unknown keys {sorted(d)}")


class GuiderSeeing:
    """The guiders' live FWHM (module doc)."""

    name = "guider"

    def __init__(
        self,
        *,
        window: float = 1800.0,
        max_age: float = 900.0,
        min_samples: int = 3,
        sample_interval: float = 30.0,
        factor: float = 1.0,
        telescopes: list[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.window = window
        self.max_age = max_age
        self.min_samples = min_samples
        self.sample_interval = sample_interval
        self.factor = factor
        self.telescopes = None if telescopes is None else set(telescopes)
        self.clock = clock or (lambda: datetime.now(UTC))
        #: (received, telescope, FWHM arcsec), oldest first.
        self.samples: deque[tuple[datetime, str, float]] = deque(maxlen=10000)
        self._last: dict[str, datetime] = {}

    @classmethod
    def from_options(cls, d: dict[str, Any], clock: Callable[[], datetime]) -> GuiderSeeing:
        d, what = dict(d), "seeing"
        kw: dict[str, Any] = {
            k: _positive(d, k, default, what)
            for k, default in (
                ("window", 1800.0),
                ("max_age", 900.0),
                ("sample_interval", 30.0),
                ("factor", 1.0),
            )
        }
        n = d.pop("min_samples", 3)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"{what}.min_samples: an integer >= 1")
        tel = d.pop("telescopes", None)
        if tel is not None and (
            not isinstance(tel, list) or not all(isinstance(t, str) for t in tel)
        ):
            raise ValueError(f"{what}.telescopes: a list of telescope names")
        _no_more(d, what)
        return cls(min_samples=n, telescopes=tel, clock=clock, **kw)

    def bind(self, registry: Any) -> bool:
        """Collect the guiders' ``guide_seeing`` from ``registry``. True:
        this source wants the guiders' values in that registry."""
        registry.subscribe(self._on_value)
        return True

    def _on_value(self, v: Any) -> None:
        scope, _, name = v.name.partition(".")
        if name != GUIDER_VALUE:
            return
        if self.telescopes is not None and scope not in self.telescopes:
            return
        self.add(scope, v.value, self.clock())

    def add(self, telescope: str, fwhm: Any, when: datetime) -> bool:
        """One guider's FWHM (arcsec) at ``when`` (the scheduler's clock);
        False if it was not kept (not a sane number, or too soon after
        that telescope's last)."""
        if isinstance(fwhm, bool) or not isinstance(fwhm, int | float):
            return False
        if not (math.isfinite(fwhm) and 0 < fwhm < 60):
            return False
        last = self._last.get(telescope)
        if last is not None and 0 <= (when - last).total_seconds() < self.sample_interval:
            return False
        self._last[telescope] = when
        self.samples.append((when, telescope, float(fwhm)))
        return True

    def current(self, now: datetime) -> Seeing | None:
        while self.samples and (now - self.samples[0][0]).total_seconds() > self.window:
            self.samples.popleft()
        recent = [s for s in self.samples if (now - s[0]).total_seconds() >= -1.0]
        if len(recent) < self.min_samples:
            return None
        age = (now - max(s[0] for s in recent)).total_seconds()
        if age > self.max_age:
            return None
        value = statistics.median(s[2] for s in recent) * self.factor
        return Seeing(value, self.name, len(recent), max(age, 0.0))


class FixedSeeing:
    """A constant (module doc)."""

    name = "fixed"

    def __init__(self, value: float) -> None:
        self.value = value

    @classmethod
    def from_options(cls, d: dict[str, Any], clock: Callable[[], datetime]) -> FixedSeeing:
        d = dict(d)
        if "value" not in d:
            raise ValueError("seeing.value: the seeing (arcsec) of a 'fixed' source")
        value = _positive(d, "value", 0.0, "seeing")
        _no_more(d, "seeing")
        return cls(value)

    def current(self, now: datetime) -> Seeing | None:
        return Seeing(self.value, self.name)


SEEING_SOURCES.register("guider", GuiderSeeing.from_options)
SEEING_SOURCES.register("fixed", FixedSeeing.from_options)


class SeeingEstimator:
    """A source with its fallback (module doc): what the dispatch scheduler
    asks."""

    def __init__(
        self, source: SeeingSource, fallback: float | None = None, unknown: str = "pass"
    ) -> None:
        self.source = source
        self.fallback = fallback
        self.unknown = unknown

    def bind(self, registry: Any) -> bool:
        bind = getattr(self.source, "bind", None)
        return bool(bind(registry)) if bind is not None else False

    def current(self, now: datetime, fallback: float | None = None) -> Seeing | None:
        """The source's seeing; without a recent one, ``fallback`` (else the
        configured one) marked as such; None if there is neither."""
        try:
            est = self.source.current(now)
        except Exception as exc:
            log.warning("seeing source %s: %r (fallback used)", self.source.name, exc)
            est = None
        if est is not None and math.isfinite(est.value) and est.value > 0:
            return est
        fb = self.fallback if fallback is None else fallback
        if fb is None:
            return None
        return Seeing(fb, "fallback", note=f"{self.source.name}: nothing recent")


def seeing_from_config(
    d: dict[str, Any] | None, clock: Callable[[], datetime] | None = None
) -> SeeingEstimator:
    """A :class:`SeeingEstimator` from a dispatch plan's ``seeing`` entry
    (module doc); None or ``{}``: the guiders, no fallback, ``unknown``
    pass. Errors name the key."""
    clock = clock or (lambda: datetime.now(UTC))
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise ValueError("seeing: {source, ...} (docs/seeing.md)")
    d = dict(d)
    kind = d.pop("source", "guider")
    fallback = d.pop("fallback", None)
    if fallback is not None:
        fallback = _positive({"fallback": fallback}, "fallback", 0.0, "seeing")
    unknown = d.pop("unknown", "pass")
    if unknown not in UNKNOWN:
        raise ValueError(f"seeing.unknown: one of {', '.join(UNKNOWN)}")
    factory = SEEING_SOURCES.lookup(kind, "seeing.source")
    return SeeingEstimator(factory(d, clock), fallback, unknown)
