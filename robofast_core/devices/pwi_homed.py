"""Whether a PWI4 mount is homed when PWI4 itself does not say (docs/pwi.md).

PWI4 (4.1.6 on a CDK700) reports no homed flag in ``/status``, so robofast
keeps its own record: a small JSON file per telescope
(``$ROBOFAST_DATA_DIR/pwi-homed/<name>.json``, or the config's
``home_record``), written only when a home **robofast commanded** (the
server's FindHome or ``robofast-pwi-jog home``) ended with the mount still,
at the configured home position (``home_alt``/``home_az``, within
``home_tolerance``) -- PWI4 can abort its own fine search and stop
elsewhere, and that is not a home.

The record is evidence, not a promise, so it is dropped (the file removed)
as soon as anything reading PWI sees a sign that the encoders may have lost
their reference:

* PWI not connected to the mount (a PWI4 restart, a controller power cycle,
  a disconnect): reconnecting does not bring the record back;
* both axes reading within :data:`RESET_DEG` of 0 deg, which is what a CDK700
  controller reports after a power cycle, before its first home;
* a different PWI version than the one that homed.

What it cannot see: a controller power cycle while nothing of robofast's
reads PWI, followed by a move away from 0 by another client. After any power
cycle, home again (``robofast-pwi-jog home``) or drop the record
(``robofast-pwi-jog forget-home``).

A PWI that reports homed state itself (PWI2 ``encoders_have_been_set``,
PWI4 ``mount.axisN.is_homed`` where present) is always believed instead.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

from robofast_core.devices.pwi_client import PWIStatus

log = logging.getLogger(__name__)

#: Both axes within this of 0 deg: encoders as after a controller power cycle.
RESET_DEG = 1.0


def default_path(name: str) -> Path:
    from robofast_core.zenodo import data_dir

    return data_dir() / "pwi-homed" / f"{name}.json"


def _az_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def encoders_reset(st: PWIStatus) -> bool:
    """Both axes read ~0 deg: the controller has not been homed since power-on."""
    return abs(st.alt_deg) < RESET_DEG and _az_diff(st.az_deg, 0.0) < RESET_DEG


def lost_reason(st: PWIStatus) -> str | None:
    """Why ``st`` says any homing done earlier no longer counts, or None."""
    if not st.connected:
        return "PWI is not connected to the mount (PWI restart or controller power cycle?)"
    if encoders_reset(st):
        return (
            f"both axes read ~0 deg (alt {st.alt_deg:.3f}, az {st.az_deg:.3f}): encoders as"
            " after a controller power cycle"
        )
    return None


def not_at_home(
    st: PWIStatus,
    axes: tuple[int, ...] | set[int],
    home_alt: float | None,
    home_az: float | None,
    tolerance: float,
) -> str | None:
    """None if each homed axis ended at its configured home; else why not."""
    if st.slewing:
        return "PWI still reports slewing"
    for axis in axes:
        want = home_az if axis == 0 else home_alt
        if want is None:
            return f"no home_{'az' if axis == 0 else 'alt'} in the config to check the home against"
        got = st.az_deg if axis == 0 else st.alt_deg
        off = _az_diff(got, want) if axis == 0 else abs(got - want)
        if off > tolerance:
            name = "azimuth" if axis == 0 else "altitude"
            return (
                f"{name} {got:.3f} deg is {off:.3f} deg from the configured home {want}"
                f" (more than home_tolerance {tolerance}): PWI may have aborted the home"
            )
    return None


class HomeRecord:
    """The record file of one telescope. Safe to share between processes
    (the server and the jog tool): every check re-reads the file when it
    changed, and writes replace it atomically."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._cache: dict[str, Any] | None = None
        self._stamp: tuple[int, int] | None = None

    def load(self) -> dict[str, Any] | None:
        try:
            s = self.path.stat()
        except OSError:
            self._cache, self._stamp = None, None
            return None
        stamp = (s.st_mtime_ns, s.st_size)
        if stamp != self._stamp:
            try:
                data = json.loads(self.path.read_text())
                self._cache = data if isinstance(data, dict) else None
            except (OSError, ValueError):
                self._cache = None
            self._stamp = stamp
        return self._cache

    def axes(self, st: PWIStatus) -> set[int]:
        """The axes the record says are homed, for this PWI (version)."""
        rec = self.load()
        if not rec or rec.get("version") != st.version:
            return set()
        out = set()
        for k in (rec.get("axes") or {}).keys():
            with contextlib.suppress(ValueError):
                if int(k) in (0, 1):
                    out.add(int(k))
        return out

    def record(self, axes: tuple[int, ...] | set[int], st: PWIStatus, how: str) -> None:
        rec = self.load() or {}
        if rec.get("version") != st.version:
            rec = {}
        now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        entries = dict(rec.get("axes") or {})
        for axis in axes:
            entries[str(axis)] = {
                "at": now,
                "how": how,
                "position_deg": round(st.az_deg if axis == 0 else st.alt_deg, 4),
            }
        rec = {"version": st.version, "backend": st.backend, "axes": entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(rec, indent=1) + "\n")
        os.replace(tmp, self.path)
        log.warning("home recorded (axes %s, %s) in %s", sorted(axes), how, self.path)

    def clear(self, why: str) -> bool:
        """Drop the record; True if there was one."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        self._cache, self._stamp = None, None
        log.warning("home record %s dropped: %s", self.path, why)
        return True

    def observe(self, st: PWIStatus) -> str | None:
        """Drop the record on any sign in ``st`` that it no longer holds;
        returns that sign (also when there was no record)."""
        why = lost_reason(st)
        rec = self.load()
        if why is None and rec and rec.get("version") != st.version:
            why = f"PWI version changed ({rec.get('version')} -> {st.version})"
        if why is not None and rec:
            self.clear(why)
        return why
