"""Offset guiding: guide on a nearby star, and hold it where the target
falls on the fiber (docs/offset_guiding.md).

On-target ("annulus") guiding centroids the target's own light spilling
around the fiber hole; for a faint target that spill is too weak to
centroid. Offset guiding instead puts a brighter star -- the *guide star*
-- at the pixel that places the target on the fiber:

    setpoint = fiber + S^-1 @ (east, north)

where (east, north) is the guide star's position relative to the target
(arcsec on the sky, at the time of observation) and ``S`` the guide
camera's sky matrix (plate scale, PA, parity: :func:`fits_sky_matrix`, the
PA calibration's convention). The guide loop then keeps the guide star
there and the target on the fiber, through every disturbance they share
(seeing tilt, drift, flexure of the mount).

What this module adds on top of that geometry:

* **proper motion**: both stars are moved to the epoch of observation
  (:func:`robofast_core.catalog.propagate`) before their offset is taken;
* **differential refraction** across the offset (:func:`refraction_shift`):
  the atmosphere lifts the lower of the two stars by more; ERFA's
  ``refco`` refraction constants, so it costs nothing to correct (it is
  always computed, logged, and applied unless ``refraction`` is off);
* **field rotation**: the setpoint is recomputed every ``update`` seconds
  (:class:`OffsetGuide`), with the PA of the moment -- constant for an
  equatorial mount or a derotated port (MINERVA's FAU rotator in derotate
  mode, the 60-inch), or following the parallactic angle on an alt-az
  port without a derotator (``field_rotation``).

The guide star comes from one of: an explicit offset from the target
(``{"east", "north"}`` arcsec, the guide star relative to the target: no
proper motion of its own, the offset is taken as fixed), explicit
coordinates (``{"ra", "dec", "pmra", "pmdec", "epoch", "g"}``), or
``"auto"`` -- the best star of the catalog in the guide camera's field
(:func:`choose_guide_star`: bright but not saturating, isolated, close to
the fiber).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from robofast_core.catalog import _tan, _untan, decimal_year, propagate, separation
from robofast_core.guider.calibration import fits_sky_matrix

log = logging.getLogger(__name__)

__all__ = [
    "AutoConfig",
    "GuideStar",
    "OffsetCheck",
    "OffsetGuide",
    "OffsetSite",
    "OffsetVerdict",
    "TargetPosition",
    "check_offset_star",
    "choose_guide_star",
    "guide_star_from_spec",
    "pixel_offset",
    "refraction_shift",
    "sky_offset",
]

#: Field-rotation models (:class:`OffsetGuide`).
FIELD_ROTATION = ("none", "altaz")


@dataclass(frozen=True)
class TargetPosition:
    """The science target: ICRS (deg) at ``epoch`` (Julian year), proper
    motion mas/yr (``pmra`` with cos dec)."""

    ra: float
    dec: float
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float = 2000.0

    def at(self, when: datetime | float) -> tuple[float, float]:
        ep = decimal_year(when)
        assert ep is not None
        ra, dec = propagate(self.ra, self.dec, self.pmra, self.pmdec, ep - self.epoch)
        return float(ra), float(dec)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TargetPosition:
        return cls(
            float(d["ra"]),
            float(d["dec"]),
            float(d.get("pmra") or 0.0),
            float(d.get("pmdec") or 0.0),
            float(d.get("epoch") or 2000.0),
        )


@dataclass(frozen=True)
class GuideStar:
    """The star the guider holds. ``source``: ``"offset"`` (an explicit
    offset from the target: ``east``/``north`` arcsec, moving with the
    target), ``"coordinates"`` (given) or ``"catalog"`` (chosen
    automatically; ``source_id`` is Gaia's)."""

    source: str
    ra: float | None = None
    dec: float | None = None
    pmra: float = 0.0
    pmdec: float = 0.0
    epoch: float = 2000.0
    g: float | None = None
    east: float | None = None
    north: float | None = None
    source_id: int | None = None

    def offset_from(self, target: TargetPosition, when: datetime | float) -> tuple[float, float]:
        """(east, north) arcsec of this star from the target at ``when``."""
        if self.source == "offset":
            assert self.east is not None and self.north is not None
            return float(self.east), float(self.north)
        assert self.ra is not None and self.dec is not None
        ep = decimal_year(when)
        assert ep is not None
        ra, dec = propagate(self.ra, self.dec, self.pmra, self.pmdec, ep - self.epoch)
        return sky_offset(*target.at(when), float(ra), float(dec))

    def radec(self, target: TargetPosition, when: datetime | float) -> tuple[float, float]:
        """ICRS (deg) at ``when`` (an offset star: the target's plus the offset)."""
        if self.source == "offset":
            ra0, dec0 = target.at(when)
            assert self.east is not None and self.north is not None
            ra, dec = _untan(
                ra0,
                dec0,
                math.radians(self.east / 3600.0),
                math.radians(self.north / 3600.0),
            )
            return float(ra), float(dec)
        assert self.ra is not None and self.dec is not None
        ep = decimal_year(when)
        assert ep is not None
        ra, dec = propagate(self.ra, self.dec, self.pmra, self.pmdec, ep - self.epoch)
        return float(ra), float(dec)

    def as_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def guide_star_from_spec(spec: Any) -> GuideStar | None:
    """A request's ``offset_star`` (module doc) as a :class:`GuideStar`;
    None for ``"auto"`` (to be chosen from the catalog). Raises ValueError."""
    if spec == "auto":
        return None
    if not isinstance(spec, dict):
        raise ValueError(f"offset star: expected 'auto' or an object, got {spec!r}")
    if "east" in spec or "north" in spec:
        e, n = float(spec.get("east", 0.0)), float(spec.get("north", 0.0))
        if not (math.isfinite(e) and math.isfinite(n)) or math.hypot(e, n) > 3600.0:
            raise ValueError(f"offset star: offset ({e}, {n}) arcsec out of range")
        return GuideStar("offset", east=e, north=n, g=_opt(spec.get("g")))
    if "ra" in spec and "dec" in spec:
        return GuideStar(
            "coordinates",
            float(spec["ra"]),
            float(spec["dec"]),
            float(spec.get("pmra") or 0.0),
            float(spec.get("pmdec") or 0.0),
            float(spec.get("epoch") or 2000.0),
            _opt(spec.get("g")),
        )
    raise ValueError(f"offset star: give east/north or ra/dec, got keys {sorted(spec)}")


def _opt(v: Any) -> float | None:
    return None if v is None else float(v)


def sky_offset(ra0: float, dec0: float, ra: float, dec: float) -> tuple[float, float]:
    """(east, north) arcsec of (ra, dec) from (ra0, dec0): gnomonic
    standard coordinates, so east is a true angle (cos dec included)."""
    xi, eta = _tan(ra0, dec0, ra, dec)
    return float(np.degrees(xi) * 3600.0), float(np.degrees(eta) * 3600.0)


def pixel_offset(
    east: float, north: float, plate_scale: float, pa: float, flip: bool
) -> np.ndarray:
    """Pixels (dx, dy) of a sky offset (arcsec east, north) on a camera with
    this plate scale (arcsec/px), PA (deg) and parity (FITS convention)."""
    return np.linalg.solve(fits_sky_matrix(plate_scale, pa, flip), np.array([east, north], float))


@dataclass(frozen=True)
class OffsetSite:
    """Where and in what air: latitude/longitude (deg), elevation (m),
    pressure (hPa; None: from the elevation), temperature (C), relative
    humidity (0-1), and the guide camera's effective wavelength (um)."""

    latitude: float
    longitude: float
    elevation: float = 0.0
    pressure: float | None = None
    temperature: float = 10.0
    humidity: float = 0.3
    wavelength: float = 0.65

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OffsetSite:
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"offset site: unknown keys {sorted(set(d) - known)}")
        return cls(**{k: (None if v is None else float(v)) for k, v in d.items()})

    @property
    def pressure_hpa(self) -> float:
        if self.pressure is not None:
            return self.pressure
        return 1013.25 * math.exp(-self.elevation / 8400.0)


