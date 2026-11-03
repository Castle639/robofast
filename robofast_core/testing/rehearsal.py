"""A real-time hardware rehearsal of the MINERVA RV night (docs/demo.md).

The simulated RV night (:mod:`robofast_core.testing.rv_night`) with, for
the telescopes chosen on the command line, the **real mount** (its Alpaca
Telescope, Focuser and Rotator) and the **real science camera** in place of
the fakes, **with the enclosure closed**. Everything else is simulated
exactly as in the simulated night: the weather (forced safe), the Aqawan
(the PAC simulator and the real Aqawan Dome server: the *simulated* roof
opens; the real enclosure is never contacted), the supervisor, the guiders
(simulated FAU cameras: they acquire and guide on synthetic star fields
wherever the real mount points), KiwiSpec (simulator and service). A
telescope not named on the command line stays simulated.

* **Real time.** The clock runs at real speed from now, so the real mounts
  point by the real sidereal time. The plan is made now: ``SPECTRUM``
  requests (a B star through iodine, RV standards with iodine, a template
  without) and a few short imaging requests per telescope, on stars from
  ``sites/minerva/requests.example.json`` (and the bright stars below, so
  that something is up at any sidereal time) that are within the
  telescopes' altitude limits (21..83 deg) during the rehearsal. It ends
  after ``--duration`` or at morning twilight, whichever is first, or when
  every request is done.
* **No autofocus** on a real telescope: dark frames cannot be focused (a
  log line says so). A simulated telescope still focuses its simulated
  imager.
* **Real exposures are dark** (the enclosure is closed). Nothing looks at
  their content (no FWHM, no plate solving); the frames and sidecars are
  written as usual, with the real mount's pointing in the headers.
* **Safety.** It refuses to start without every endpoint of every real
  telescope given explicitly and without ``--i-know-the-enclosure-is-closed``.
  It starts nothing that talks to a real enclosure, PAC or PDU (every
  device server it starts runs without ``--hardware`` on a verified
  simulator: :mod:`robofast_core.hwgate`). robofast reaches each real mount
  only through a :class:`~robofast_core.testing.mount_guard.MountGuard`:
  reads pass, slews outside the altitude limits are refused, robofast's
  ``Park`` becomes "stop tracking, stay where you are" and every other
  command is refused. At the end (also on Ctrl-C or an error) the guard is
  locked and each real mount gets the ``--end-action``: ``leave`` (tracking
  off, where it is; the default) or ``altaz:ALT,AZ`` (tracking off, then a
  slew to that altitude and azimuth).

``python -m robofast_core.testing.rehearsal --help``; the tests
(``tests/e2e/test_rehearsal.py``) run it on fake mounts and cameras served
over Alpaca on loopback as if they were real, with a faster clock.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from robofast_core import hwgate, sky
from robofast_core.autofocus import AutofocusSetup, FocusModel, FocusStore
from robofast_core.autofocus_plan import AutofocusTriggers, TriggerConfig
from robofast_core.dispatch import dispatch_from_config
from robofast_core.owner import DeviceOwner
from robofast_core.plan import Idle, Observation
from robofast_core.plan_record import read_record
from robofast_core.request_model import Status
from robofast_core.sequencer import Sequencer
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.focus_sim import SimFocuser, StarfieldCamera
from robofast_core.testing.mount_guard import Endpoint, MountGuard, separation
from robofast_core.testing.rv_night import (
    AUTOFOCUS,
    MINERVA,
    NAMES,
    SEQUENCER,
    SITES,
    TRUE_FOCUS,
    NightSettings,
    Outcome,
    SimulatedNight,
    Telescope,
    _recording_telescope,
    _scene,
    night_names,
    safety_violations,
)
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

#: robofast's altitude limits for MINERVA (sites/minerva/aqawan1.json).
MIN_ALT, MAX_ALT = 21.0, 83.0
#: Targets are chosen this far inside the limits.
ALT_MARGIN = 2.0
#: In daylight (``ignore_twilight``), no target closer to the Sun than this (deg).
MIN_SUN_SEP = 45.0

#: Bright stars added to the example requests' targets so that, at any
#: sidereal time, a B star and a few RV standards are up. Rounded J2000
#: positions (good to about an arcminute): for pointing only, since the
#: spectra are simulated. (name, ra deg, dec deg)
EXTRA_BSTARS = [
    ("HD 358", 2.0965, 29.0904),  # alpha And
    ("HD 23630", 56.8712, 24.1051),  # eta Tau
    ("HD 32630", 76.6288, 41.2345),  # eta Aur
    ("HD 58715", 111.7877, 8.2893),  # beta CMi
    ("HD 160762", 264.8662, 46.0063),  # iota Her
    ("HD 196867", 309.9096, 15.9119),  # alpha Del
    ("HD 214923", 340.3654, 10.8314),  # zeta Peg
]
EXTRA_RV = [
    ("HD 3651", 9.8408, 21.2506),
    ("HD 26965", 63.8180, -7.6528),
    ("HD 34411", 79.7854, 40.0992),
    ("HD 50692", 103.8279, 25.3756),
    ("HD 75732", 133.1492, 28.3308),
    ("HD 84737", 147.1475, 46.0211),
]


@dataclass
class RealTelescope:
    """One real telescope's endpoints (``rotator`` None: it has none)."""

    telescope: Endpoint
    camera: Endpoint
    focuser: Endpoint
    rotator: Endpoint | None


@dataclass
class EndAction:
    """What each real mount does at the end: tracking off and ``leave`` it,
    or slew to ``alt``/``az`` (deg)."""

    kind: str = "leave"
    alt: float | None = None
    az: float | None = None

    @classmethod
    def parse(cls, text: str) -> EndAction:
        if text == "leave":
            return cls()
        m = re.fullmatch(r"altaz:([-+0-9.]+),([-+0-9.]+)", text)
        if not m:
            raise ValueError(f"--end-action {text!r}: expected 'leave' or 'altaz:ALT,AZ'")
        alt, az = float(m[1]), float(m[2])
        if not MIN_ALT <= alt <= MAX_ALT:
            raise ValueError(f"--end-action altitude {alt}: outside {MIN_ALT}..{MAX_ALT}")
        if not 0.0 <= az < 360.0:
            raise ValueError(f"--end-action azimuth {az}: outside 0..360")
        return cls("altaz", alt, az)

    def __str__(self) -> str:
        return "leave" if self.kind == "leave" else f"altaz:{self.alt:g},{self.az:g}"


