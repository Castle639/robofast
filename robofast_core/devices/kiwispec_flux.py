"""Spectrum flux from the fiber-acquisition units' guide frames.

KiwiSpec's legacy exposure meter (a PMT) is not used. The flux record for
the flux-weighted exposure midpoint and for count termination comes from
the **FAU guide cameras** instead (docs/kiwispec.md, "Flux from the FAU
guide frames"): with the fiber located on each guide camera (the backlight,
``POST /backlight``), every guide frame's counts inside the fiber's
footprint measure the light reaching the fiber from that telescope. Up to
four telescopes feed the fiber at once; their flux is summed.

Each guider service reports its frames to the KiwiSpec service
(``POST /flux``)::

    {"telescope": "T1", "frames": [[t_start, t_end, counts], ...]}

``t_start``/``t_end`` are the guide exposure's start and end (Unix seconds,
UTC) and ``counts`` the background-subtracted counts inside the fiber
aperture (:func:`robofast_core.guider.loop.fiber_counts`). A frame with no
measurement (the star lost) is reported with ``counts`` 0 or simply not
reported.

**A relative scale.** MINERVA's FAU is a beamsplitter: the guide camera sees
a fixed fraction of the light that reaches the fiber's position, not the
light that actually couples into the fiber (fiber-coupling losses from
focus, seeing and centring are not seen). The counts are therefore a
relative flux: good for the time weighting of the midpoint, and for a
termination threshold calibrated in these same units, but not a measure of
the light in the spectrum.

The math
--------
A frame's counts arrive at a constant rate ``counts / (t_end - t_start)``.
The gaps between frames (readout, processing) are bridged: each frame's rate
is taken to hold from halfway to the previous frame until halfway to the
next one, as long as the gap is at most ``max_gap`` seconds (a longer gap is
left uncovered: nothing is known about it). Within an exposure ``[start,
end]`` the counts are the integral of that piecewise-constant rate, and the
midpoint is ``integral(t R(t)) / integral(R(t))``, summed over telescopes
(:func:`fau_flux`). ``coverage`` is the fraction of the exposure each
telescope's series covers.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: A reported frame: (start, end, counts) in Unix seconds and counts.
FluxFrame = tuple[float, float, float]


def _num(value: Any) -> float:
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("flux frame values must be finite")
    return x


def parse_frames(frames: Any, max_frames: int = 10000) -> list[FluxFrame]:
    """Validate a ``POST /flux`` body's ``frames`` (raises ValueError)."""
    if not isinstance(frames, list):
        raise ValueError("frames must be a list of [t_start, t_end, counts]")
    if len(frames) > max_frames:
        raise ValueError(f"at most {max_frames} frames per report")
    out: list[FluxFrame] = []
    for f in frames:
        if not isinstance(f, (list, tuple)) or len(f) != 3:
            raise ValueError(f"a frame is [t_start, t_end, counts], not {f!r}")
        try:
            t0, t1, c = (_num(v) for v in f)
        except (TypeError, ValueError):
            raise ValueError(f"a frame is [t_start, t_end, counts] numbers, not {f!r}") from None
        if t1 <= t0:
            raise ValueError(f"frame ends before it starts: {f!r}")
        if c < 0:
            c = 0.0  # a background-dominated frame: no light, not negative light
        out.append((t0, t1, c))
    return out


def _segments(frames: list[FluxFrame], max_gap: float) -> list[tuple[float, float, float]]:
    """Each frame as ``(a, b, rate)``, its interval widened to bridge gaps."""
    fs = sorted(frames)
    segs: list[tuple[float, float, float]] = []
    for i, (t0, t1, c) in enumerate(fs):
        rate = c / (t1 - t0)
        a, b = t0, t1
        if i > 0:
            prev_end = fs[i - 1][1]
            gap = t0 - prev_end
            if 0 < gap <= max_gap:
                a = t0 - gap / 2
        if i + 1 < len(fs):
            gap = fs[i + 1][0] - t1
            if 0 < gap <= max_gap:
                b = t1 + gap / 2
        if segs and a < segs[-1][1]:
            a = segs[-1][1]  # overlapping reports: do not count twice
        if b > a:
            segs.append((a, b, rate))
    return segs


@dataclass
class FluxResult:
    #: Summed relative counts in ``[start, end]``.
    counts: float
    #: Flux-weighted midpoint, Unix seconds (None: no flux).
    midpoint: float | None
    #: Per telescope: counts, frames used and coverage (0..1).
    telescopes: dict[str, dict[str, float]] = field(default_factory=dict)


def fau_flux(
    series: dict[str, list[FluxFrame]], start: float, end: float, max_gap: float = 10.0
) -> FluxResult:
    """The summed flux and flux-weighted midpoint of ``[start, end]`` (module doc)."""
    total = weighted = 0.0
    per: dict[str, dict[str, float]] = {}
    span = max(end - start, 1e-9)
    for tel, frames in series.items():
        counts = wsum = covered = 0.0
        used = 0
        for a, b, rate in _segments(frames, max_gap):
            lo, hi = max(a, start), min(b, end)
            if hi <= lo:
                continue
            part = rate * (hi - lo)
            counts += part
            wsum += part * (lo + hi) / 2
            covered += hi - lo
            used += 1
        per[tel] = {"counts": counts, "frames": used, "coverage": min(1.0, covered / span)}
        total += counts
        weighted += wsum
    return FluxResult(total, weighted / total if total > 0 else None, per)


class FluxLog:
    """Recent FAU frames per telescope (bounded), as reported."""

    def __init__(self, max_frames: int = 20000, max_age: float = 4 * 3600.0) -> None:
        self.max_age = max_age
        self._frames: dict[str, deque[FluxFrame]] = {}
        self._max = max_frames
        self._lock = threading.Lock()
        #: Per telescope: when the last report came in (Unix s) and how many frames.
        self.reports: dict[str, dict[str, float]] = {}

    def add(self, telescope: str, frames: list[FluxFrame], now: float) -> int:
        with self._lock:
            d = self._frames.setdefault(telescope, deque(maxlen=self._max))
            d.extend(frames)
            while d and d[0][1] < now - self.max_age:
                d.popleft()
            r = self.reports.setdefault(telescope, {"frames": 0.0})
            r["at"] = now
            r["frames"] += len(frames)
            if frames:
                r["last_frame_end"] = max(f[1] for f in frames)
            return len(d)

    def window(self, telescopes: list[str], start: float, end: float) -> dict[str, list[FluxFrame]]:
        """The frames of ``telescopes`` overlapping ``[start, end]`` (with a
        frame either side, for bridging)."""
        out: dict[str, list[FluxFrame]] = {}
        with self._lock:
            for t in telescopes:
                fs = sorted(self._frames.get(t, ()))
                out[t] = [f for f in fs if f[1] >= start - 60 and f[0] <= end + 60]
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {t: dict(r) for t, r in self.reports.items()}