def altaz_and_parallactic(
    ra: float, dec: float, when: datetime, site: OffsetSite
) -> tuple[float, float]:
    """(altitude, parallactic angle) in degrees, geometric; the parallactic
    angle is the PA of the direction to the zenith (east of north)."""
    import erfa

    from robofast_core import sky

    lst = sky.sky_now(sky.Site(site.latitude, site.longitude, site.elevation), when).lst_hours
    ha = math.radians((lst * 15.0 - ra + 540.0) % 360.0 - 180.0)
    phi, d = math.radians(site.latitude), math.radians(dec)
    _, el = erfa.hd2ae(ha, d, phi)
    q = erfa.hd2pa(ha, d, phi)
    return math.degrees(float(el)), math.degrees(float(q))


def refraction_shift(
    east: float, north: float, alt: float, parallactic: float, site: OffsetSite
) -> tuple[float, float]:
    """Differential refraction: how far (arcsec east, north) the atmosphere
    moves the guide star, at (east, north) arcsec from the target, relative
    to the target (at altitude ``alt``, parallactic angle ``parallactic``,
    deg). Refraction lifts each star towards the zenith by ``A tan z + B
    tan^3 z`` (ERFA ``refco``): along the vertical the offset grows by the
    derivative of that times the difference in zenith distance, across it
    it shrinks by ``A`` (the stars converge on the zenith)."""
    import erfa

    a, b = erfa.refco(site.pressure_hpa, site.temperature, site.humidity, site.wavelength)
    z = math.radians(90.0 - min(max(alt, 5.0), 90.0))
    q = math.radians(parallactic)
    up = np.array([math.sin(q), math.cos(q)])  # towards the zenith, (E, N)
    across = np.array([up[1], -up[0]])
    off = np.array([east, north], float)
    v, h = float(off @ up), float(off @ across)
    # A star v arcsec higher has zenith distance z - v; it is lifted less.
    sec2 = 1.0 / math.cos(z) ** 2
    dr_dz = a * sec2 + 3.0 * b * math.tan(z) ** 2 * sec2
    shift = -dr_dz * v * up - a * h * across
    return float(shift[0]), float(shift[1])