@dataclass
class RehearsalSettings:
    """The rehearsal (module doc). ``real`` names the real telescopes."""

    real: dict[str, RealTelescope]
    end_action: EndAction = field(default_factory=EndAction)
    #: Real seconds of the scheduler's clock; the rehearsal ends after this
    #: or at morning twilight, whichever first.
    duration: float = 2 * 3600.0
    #: Spectra: exposure time (s; the KiwiSpec simulator's, real seconds).
    exptime: float = 60.0
    #: Imaging requests per telescope (spread over the rehearsal), each
    #: ``image_count`` frames of ``image_exptime`` s.
    images: int = 2
    image_exptime: float = 10.0
    image_count: int = 2
    #: Number of SPECTRUM requests (None: enough for the duration).
    spectra: int | None = None
    #: Observe in daylight (the scheduler's Sun limit off; targets kept
    #: MIN_SUN_SEP from the Sun). Only sensible with the enclosure closed.
    ignore_twilight: bool = False
    console_port: int | None = 0
    #: Seconds the scheduler sleeps when nothing is observable.
    idle_poll: float = 30.0
    #: Test only: the clock's start and speed (default: now, real time).
    start: datetime | None = None
    scale: float = 1.0
    #: The end action's slew bound (s).
    end_slew_timeout: float = 240.0

    def __post_init__(self) -> None:
        unknown = set(self.real) - set(NAMES)
        if unknown:
            raise ValueError(f"unknown telescopes {sorted(unknown)} (MINERVA's are {NAMES})")
        if not self.real:
            raise ValueError("no real telescope: use robofast_core.testing.rv_night instead")
        seen: dict[tuple[str, int, int, str], str] = {}
        for name, r in self.real.items():
            for kind, ep in (("telescope", r.telescope), ("camera", r.camera)):
                key = (ep.host, ep.port, ep.number, kind)
                if key in seen:
                    raise ValueError(f"{name} {kind} {ep} is also {seen[key]}'s")
                seen[key] = name


class RehearsalRefused(Exception):
    """The rehearsal will not start (the message says why)."""


@dataclass
class RehearsalOutcome(Outcome):
    """:class:`~robofast_core.testing.rv_night.Outcome` plus the guard's
    records and the end action."""

    rehearsal: RehearsalSettings | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    why_end: str = ""
    targets: list[dict[str, Any]] = field(default_factory=list)
    slews: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    #: name -> what the end action did (and the mount's state after it).
    end: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: name -> what the preflight read.
    preflight: dict[str, dict[str, Any]] = field(default_factory=dict)


# -- the plan ------------------------------------------------------------------------------


def catalog() -> list[dict[str, Any]]:
    """The candidate targets: the example requests' SPECTRUM targets (B stars
    from the ``BSTAR-`` groups) and :data:`EXTRA_BSTARS`, :data:`EXTRA_RV`."""
    raw = json.loads((SITES / "requests.example.json").read_text())
    out: dict[str, dict[str, Any]] = {}
    for g in raw["groups"]:
        bstar = g["id"].startswith("BSTAR-")
        for r in g["requests"]:
            for c in r["configurations"]:
                t = c["target"]
                if c["type"] != "SPECTRUM" or t["name"] in out:
                    continue
                out[t["name"]] = {
                    "name": t["name"],
                    "ra": float(t["ra"]),
                    "dec": float(t["dec"]),
                    "bstar": bstar,
                    **{
                        k: t[k]
                        for k in ("proper_motion_ra", "proper_motion_dec", "epoch", "parallax")
                        if k in t
                    },
                }
    for rows, bstar in ((EXTRA_BSTARS, True), (EXTRA_RV, False)):
        for name, ra, dec in rows:
            out.setdefault(name, {"name": name, "ra": ra, "dec": dec, "bstar": bstar})
    return list(out.values())


def observable(
    targets: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    *,
    ignore_twilight: bool = False,
    step: float = 600.0,
) -> list[dict[str, Any]]:
    """``targets`` with ``"up"`` (seconds within the limits, with margin, from
    ``start`` to ``end``) and ``"up_now"`` and ``"alt_now"``; those never up
    are left out. Best first: up now, then longest up."""
    from astropy import units as u
    from astropy.coordinates import AltAz, SkyCoord, get_body
    from astropy.time import Time

    n = max(2, math.ceil((end - start).total_seconds() / step) + 1)
    times = [start + (end - start) * i / (n - 1) for i in range(n)]
    ra = [t["ra"] / 15.0 for t in targets]
    dec = [t["dec"] for t in targets]
    alt = sky.altitudes(MINERVA, ra, dec, [times] * len(targets))
    ok = (alt >= MIN_ALT + ALT_MARGIN) & (alt <= MAX_ALT - ALT_MARGIN)
    if ignore_twilight:
        tt = Time(times)
        sun = get_body("sun", tt, MINERVA.location).transform_to(
            AltAz(obstime=tt, location=MINERVA.location)
        )
        coords = SkyCoord(
            ra=np.asarray(ra)[:, None] * 15 * u.deg, dec=np.asarray(dec)[:, None] * u.deg
        )
        frame = AltAz(obstime=tt, location=MINERVA.location)
        sep = coords.transform_to(frame).separation(sun).deg
        ok &= sep >= MIN_SUN_SEP
    dt = (end - start).total_seconds() / (n - 1)
    out = []
    for t, row, a in zip(targets, ok, alt, strict=True):
        if row.any():
            out.append(
                {**t, "up": float(row.sum() * dt), "up_now": bool(row[0]), "alt_now": float(a[0])}
            )
    out.sort(key=lambda t: (not t["up_now"], -t["up"]))
    return out