@dataclass
class OffsetGuide:
    """The setpoint of an offset-guided observation over time (module doc).

    ``pixels(when)`` is what to add to the fiber pixel: the guide star's
    offset from the target at ``when`` (proper motion), plus differential
    refraction (if ``site`` is given and ``refraction``), through the
    camera's sky matrix at the PA of the moment. ``pa`` is the PA at the
    start (FITS convention); with ``field_rotation`` ``"altaz"`` it then
    follows the parallactic angle (``sense`` +1 or -1: which way the field
    turns on the detector), with ``"none"`` it is constant."""

    target: TargetPosition
    star: GuideStar
    plate_scale: float
    pa: float
    flip: bool = False
    site: OffsetSite | None = None
    refraction: bool = True
    field_rotation: str = "none"
    sense: int = 1
    #: Seconds between setpoint updates in the guide loop.
    update: float = 10.0
    start: datetime | None = None
    _q0: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.field_rotation not in FIELD_ROTATION:
            raise ValueError(f"field_rotation {self.field_rotation!r}: one of {FIELD_ROTATION}")
        if self.field_rotation != "none" and self.site is None:
            raise ValueError("field rotation needs the site")
        if not self.plate_scale or self.plate_scale <= 0:
            raise ValueError("offset guiding needs the guide camera's plate scale")

    def solve(self, when: datetime) -> dict[str, Any]:
        """Everything about the setpoint at ``when`` (logged and reported)."""
        east, north = self.star.offset_from(self.target, when)
        out: dict[str, Any] = {"east": round(east, 3), "north": round(north, 3)}
        pa = self.pa
        de = dn = 0.0
        if self.site is not None:
            ra, dec = self.target.at(when)
            alt, q = altaz_and_parallactic(ra, dec, when, self.site)
            de, dn = refraction_shift(east, north, alt, q, self.site)
            out.update(alt=round(alt, 3), parallactic=round(q, 3))
            out["refraction"] = [round(de, 4), round(dn, 4)]
            if self.field_rotation == "altaz":
                if self._q0 is None:
                    self._q0 = q
                dq = (q - self._q0 + 180.0) % 360.0 - 180.0
                pa = (self.pa + self.sense * dq) % 360.0
        if not self.refraction:
            de = dn = 0.0
        px = pixel_offset(east + de, north + dn, self.plate_scale, pa, self.flip)
        out.update(pa=round(pa, 4), pixels=[float(px[0]), float(px[1])])
        return out

    def pixels(self, when: datetime) -> np.ndarray:
        return np.asarray(self.solve(when)["pixels"], dtype=float)

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "star": self.star.as_json(),
            "plate_scale": self.plate_scale,
            "pa": self.pa,
            "flip": self.flip,
            "refraction": self.refraction and self.site is not None,
            "field_rotation": self.field_rotation,
            "update": self.update,
        }
        return d


# -- choosing a guide star ---------------------------------------------------------


@dataclass
class AutoConfig:
    """``offset_star: "auto"``: which catalog star to guide on (per guide
    camera, in the guider service's ``offset_guide.auto`` config).

    A candidate is brighter than ``g_faint`` and fainter than ``g_bright``
    (which saturates the guide exposure: depends on the camera and the
    exposure; set it per guider), at least ``min_separation`` arcsec from
    the target (clear of the fiber hole and of the target's spill) and
    within the camera's field with ``edge_margin`` pixels to spare, with
    no catalog star brighter than its own G + ``isolation_dmag`` within
    ``isolation`` arcsec. Of those, the one with the smallest
    ``separation / separation_scale + (G - g_good if fainter than g_good)``
    wins: close stars keep the offset's systematic errors (plate scale, PA,
    differential refraction) small, bright ones the centroid noise."""

    g_bright: float = 7.0
    g_faint: float = 14.5
    g_good: float = 11.0
    min_separation: float = 8.0
    edge_margin: float = 40.0
    isolation: float = 6.0
    isolation_dmag: float = 3.0
    separation_scale: float = 60.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> AutoConfig:
        d = {k: v for k, v in dict(d or {}).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"offset_guide.auto: unknown keys {sorted(set(d) - known)}")
        return cls(**{k: float(v) for k, v in d.items()})


def choose_guide_star(
    stars: Any,
    target: TargetPosition,
    when: datetime,
    *,
    fiber: tuple[float, float],
    sensor: tuple[int, int],
    plate_scale: float,
    pa: float,
    flip: bool,
    config: AutoConfig | None = None,
) -> tuple[GuideStar | None, list[str]]:
    """The best guide star among catalog ``stars`` (a
    :class:`~robofast_core.catalog.Stars`, any epoch) for ``target`` (see
    :class:`AutoConfig`), and why each rejected bright one was rejected."""
    cfg = config or AutoConfig()
    ep = decimal_year(when)
    s = stars.at_epoch(when)
    ra0, dec0 = target.at(when)
    notes: list[str] = []
    best: tuple[float, int] | None = None
    sep_all = separation(ra0, dec0, s.ra, s.dec) * 3600.0
    for i in range(len(s)):
        g = float(s.g[i])
        sep = float(sep_all[i])
        label = f'G={g:.1f} at {sep:.0f}"'
        if not np.isfinite(g) or g > cfg.g_faint:
            continue
        if sep < cfg.min_separation:
            continue  # the target itself, or too close to the fiber
        if g < cfg.g_bright:
            notes.append(f"{label}: saturates (G < {cfg.g_bright:g})")
            continue
        e, n = sky_offset(ra0, dec0, float(s.ra[i]), float(s.dec[i]))
        px = np.asarray(fiber) + pixel_offset(e, n, plate_scale, pa, flip)
        m = cfg.edge_margin
        if not (m <= px[0] <= sensor[0] - 1 - m and m <= px[1] <= sensor[1] - 1 - m):
            continue  # off the camera
        d = separation(s.ra[i], s.dec[i], s.ra, s.dec) * 3600.0
        crowd = (d > 0.01) & (d < cfg.isolation) & (s.g < g + cfg.isolation_dmag)
        if crowd.any():
            notes.append(f'{label}: not isolated ({int(crowd.sum())} within {cfg.isolation:g}")')
            continue
        score = sep / cfg.separation_scale + max(0.0, g - cfg.g_good)
        if best is None or score < best[0]:
            best = (score, i)
    if best is None:
        return None, notes
    i = best[1]
    return (
        GuideStar(
            "catalog",
            float(s.ra[i]),
            float(s.dec[i]),
            float(s.pmra[i]),
            float(s.pmdec[i]),
            float(ep if ep is not None else s.epoch),
            float(s.g[i]),
            source_id=int(s.source_id[i]),
        ),
        notes,
    )