def morning_twilight(start: datetime, limit: float = -12.0) -> datetime:
    """The first time after ``start`` with the Sun above ``limit`` and rising
    (to a minute)."""
    t = start
    for _ in range(24 * 60 // 10):
        s = sky.sky_now(MINERVA, t)
        if s.sun_alt > limit and s.sun_rising:
            break
        t += timedelta(minutes=10)
    while t > start:
        s = sky.sky_now(MINERVA, t - timedelta(minutes=1))
        if not (s.sun_alt > limit and s.sun_rising):
            break
        t -= timedelta(minutes=1)
    return t


def make_requests(
    s: RehearsalSettings, start: datetime, end: datetime, targets: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The rehearsal's request groups (module doc) from ``observable``'s targets."""
    if not targets:
        raise RehearsalRefused(
            f"nothing in the catalog is within {MIN_ALT + ALT_MARGIN}..{MAX_ALT - ALT_MARGIN} deg"
            f" between {start:%H:%M} and {end:%H:%M} UTC"
        )
    bstars = [t for t in targets if t["bstar"]]
    rv = [t for t in targets if not t["bstar"]] or targets
    real_seconds = (end - start).total_seconds() / s.scale
    n = s.spectra
    if n is None:
        n = max(3, math.ceil(real_seconds / (s.exptime + 60.0)) + 2)
    window = {"start": start.isoformat(), "end": end.isoformat()}

    def target(t: dict[str, Any]) -> dict[str, Any]:
        keep = ("proper_motion_ra", "proper_motion_dec", "epoch", "parallax")
        return {
            "type": "ICRS",
            "name": t["name"],
            "ra": t["ra"],
            "dec": t["dec"],
            **{k: t[k] for k in keep if k in t},
        }

    groups = []
    j = 0  # the next RV standard
    for k in range(n):
        if k == 0 and bstars:
            t, role, iodine = bstars[0], "B star through iodine", True
        elif k == 2:
            t, role, iodine = rv[0], "template (iodine out)", False
        else:
            t, role, iodine = rv[j % len(rv)], "RV standard", True
            j += 1
        gid = f"REH{k + 1:02d}"
        ic = {
            "exposure_time": s.exptime,
            "exposure_count": 1,
            "extra_params": {"iodine": iodine},
        }
        groups.append(
            {
                "id": gid,
                "name": f"{role}: {t['name']}",
                "proposal": "rehearsal",
                "ipp_value": max(1.0, 10.0 - 0.1 * k),
                "requests": [
                    {
                        "id": f"{gid}-r",
                        "extra_params": {"telescopes": list(NAMES)},
                        "windows": [window],
                        "configurations": [
                            {
                                "id": f"{gid}-spec",
                                "type": "SPECTRUM",
                                "instrument_type": "spectrograph",
                                "target": target(t),
                                "instrument_configs": [ic],
                            }
                        ],
                    }
                ],
            }
        )
    # Imaging: spread over the rehearsal, per telescope, on the highest
    # target up in the middle of each window.
    span = end - start
    for name in NAMES:
        for i in range(s.images):
            w0, w1 = start + span * i / s.images, start + span * (i + 1) / s.images
            mid = w0 + (w1 - w0) / 2
            best = observable(
                targets, mid, mid + timedelta(minutes=1), ignore_twilight=s.ignore_twilight
            )
            if not best:
                continue
            t = max(best, key=lambda x: x["alt_now"])
            gid = f"IMG-{name}-{i + 1}"
            groups.append(
                {
                    "id": gid,
                    "name": f"imaging on {name}: {t['name']}",
                    "proposal": "rehearsal",
                    "ipp_value": 20,
                    "requests": [
                        {
                            "id": f"{gid}-r",
                            "location": {"telescope": name},
                            "windows": [{"start": w0.isoformat(), "end": w1.isoformat()}],
                            "configurations": [
                                {
                                    "id": f"{gid}-img",
                                    "type": "EXPOSE",
                                    "instrument_type": "imager",
                                    "target": target(t),
                                    "instrument_configs": [
                                        {
                                            "exposure_time": s.image_exptime,
                                            "exposure_count": s.image_count,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
    return groups


class RehearsalEnd:
    """A plan wrapper: the night is over (``None``) at ``end`` or once every
    request is done. A telescope reserved for another's spectrum still
    gets it (the leader would wait for it otherwise)."""

    def __init__(self, inner: Any, dispatch: Any, end: datetime, clock: Any) -> None:
        self.inner, self.dispatch, self.end, self.clock = inner, dispatch, end, clock
        self.why: str = ""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def _over(self) -> str | None:
        if self.clock() >= self.end:
            return f"the rehearsal's end ({self.end:%H:%M:%S} UTC)"
        configs = [c for g in self.dispatch.groups.values() for c in g.configurations()]
        if configs and all(Status(c.status).terminal for c in configs):
            return "every request done"
        return None

    def _member(self, telescope: str, obs: Any) -> bool:
        return (
            isinstance(obs, Observation)
            and obs.spectrum is not None
            and obs.spectrum.get("leader") not in (None, telescope)
        )

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        why = self._over()
        if why is None:
            return await self.inner.next_observation(telescope)
        if self._member(telescope, await self.inner.peek(telescope)):
            return await self.inner.next_observation(telescope)
        if not self.why:
            self.why = why
            log.warning("rehearsal over: %s", why)
        return None

    async def peek(self, telescope: str) -> Observation | Idle | None:
        if self._over() is None:
            return await self.inner.peek(telescope)
        nxt = await self.inner.peek(telescope)
        return nxt if self._member(telescope, nxt) else None

    def report(self, *args: Any, **kw: Any) -> None:
        self.inner.report(*args, **kw)


# -- the rehearsal --------------------------------------------------------------------------


def _alpaca_get(client: httpx.Client, url: str) -> Any:
    r = client.get(url, params={"ClientID": 1, "ClientTransactionID": 0})
    r.raise_for_status()
    d = r.json()
    if d.get("ErrorNumber"):
        raise RuntimeError(f"{d.get('ErrorNumber')}: {d.get('ErrorMessage')}")
    return d.get("Value")


def _alpaca_put(client: httpx.Client, url: str, **values: Any) -> None:
    form = {"ClientID": "1", "ClientTransactionID": "0", **{k: str(v) for k, v in values.items()}}
    r = client.put(url, data=form)
    r.raise_for_status()
    d = r.json()
    if d.get("ErrorNumber"):
        raise RuntimeError(f"{d.get('ErrorNumber')}: {d.get('ErrorMessage')}")


def preflight(s: RehearsalSettings, timeout: float = 10.0) -> dict[str, dict[str, Any]]:
    """Read every real endpoint once; :class:`RehearsalRefused` with every
    problem found. Nothing moves: the only command is ``Connected=True`` to
    a camera not yet connected (as robofast's device owner does anyway). A
    focuser or rotator that does not answer is only noted (they are read for
    the FITS headers)."""
    bad: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    with httpx.Client(trust_env=False, timeout=timeout) as c:
        for name, r in s.real.items():
            info: dict[str, Any] = {}
            seen[name] = info
            for kind, ep in (
                ("telescope", r.telescope),
                ("focuser", r.focuser),
                ("rotator", r.rotator),
                ("camera", r.camera),
            ):
                if ep is None:
                    continue
                url = ep.url(kind)
                sim = _loopback(ep.host) and hwgate.is_simulator(ep.host, ep.port)
                info[f"{kind}_endpoint"] = str(ep)
                info[f"{kind}_simulator"] = sim
                what = "a verified robofast simulator" if sim else "NOT a verified simulator: REAL"
                log.warning("%s %s at %s: %s", name, kind, ep, what)
                try:
                    if kind == "telescope":
                        if not _alpaca_get(c, url + "/connected"):
                            bad.append(f"{name} telescope at {ep}: not connected")
                        if _alpaca_get(c, url + "/atpark"):
                            bad.append(
                                f"{name} telescope at {ep} is parked: unpark it yourself first"
                                " (the rehearsal never sends a real mount Park or Unpark)"
                            )
                        for m in ("rightascension", "declination", "altitude", "azimuth"):
                            info[m] = _alpaca_get(c, f"{url}/{m}")
                        info["tracking"] = _alpaca_get(c, url + "/tracking")
                        if s.end_action.kind == "altaz" and not _alpaca_get(
                            c, url + "/canslewaltazasync"
                        ):
                            bad.append(f"{name} telescope at {ep} cannot SlewToAltAzAsync")
                    elif kind == "camera":
                        if not _alpaca_get(c, url + "/connected"):
                            log.warning("%s camera at %s: connecting it", name, ep)
                            _alpaca_put(c, url + "/connected", Connected=True)
                        state = _alpaca_get(c, url + "/camerastate")
                        info["camera_state"] = state
                        if state != 0:
                            bad.append(f"{name} camera at {ep}: CameraState {state}, not idle")
                        for m in ("cameraxsize", "cameraysize"):
                            info[m] = _alpaca_get(c, f"{url}/{m}")
                    else:
                        info[f"{kind}_position"] = _alpaca_get(c, url + "/position")
                except Exception as exc:
                    if kind in ("focuser", "rotator"):
                        info[f"{kind}_error"] = repr(exc)
                        log.warning("%s %s at %s: %r (headers will lack it)", name, kind, ep, exc)
                    else:
                        bad.append(f"{name} {kind} at {ep}: {exc!r}")
    if bad:
        raise RehearsalRefused("; ".join(bad))
    return seen


def _loopback(host: str) -> bool:
    try:
        return all(a.is_loopback for a in hwgate.resolve(host, what="endpoint"))
    except hwgate.HardwareNotAllowed:
        return False


class Rehearsal(SimulatedNight):
    """The rehearsal's services and night (module doc)."""

    def __init__(self, out: Path, settings: RehearsalSettings) -> None:
        self.r = settings
        start = settings.start or datetime.now(UTC)
        base = NightSettings(
            scale=settings.scale,
            start=start,
            rv_exptime=settings.exptime,
            console_port=settings.console_port,
        )
        super().__init__(out, base)
        self.outcome = RehearsalOutcome(
            self.out, base, reopen_hold=base.reopen_hold, rehearsal=settings
        )
        self.set_clock(start, settings.scale)
        self.guard: MountGuard | None = None
        self._ended = False

    @property
    def sim_names(self) -> list[str]:
        return [n for n in NAMES if n not in self.r.real]

    # -- the services ----------------------------------------------------------------

    async def start(self) -> None:
        self.outcome.preflight = await asyncio.to_thread(preflight, self.r)
        await super().start()

    def _start_telescopes(self) -> None:
        devices: dict[str, Any] = {}
        for i, name in enumerate(NAMES):
            if name in self.r.real:
                continue
            mount = _recording_telescope(name, self.outcome.mount_events)
            foc = SimFocuser(position=round(TRUE_FOCUS + 250 * (1 if i == 0 else -1)))
            cam = StarfieldCamera(foc, _scene(31 + i))
            cam.clock = self.clock
            self.telescopes[name] = Telescope(name, mount, cam, foc)
            devices[f"telescope/{i}"] = mount
            devices[f"camera/{i}"] = cam
        if devices:
            self.alpaca = FakeAlpacaServer(devices, hang_limit=10).start()
            self._stack.callback(self.alpaca.stop)
        self.guard = MountGuard(
            {
                name: {"telescope": r.telescope, "focuser": r.focuser, "rotator": r.rotator}
                for name, r in self.r.real.items()
            },
            limits=(MIN_ALT, MAX_ALT),
            site=(MINERVA.latitude, MINERVA.longitude),
            clock=self.clock,
            events=self.outcome.mount_events,
        ).start()
        self._stack.callback(self.guard.stop)
        for name in self.r.real:
            # No simulated devices: the guider is the only simulated piece.
            self.telescopes[name] = Telescope(name, None, None, None)  # type: ignore[arg-type]

    def _enclosure(self) -> dict[str, Any]:
        cfg = json.loads((SITES / "aqawan1.json").read_text())
        cfg.pop("_note", None)
        assert self.guard is not None
        cfg["devices"] = {
            "dome": self.aqawan.address,
            "safetymonitor": self.bridge_address,
            "observingconditions": self.bridge_address,
        }
        limits = {"min_alt": MIN_ALT, "max_alt": MAX_ALT}
        tels: dict[str, Any] = {}
        for i, name in enumerate(NAMES):
            if name in self.r.real:
                r = self.r.real[name]
                g = self.guard.spec(name)
                tels[name] = {
                    "telescope": g,
                    "camera": str(r.camera),
                    "focuser": g,
                    "limits": limits,
                }
                if r.rotator is not None:
                    tels[name]["rotator"] = g
            else:
                a = self.alpaca.address
                tels[name] = {"telescope": f"{a}/{i}", "camera": f"{a}/{i}", "limits": limits}
        cfg["telescopes"] = tels
        cfg["safety"].update(
            poll_interval=0.2,
            read_timeout=2.0,
            stale_after=5.0,
            reopen_hold=self.s.reopen_hold,
            lease_ttl=10.0,
            shutter_timeout=5.0,
            dome_timeout=4.0,
            park_wait=30.0,
            heartbeat_interval=1.0,
            heartbeat_timeout=10.0,
        )
        cfg["mode"] = "robotic"
        cfg["mode_file"] = str(Path(self.out).resolve() / "mode.json")
        return cfg

    def _sequencer_config(self, plan: dict[str, Any]) -> dict[str, Any]:
        cfg = super()._sequencer_config(plan)
        # Real mounts and cameras: real slews, readouts and downloads.
        cfg["command_timeout"] = 30.0
        cfg["lease_client"] = {"timeout": 3.0, "poll": 0.5, "retry": 0.5}
        cfg["sequencer"].update(
            poll_interval=0.5,
            slew_timeout=300.0,
            readout_margin=120.0,
            lease_renew_interval=2.0,
            cancel_ack_timeout=30.0,
            park_timeout=60.0,
            error_backoff=30.0,
            idle_release_after=600.0,
            plan_timeout=30.0,
            spectrum_ready_timeout=300.0,
            spectrum_check_interval=2.0,
            spectrum_poll=1.0,
            spectrum_margin=120.0,
            locate_timeout=30.0,
            spectrograph_abort_wait=10.0,
        )
        # Dark frames: nothing measures them (no FWHM, no plate solving).
        cfg["images"].update(
            imagebytes=True,
            measure_fwhm=False,
            solve=None,
            download_timeout_base=60.0,
            download_min_rate=2e5,
            telemetry_timeout=30.0,
        )
        return cfg

    def _build(self, tag: str, plan_entry: dict[str, Any], plan: Any = None) -> Sequencer:
        from robofast_core.sequencer_service import build

        cfg = self._sequencer_config(plan_entry)
        (self.out / f"sequencer-{tag}.json").write_text(json.dumps(cfg, indent=1, default=str))
        tl = Timeline(maxlen=200000)
        seq, remote = build(cfg, tl, plan=plan, base=self.out)
        seq._remote = remote  # type: ignore[attr-defined]
        for name, unit in seq.units.items():
            if name in self.r.real:
                continue  # its real camera, from the config; no autofocus
            t = self.telescopes[name]
            unit.camera = DeviceOwner(f"{name}.camera", lambda c=t.camera: c, tl, default_timeout=5)
            unit.focuser = DeviceOwner(
                f"{name}.focuser", lambda f=t.focuser: f, tl, default_timeout=3
            )
            unit.autofocus = AutofocusSetup(dict(AUTOFOCUS), store=FocusStore(), model=FocusModel())
        self.outcome.runs[tag] = seq
        self.outcome.timelines[tag] = tl
        return seq

    # -- the night -------------------------------------------------------------------

    def _plan(self) -> tuple[dict[str, Any], datetime]:
        r = self.r
        start = self.clock()
        end = start + timedelta(seconds=r.duration * r.scale)
        if not r.ignore_twilight:
            dawn = morning_twilight(start)
            if dawn < end:
                end = dawn
                self._note(f"the rehearsal ends at morning twilight, {dawn:%H:%M} UTC")
            sun = sky.sky_now(MINERVA, start).sun_alt
            if sun > -12.0:
                self._note(
                    f"the Sun is at {sun:.1f} deg: nothing is observed until it is below -12"
                    " (--ignore-twilight observes now; the enclosure is closed)"
                )
        targets = observable(catalog(), start, end, ignore_twilight=r.ignore_twilight)
        self.outcome.targets = targets
        entry = json.loads((SITES / "dispatch.example.json").read_text())
        entry.pop("_note", None)
        entry.update(
            requests=make_requests(r, start, end, targets),
            state=str(self.out / "dispatch-state.json"),
            too_dropbox=str(self.out / "too-dropbox"),
            record={"dir": str(self.out / "plans"), "timezone": "America/Phoenix"},
            idle_poll=r.idle_poll,
            too_poll=5.0,
            reserve_timeout=120.0 * r.scale,
        )
        if r.ignore_twilight:
            entry["max_sun_alt"] = 90.0
        for lim in entry["telescopes"].values():
            lim.pop("filters", None)  # no filter wheel in the rehearsal
            lim.update(min_alt=MIN_ALT + ALT_MARGIN / 2, max_alt=MAX_ALT - ALT_MARGIN / 2)
        return entry, end

    async def run(self) -> RehearsalOutcome:  # type: ignore[override]
        o = self.outcome
        assert isinstance(o, RehearsalOutcome)
        entry, end = self._plan()
        o.start_utc, o.end_utc = self.clock(), end
        n_spec = sum(1 for g in entry["requests"] if g["id"].startswith("REH"))
        self._note(
            f"rehearsal from {o.start_utc:%Y-%m-%d %H:%M:%S} to {end:%H:%M:%S} UTC:"
            f" {n_spec} spectra, {len(entry['requests']) - n_spec} imaging requests on"
            f" {', '.join(t['name'] for t in o.targets[:8])}"
        )
        for name in self.r.real:
            self._note(
                f"{name}: autofocus skipped (real camera, enclosure closed: dark frames cannot be"
                " focused)"
            )
        dispatch = dispatch_from_config(
            {k: v for k, v in entry.items() if k != "type"},
            list(NAMES),
            base=self.out,
            clock=self.clock,
        )
        plan: Any = dispatch
        if self.sim_names:
            plan = AutofocusTriggers(
                plan,
                TriggerConfig(start_of_night=True, temperature_change=None, fwhm_degradation=None),
                telescopes=self.sim_names,
            )
        plan = ender = RehearsalEnd(plan, dispatch, end, self.clock)
        seq = self._build("night", entry, plan=plan)
        if isinstance(plan.inner, AutofocusTriggers):
            plan.inner.bind(seq.registry)
        if self.s.console_port is not None:
            await self._start_console()
        self.dispatch = dispatch
        watch = asyncio.create_task(self._watch(seq), name="rehearsal-watch")
        # The night ends by itself (RehearsalEnd); this bound is a safety net.
        bound = (end - o.start_utc).total_seconds() / self.r.scale + max(900.0, 3 * self.r.exptime)
        try:
            await self._run(seq, bound)
        finally:
            watch.cancel()
            with contextlib.suppress(BaseException):
                await watch
        o.why_end = ender.why or "the sequencer stopped"
        o.statuses = {
            c.id: str(c.status) for g in dispatch.groups.values() for c in g.configurations()
        }
        with contextlib.suppress(TimeoutError):
            await self._until(lambda: self.aqawan_sim.closed(), 60, "the simulated roof closed")
        o.t_end = time.monotonic()
        o.dome_log = list(self.aqawan_sim.log)
        o.spectrograph_files = {
            k: {kk: vv for kk, vv in v.items() if kk != "data"}
            for k, v in self.kiwi_sim.files.items()
        }
        for f in sorted((self.out / "plans").glob("*.jsonl")):
            o.plan_record += read_record(f)
        return o

    async def _watch(self, seq: Sequencer) -> None:
        """Print the night's progress (spectra, slews, frames) as it goes."""
        tl = seq.timeline
        seen = 0
        nslews = 0
        while True:
            await asyncio.sleep(2.0)
            for e in tl.entries()[seen:]:
                d = e.detail
                if e.kind != "event":
                    continue
                if e.what == "spectrum_done":
                    self._note(
                        f"spectrum {d['file']} ({','.join(d['telescopes'])}, {d['ended_by']})"
                    )
                elif e.what == "frame_written":
                    self._note(f"{d['telescope']}: frame {d['file']}")
                elif e.what in (
                    "telescope_error",
                    "plan_exhausted",
                    "observe_start",
                    "lease_released",
                ):
                    self._note(f"{e.what} {d}")
            seen = len(tl.entries())
            if self.guard is not None:
                for rec in self.guard.slews[nslews:]:
                    if "seconds" not in rec and "error" not in rec:
                        break
                    nslews += 1
                    self._note(
                        f"{rec['telescope']}: slew to RA {rec['ra']:.4f} h Dec {rec['dec']:+.3f}"
                        f" (alt {rec['alt']:.1f}) in {rec.get('seconds', '?')} s,"
                        f" pointing error {rec.get('error_arcsec', rec.get('error'))} arcsec"
                    )

    # -- the end ---------------------------------------------------------------------

    def end_action(self) -> None:
        """Lock the guard; every real mount: AbortSlew, tracking off, then the
        end action (module doc). Runs once, whatever happened before."""
        if self._ended or not self.outcome.preflight:  # refused: nothing touched
            return
        self._ended = True
        o = self.outcome
        assert isinstance(o, RehearsalOutcome)
        if self.guard is not None:
            self.guard.lock()
            o.slews = [dict(x) for x in self.guard.slews]
            o.refused = [dict(x) for x in self.guard.refused]
        act = self.r.end_action
        with httpx.Client(trust_env=False, timeout=30.0) as c:
            for name, r in self.r.real.items():
                url = r.telescope.url("telescope")
                rec: dict[str, Any] = {"action": str(act)}
                o.end[name] = rec
                try:
                    with contextlib.suppress(Exception):
                        _alpaca_put(c, url + "/abortslew")
                    _alpaca_put(c, url + "/tracking", Tracking=False)
                    if act.kind == "altaz":
                        _alpaca_put(c, url + "/slewtoaltazasync", Azimuth=act.az, Altitude=act.alt)
                        deadline = time.monotonic() + self.r.end_slew_timeout
                        while _alpaca_get(c, url + "/slewing"):
                            if time.monotonic() > deadline:
                                raise TimeoutError(
                                    f"still slewing after {self.r.end_slew_timeout} s"
                                )
                            time.sleep(0.5)
                        with contextlib.suppress(Exception):  # some mounts track after a slew
                            _alpaca_put(c, url + "/tracking", Tracking=False)
                    for m in ("tracking", "altitude", "azimuth", "atpark"):
                        rec[m] = _alpaca_get(c, url + "/" + m)
                except Exception as exc:
                    rec["error"] = repr(exc)
                    o.errors.append(f"{name}: end action {act}: {exc!r}")
                self._note(f"{name}: end action {act}: {rec}")

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self.end_action)
        finally:
            await super().close()


# -- checks and the report ------------------------------------------------------------------


def check(o: RehearsalOutcome) -> list[str]:
    """What went wrong in the rehearsal, as text (empty: all fine)."""
    bad: list[str] = list(o.errors)
    s = o.rehearsal
    assert s is not None
    tl = o.timelines.get("night")
    if tl is None:
        return [*bad, "no night run"]

    def ev(what: str) -> list[Any]:
        return tl.events(what, SEQUENCER)

    done = ev("spectrum_done")
    if not done:
        bad.append("no spectrum")
    for name in s.real:
        if not any(name in e.detail["telescopes"] for e in done):
            bad.append(f"{name} fed no spectrum")
        if any(e.detail.get("telescope") == name for e in tl.events("autofocus_done")):
            bad.append(f"{name}: autofocus ran on a real camera")
    if not ev("plan_exhausted"):
        bad.append("the night did not end by itself")
    if any(u.gave_up for u in o.runs["night"].units.values()):
        bad.append("a telescope gave up")
    for name, rec in o.end.items():
        if rec.get("error"):
            continue  # in o.errors
        if rec.get("tracking"):
            bad.append(f"{name}: still tracking after the end action")
        if s.end_action.kind == "altaz":
            alt, az = rec.get("altitude"), rec.get("azimuth")
            daz = abs(((az or 0) - s.end_action.az + 180) % 360 - 180)
            if alt is None or abs(alt - s.end_action.alt) > 2 or daz > 2:
                bad.append(f"{name}: at alt {alt} az {az} after the end action {s.end_action}")
    # The guard: nothing refused, every slew within the limits and ended.
    for r in o.refused:
        bad.append(f"{r['telescope']} {r['device']}: {r['member']} refused ({r['why']})")
    for r in o.slews:
        if not MIN_ALT <= r["alt"] <= MAX_ALT:
            bad.append(f"{r['telescope']}: slew to altitude {r['alt']:.1f}")
        if "seconds" not in r:
            bad.append(f"{r['telescope']}: a slew never reported its end ({r['utc']})")
    # Safety: never a spectrum unguided, every abort before a park, every
    # park before the (simulated) roof closes. The guard parks at once
    # (tracking off), so "still parking" cannot happen.
    bad += [v for v in safety_violations(o) if "still parking" not in v]
    bad += output_problems(o)
    return bad


def frames(o: RehearsalOutcome) -> list[dict[str, Any]]:
    """Every FITS frame of the night with its header's pointing and the
    timeline's download (seconds, bytes)."""
    from astropy.io import fits

    tl = o.timelines["night"]
    downloads: dict[str, list[Any]] = {}
    for e in tl.events("image_downloaded"):
        downloads.setdefault(e.detail["telescope"], []).append(e.detail)
    out = []
    counts: dict[str, int] = {}
    for e in tl.events("frame_written"):
        d = e.detail
        name = d["telescope"]
        k = counts.get(name, 0)
        counts[name] = k + 1
        dl = (downloads.get(name) or [])[k] if k < len(downloads.get(name) or []) else {}
        path = o.out / "data" / d["file"]
        rec: dict[str, Any] = {
            "telescope": name,
            "file": d["file"],
            "path": path,
            **{
                "download_s": dl.get("seconds"),
                "bytes": dl.get("bytes"),
                "transfer": dl.get("transfer"),
            },
        }
        try:
            h = fits.getheader(path, ext=1 if str(path).endswith(".fz") else 0)
            for kw in (
                "IMAGETYP",
                "EXPTIME",
                "DATE-OBS",
                "RA",
                "DEC",
                "OBJRA",
                "OBJDEC",
                "ALT",
                "AZ",
            ):
                rec[kw] = h.get(kw)
        except Exception as exc:
            rec["error"] = repr(exc)
        out.append(rec)
    return out


def output_problems(o: RehearsalOutcome) -> list[str]:
    """Sidecars for every spectrum; every real camera's frames on disk with
    the mount's pointing; one night name."""
    bad = []
    s = o.rehearsal
    assert s is not None
    data = o.out / "data"
    science = [
        json.loads(p.read_text()) for p in sorted(data.glob("minerva/kiwispec/n*/*.fits.json"))
    ]
    science = [r for r in science if r.get("kind") == "science"]
    n_done = len(o.timelines["night"].events("spectrum_done", SEQUENCER))
    if len(science) != n_done:
        bad.append(f"{len(science)} science sidecars for {n_done} spectra")
    for r in science:
        b = r.get("barycentric") or {}
        if not b.get("bjd_tdb") or abs(b.get("barycentric_correction", 1e9)) > 31000:
            bad.append(f"{r['file']}: no barycentric correction")
        if not r.get("guide"):
            bad.append(f"{r['file']}: no guide statistics")
    fr = frames(o)
    taken = {n for n in s.real if any(f["telescope"] == n for f in fr)}
    imaging = [c for c in o.statuses if c.startswith("IMG-") and o.statuses[c] == "COMPLETED"]
    for name in s.real:
        if any(c.startswith(f"IMG-{name}-") for c in imaging) and name not in taken:
            bad.append(f"{name}: imaging completed but no frame on disk")
    for f in fr:
        if f["telescope"] not in s.real or f.get("IMAGETYP") != "LIGHT":
            continue
        if f.get("error"):
            bad.append(f"{f['file']}: {f['error']}")
            continue
        if f.get("RA") is None or f.get("DEC") is None:
            bad.append(f"{f['file']}: no mount pointing in the header")
        elif f.get("OBJRA") is not None:
            sep = separation(f["RA"] / 15, f["DEC"], f["OBJRA"] / 15, f["OBJDEC"]) / 3600
            if sep > 1.0:
                bad.append(f"{f['file']}: the mount was {sep:.2f} deg from the target")
    if not list(data.glob("minerva/t*/n*/*.frames.jsonl")) and fr:
        bad.append("no frame index")
    names = night_names(o)
    if len(names) > 1:
        bad.append(f"night names {names}: expected one")
    return bad


def summary(o: RehearsalOutcome) -> str:
    s = o.rehearsal
    assert s is not None
    lines = [
        f"rehearsal {o.start_utc:%Y-%m-%d %H:%M:%S} .. {o.end_utc:%H:%M:%S} UTC, runtime"
        f" {o.runtime:.0f} s (clock x{s.scale:g}); real: {', '.join(sorted(s.real))};"
        f" ended: {o.why_end}"
    ]
    night = o.timelines.get("night")
    if night is not None:
        done = night.events("spectrum_done", SEQUENCER)
        lines.append(f"spectra: {len(done)}")
        for e in done:
            d = e.detail
            lines.append(
                f"  {d['spectrum']:<40} {','.join(d['telescopes']):<6} -> "
                f"{','.join(d['telescopes_end']):<6} {d['ended_by']:<6} {d['file']}"
            )
    lines.append(
        "requests at the end: " + ", ".join(f"{k} {v}" for k, v in sorted(o.statuses.items()))
    )
    lines.append(f"slews (real mounts, through the guard): {len(o.slews)}")
    for r in o.slews:
        lines.append(
            f"  {r['utc'][11:19]} {r['telescope']} RA {r['ra']:8.4f} h Dec {r['dec']:+8.3f}"
            f" alt {r['alt']:5.1f}  {r.get('seconds', '?'):>6} s  error"
            f" {r.get('error_arcsec', r.get('error', '?'))} arcsec"
        )
    errs = [r["error_arcsec"] for r in o.slews if "error_arcsec" in r]
    if errs:
        lines.append(f"  pointing error: median {np.median(errs):.1f}, max {max(errs):.1f} arcsec")
    if night is not None:
        fr = frames(o)
        lines.append(f"frames: {len(fr)}")
        for f in fr:
            rate = (
                f"{f['bytes'] / f['download_s'] / 1e6:6.2f} MB/s"
                if f.get("bytes") and f.get("download_s")
                else "      ?"
            )
            pointing = (
                f"RA {f['RA']:9.4f} Dec {f['DEC']:+8.4f} deg"
                if f.get("RA") is not None
                else "no pointing"
            )
            lines.append(
                f"  {f['telescope']} {f['file']}  exptime {f.get('EXPTIME')}  download"
                f" {f.get('download_s')} s ({rate}, {f.get('transfer')})  {pointing}"
            )
    if o.refused:
        lines.append(f"refused by the guard: {len(o.refused)}")
        for r in o.refused:
            lines.append(
                f"  {r['telescope']} {r['device']} {r['member']} {r['params']}: {r['why']}"
            )
    moves = [c for _, c in o.dome_log if c.startswith(("OPEN", "CLOSE"))]
    lines.append(f"simulated roof: {' '.join(moves)}")
    for name, rec in sorted(o.end.items()):
        lines.append(f"end action {name}: {rec}")
    return "\n".join(lines)


async def rehearse(out: Path, settings: RehearsalSettings) -> RehearsalOutcome:
    night = Rehearsal(out, settings)
    try:
        await night.start()
        return await night.run()
    finally:
        await night.close()


# -- the command line -----------------------------------------------------------------------


def parse_duration(text: str) -> float:
    """``2h``, ``90m``, ``45s``, ``1h30m`` or plain seconds."""
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", text)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"duration {text!r}: e.g. 2h, 90m, 1h30m")
    h, mi, sec = (float(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + sec


def _named(values: list[str], what: str) -> dict[str, str]:
    out = {}
    for v in values or []:
        name, eq, spec = v.partition("=")
        if not eq or not name or not spec:
            raise RehearsalRefused(f"--{what} {v!r}: expected NAME=HOST:PORT/N")
        if name in out:
            raise RehearsalRefused(f"--{what} {name} given twice")
        out[name] = spec
    return out


def settings_from_args(args: argparse.Namespace) -> RehearsalSettings:
    if not args.i_know_the_enclosure_is_closed:
        raise RehearsalRefused(
            "this moves real mounts and exposes real cameras: close the enclosure, check it is"
            " closed, and pass --i-know-the-enclosure-is-closed"
        )
    tels = _named(args.telescope, "telescope")
    cams = _named(args.camera, "camera")
    focs = _named(args.focuser, "focuser")
    rots = _named(args.rotator, "rotator")
    if not tels and not cams:
        raise RehearsalRefused("no real telescope: give --telescope NAME=... and --camera NAME=...")
    for name in sorted(set(tels) ^ set(cams)):
        raise RehearsalRefused(
            f"{name}: a real telescope needs both --telescope {name}=... and --camera {name}=..."
        )
    for extra in sorted((set(focs) | set(rots)) - set(tels)):
        raise RehearsalRefused(f"--focuser/--rotator {extra}: not a real telescope (--telescope)")
    real = {}
    try:
        for name in tels:
            mount = Endpoint.parse(tels[name])
            rot = rots.get(name)
            real[name] = RealTelescope(
                telescope=mount,
                camera=Endpoint.parse(cams[name]),
                focuser=Endpoint.parse(focs[name]) if name in focs else mount,
                rotator=None if rot == "none" else (Endpoint.parse(rot) if rot else mount),
            )
        return RehearsalSettings(
            real=real,
            end_action=EndAction.parse(args.end_action),
            duration=args.duration,
            exptime=args.exptime,
            images=args.images,
            image_exptime=args.image_exptime,
            spectra=args.spectra,
            ignore_twilight=args.ignore_twilight,
            console_port=None if args.console_port < 0 else args.console_port,
        )
    except ValueError as exc:
        raise RehearsalRefused(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rehearse the MINERVA RV night in real time on real mounts and cameras"
        " with the enclosure CLOSED; everything else simulated (docs/demo.md).",
    )
    ap.add_argument("--out", required=True, help="directory for configs, logs, data and records")
    ap.add_argument(
        "--telescope",
        action="append",
        metavar="NAME=HOST:PORT/N",
        help="a real mount's Alpaca Telescope (its Focuser and Rotator: the same server and N"
        " unless --focuser/--rotator say otherwise); e.g. t1=127.0.0.1:11121/0",
    )
    ap.add_argument(
        "--camera", action="append", metavar="NAME=HOST:PORT/N", help="its science camera"
    )
    ap.add_argument("--focuser", action="append", metavar="NAME=HOST:PORT/N", default=[])
    ap.add_argument(
        "--rotator",
        action="append",
        metavar="NAME=HOST:PORT/N|none",
        default=[],
        help="'none': no rotator",
    )
    ap.add_argument(
        "--i-know-the-enclosure-is-closed",
        action="store_true",
        help="required: the real enclosure is closed and stays closed (nothing here opens it)",
    )
    ap.add_argument(
        "--end-action",
        default="leave",
        help="each real mount at the end (also on Ctrl-C or an error): 'leave' (tracking off,"
        " where it is; default) or 'altaz:ALT,AZ' (tracking off, slew there)",
    )
    ap.add_argument(
        "--duration", type=parse_duration, default="2h", help="e.g. 2h, 90m (default 2h)"
    )
    ap.add_argument(
        "--exptime", type=float, default=60.0, help="spectrum exposure time, s (simulated)"
    )
    ap.add_argument(
        "--images", type=int, default=2, help="imaging requests per telescope (default 2)"
    )
    ap.add_argument(
        "--image-exptime", type=float, default=10.0, help="real camera exposure time, s"
    )
    ap.add_argument("--spectra", type=int, help="number of SPECTRUM requests (default: enough)")
    ap.add_argument(
        "--ignore-twilight",
        action="store_true",
        help=f"observe now even if the Sun is up (targets >= {MIN_SUN_SEP:g} deg from the Sun)",
    )
    ap.add_argument(
        "--console-port", type=int, default=0, help="observer console port (0 any, -1 none)"
    )
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    if isinstance(args.duration, str):
        args.duration = parse_duration(args.duration)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = settings_from_args(args)
    except RehearsalRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        outcome = asyncio.run(rehearse(Path(args.out), settings))
    except RehearsalRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(summary(outcome))
    problems = check(outcome)
    for p in problems:
        print("PROBLEM:", p)
    print("OK: every check passed" if not problems else f"{len(problems)} problems")
    return 0 if not problems else 1


__all__ = [
    "EndAction",
    "RealTelescope",
    "Rehearsal",
    "RehearsalOutcome",
    "RehearsalRefused",
    "RehearsalSettings",
    "check",
    "main",
    "rehearse",
    "summary",
]

if __name__ == "__main__":
    sys.exit(main())