# -- checking an explicit offset against the catalog ----------------------------------


@dataclass(frozen=True)
class OffsetCheck:
    """How an explicit offset (``{"east", "north"}``) is checked against the
    catalog (:func:`check_offset_star`; docs/offset_guiding.md).

    A catalog star must lie within ``tolerance`` arcsec of target + offset
    at the date of observation (both moved along their proper motion), and
    be a sensible guide star: brighter than ``g_faint`` and fainter than
    ``g_bright`` (catalog magnitude, Gaia G by default; the same limits as
    :class:`AutoConfig`). ``search`` arcsec: how far round the expected
    position the nearest star is reported, to help fix a wrong offset.
    ``action``: what a failed check does to the row -- ``refuse`` it, or
    ``flag`` it (kept, with a warning)."""

    tolerance: float = 3.0
    g_bright: float = 7.0
    g_faint: float = 14.5
    search: float = 60.0
    action: str = "refuse"
    enabled: bool = True
    #: The catalog's config entry (docs/sky.md); None: the default catalog.
    catalog: dict[str, Any] | None = None

    ACTIONS = ("refuse", "flag")

    @classmethod
    def from_dict(cls, d: Any) -> OffsetCheck:
        """From a config entry: an object, ``false`` (off) or None/``true``
        (the defaults). Raises ValueError naming the bad key."""
        if d is None or d is True:
            return cls()
        if d is False:
            return cls(enabled=False)
        if not isinstance(d, dict):
            raise ValueError("offset_check: an object, true or false")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = {f for f in cls.__dataclass_fields__}
        if set(d) - known:
            raise ValueError(f"offset_check: unknown keys {sorted(set(d) - known)}")
        out: dict[str, Any] = {}
        for k, v in d.items():
            if k in ("tolerance", "g_bright", "g_faint", "search"):
                if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                    raise ValueError(f"offset_check.{k}: a number")
                out[k] = float(v)
            elif k == "action":
                if v not in cls.ACTIONS:
                    raise ValueError(f"offset_check.action: one of {', '.join(cls.ACTIONS)}")
                out[k] = v
            elif k == "enabled":
                if not isinstance(v, bool):
                    raise ValueError("offset_check.enabled: true or false")
                out[k] = v
            elif k == "catalog":
                if v is not None and not isinstance(v, dict):
                    raise ValueError("offset_check.catalog: a catalog entry (docs/sky.md)")
                out[k] = v
        c = cls(**out)
        if c.tolerance <= 0 or c.search < c.tolerance or c.g_bright >= c.g_faint:
            raise ValueError("offset_check: tolerance > 0, search >= tolerance, g_bright < g_faint")
        return c


@dataclass(frozen=True)
class OffsetVerdict:
    """What :func:`check_offset_star` found. ``ok``: True (a sensible star
    is there), False (none, or not a usable one), None (not checked: the
    catalog could not answer). ``g``: the matched star's catalog magnitude;
    ``miss``: arcsec from the expected position to it; ``source_id``: its
    catalog id; ``reason``: one line for a log or an import summary."""

    ok: bool | None
    reason: str
    g: float | None = None
    miss: float | None = None
    source_id: int | None = None
    #: The nearest star's own offset from the target (east, north arcsec),
    #: when one was found within ``search``: what the offset perhaps should be.
    found: tuple[float, float] | None = None


def check_offset_star(
    catalog: Any,
    target: TargetPosition,
    east: float,
    north: float,
    when: datetime | float,
    check: OffsetCheck | None = None,
) -> OffsetVerdict:
    """Is there a catalog star at ``target`` + (``east``, ``north``) arcsec
    (the guide star relative to the target, on the sky) at ``when``?

    ``catalog``: a :class:`~robofast_core.catalog.Catalog` (anything with
    its ``cone(ra, dec, radius, epoch=..., mag_limit=...)``). Both the
    target and the catalog's stars are moved to ``when``. The verdict is
    ``ok`` only for a star within ``tolerance`` whose magnitude is inside
    (``g_bright``, ``g_faint``); a catalog that cannot answer (tiles missing
    offline) gives ``ok`` None, never a refusal."""
    from robofast_core.catalog import CatalogUnavailable

    c = check or OffsetCheck()
    ra0, dec0 = target.at(when)
    ra, dec = _untan(ra0, dec0, math.radians(east / 3600.0), math.radians(north / 3600.0))
    ra, dec = float(ra), float(dec)
    try:
        stars = catalog.cone(ra, dec, c.search / 3600.0, epoch=when)
    except (CatalogUnavailable, OSError) as exc:
        return OffsetVerdict(None, f"not checked: the catalog cannot answer ({exc})")
    if not len(stars):
        return OffsetVerdict(False, f'no catalog star within {c.search:g}" of target + offset')
    d = separation(ra, dec, stars.ra, stars.dec) * 3600.0
    near = d <= c.tolerance
    if not near.any():
        i = int(np.argmin(d))
        found = sky_offset(ra0, dec0, float(stars.ra[i]), float(stars.dec[i]))
        return OffsetVerdict(
            False,
            f'no catalog star within {c.tolerance:g}" of target + offset; the nearest'
            f' (mag {float(stars.g[i]):.1f}) is {float(d[i]):.1f}" away, at'
            f' east {found[0]:+.1f}" north {found[1]:+.1f}" from the target',
            g=float(stars.g[i]),
            miss=float(d[i]),
            source_id=int(stars.source_id[i]),
            found=found,
        )
    # Of the stars within the tolerance, the brightest (the guider would
    # centroid on it).
    idx = np.flatnonzero(near)
    i = int(idx[np.argmin(np.where(np.isfinite(stars.g[idx]), stars.g[idx], np.inf))])
    g, miss = float(stars.g[i]), float(d[i])
    found = sky_offset(ra0, dec0, float(stars.ra[i]), float(stars.dec[i]))
    base = dict(g=g, miss=miss, source_id=int(stars.source_id[i]), found=found)
    if not math.isfinite(g) or g >= c.g_faint:
        return OffsetVerdict(
            False,
            f'the catalog star {miss:.1f}" from target + offset is too faint to guide on'
            f" (mag {g:.1f}, limit {c.g_faint:g})",
            **base,
        )
    if g <= c.g_bright:
        return OffsetVerdict(
            False,
            f'the catalog star {miss:.1f}" from target + offset saturates the guider'
            f" (mag {g:.1f}, limit {c.g_bright:g})",
            **base,
        )
    return OffsetVerdict(True, f'catalog star mag {g:.1f} {miss:.1f}" from target + offset', **base)


#: A clock-time -> UTC mapping, for the guide loop (virtual or real time).
UtcAt = Callable[[float], datetime]
