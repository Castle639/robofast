"""robofast-pwi-jog: read a PWI mount's status and make small, bounded moves.

For a first session on real hardware (``docs/pwi.md``, "First hardware
session"): it talks straight to PWI (PWI4 or PWI2) through the same client
and the same hardware gate as the PWI server, using that server's config
file, and logs everything it reads and sends.

::

    robofast-pwi-jog --config t1_pwi2.json [--hardware] status [--watch S]
    robofast-pwi-jog --config ... --hardware stop
    robofast-pwi-jog --config ... --hardware connect
    robofast-pwi-jog --config ... --hardware enable | disable
    robofast-pwi-jog --config ... --hardware tracking-off
    robofast-pwi-jog --config ... --hardware altaz --dalt 0.5 --daz 0 [--yes]
    robofast-pwi-jog --config ... --hardware radec --dra 60 --ddec 0 [--yes]

* ``status`` (the default) only reads; ``--watch S`` repeats every S s.
* ``stop`` sends PWI's mount stop at once (no question asked).
* ``connect`` asks PWI to connect to the mount controller; ``enable`` /
  ``disable`` switch both axis motors (``enable`` asks for confirmation).
  Nothing else here ever connects, enables or homes.
* ``altaz``: a relative alt/az move (degrees), sent as a PWI alt/az goto to
  the current position plus the offsets. Each offset at most
  ``max_jog_deg`` (config, default 1); the target altitude within
  [``min_alt``, ``max_alt``]; tracking must be off; an azimuth move across
  0/360 is refused (the mount's cable wrap could take the long way).
* ``radec``: a relative RA/Dec offset in arcsec (RA as *coordinate* arcsec,
  as PWI applies it) while tracking, each at most ``max_jog_deg`` x 3600.

Every move prints its plan and waits for ``yes`` on the terminal (or
``--yes``), then polls PWI every ``--poll`` s and prints the position.
It sends **stop** on Ctrl-C, after ``jog_timeout`` s, if the mount goes
further from where it started than the plan allows (+ ``--slack`` deg), or
if the altitude leaves the limits. The log is appended to
``$ROBOFAST_DATA_DIR/pwi-jog/<name>.log`` (or ``--log``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import logging
import math
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from robofast_core import hwgate
from robofast_core.config import load_device_config
from robofast_core.devices.pwi_client import PWIClient, PWIError, PWIStatus, make_client
from robofast_core.devices.pwi_server import PWIConfig, _sep_deg

log = logging.getLogger("robofast.pwi_jog")


class JogRefused(Exception):
    """A move that is not allowed (limits, state, cap)."""


def fmt(st: PWIStatus) -> str:
    f = st.focuser_position
    return (
        f"alt {st.alt_deg:8.4f}  az {st.az_deg:8.4f}  ra {st.ra_hours:8.5f} h"
        f"  dec {st.dec_deg:+8.4f}  conn {int(st.connected)}"
        f"  motors {int(st.az_enabled)}{int(st.alt_enabled)}  track {int(st.tracking)}"
        f"  slew {st.slewing if st.slewing is not None else '?'}"
        f"  m3 {st.m3_port}  focus {'?' if f is None else round(f)}"
    )


def plan_altaz(cfg: PWIConfig, st: PWIStatus, dalt: float, daz: float) -> tuple[float, float]:
    """The alt/az target of a relative move, or :class:`JogRefused`."""
    cap = cfg.max_jog_deg
    for name, v in (("dalt", dalt), ("daz", daz)):
        if not math.isfinite(v):
            raise JogRefused(f"{name} is not a number")
        if abs(v) > cap:
            raise JogRefused(f"{name} {v} deg exceeds max_jog_deg {cap}")
    if dalt == 0 and daz == 0:
        raise JogRefused("nothing to move")
    if not st.connected or not st.motors_enabled:
        raise JogRefused("mount not connected or motors disabled (connect / enable first)")
    if st.tracking:
        raise JogRefused("tracking is on: send tracking-off (or stop) first")
    alt = st.alt_deg + dalt
    if not cfg.min_alt <= alt <= cfg.max_alt:
        raise JogRefused(f"target altitude {alt:.3f} outside [{cfg.min_alt}, {cfg.max_alt}]")
    az = st.az_deg + daz
    if not 0.0 <= az < 360.0:
        raise JogRefused(f"azimuth move {st.az_deg:.3f} -> {az:.3f} crosses 0/360 (cable wrap)")
    return alt, az


def plan_radec(cfg: PWIConfig, st: PWIStatus, dra: float, ddec: float) -> float:
    """The planned on-sky size (deg) of an RA/Dec offset, or :class:`JogRefused`."""
    cap = cfg.max_jog_deg * 3600.0
    for name, v in (("dra", dra), ("ddec", ddec)):
        if not math.isfinite(v):
            raise JogRefused(f"{name} is not a number")
        if abs(v) > cap:
            raise JogRefused(f"{name} {v} arcsec exceeds max_jog_deg {cfg.max_jog_deg} deg")
    if dra == 0 and ddec == 0:
        raise JogRefused("nothing to move")
    if not st.connected or not st.motors_enabled:
        raise JogRefused("mount not connected or motors disabled")
    if not st.tracking:
        raise JogRefused("an RA/Dec offset needs tracking on")
    east = dra * math.cos(math.radians(st.dec_deg))
    return math.hypot(east, ddec) / 3600.0


class Jog:
    def __init__(
        self,
        cfg: PWIConfig,
        client: PWIClient,
        *,
        out: Callable[[str], None] = print,
        confirm: Callable[[str], Awaitable[bool]] | None = None,
        poll: float = 0.25,
        slack: float = 0.5,
    ) -> None:
        self.cfg = cfg
        self.pwi = client
        self.out = out
        self.confirm = confirm or _ask
        self.poll = poll
        self.slack = slack

    def say(self, text: str) -> None:
        log.info("%s", text)
        self.out(text)

    async def status(self) -> PWIStatus:
        st = await self.pwi.status()
        self.say(fmt(st))
        log.debug("status %s", json.dumps(st.as_dict()))
        return st

    async def stop(self, why: str) -> None:
        self.say(f"STOP ({why})")
        try:
            await self.pwi.stop()
        except PWIError as exc:
            self.say(f"stop FAILED: {exc} -- use the PWI GUI / power now")
            raise
        self.say("stop sent")

    async def simple(self, what: str) -> None:
        fn = {
            "connect": self.pwi.mount_connect,
            "enable": self.pwi.enable_motors,
            "disable": self.pwi.disable_motors,
            "tracking-off": lambda: self.pwi.tracking(False),
        }[what]
        st = await self.status()
        if what == "enable" and not await self.confirm(
            f"enable both axis motors of {self.cfg.name}"
            f" (alt {st.alt_deg:.3f}, az {st.az_deg:.3f})?"
        ):
            self.say("not confirmed: nothing sent")
            return
        self.say(f"sending {what}")
        await fn()
        await self.status()

    async def altaz(self, dalt: float, daz: float) -> bool:
        st = await self.status()
        alt, az = plan_altaz(self.cfg, st, dalt, daz)
        planned = _sep_deg(st.alt_deg, st.az_deg, alt, az)
        text = (
            f"PLAN {self.cfg.name}: alt {st.alt_deg:.4f} -> {alt:.4f} ({dalt:+.4f}),"
            f" az {st.az_deg:.4f} -> {az:.4f} ({daz:+.4f}); {planned:.4f} deg on the sky"
        )
        self.say(text)
        if not await self.confirm(text + "\nmove?"):
            self.say("not confirmed: nothing sent")
            return False
        start = (st.alt_deg, st.az_deg)
        self.say(f"goto alt {alt:.5f} az {az:.5f}")
        await self.pwi.goto_altaz(alt, az)
        return await self._watch(start, planned, target=(alt, az))

    async def radec(self, dra: float, ddec: float) -> bool:
        st = await self.status()
        planned = plan_radec(self.cfg, st, dra, ddec)
        text = (
            f"PLAN {self.cfg.name}: offset RA {dra:+.1f} coordinate arcsec, Dec {ddec:+.1f}"
            f" arcsec ({planned * 3600:.1f} arcsec on the sky) while tracking"
        )
        self.say(text)
        if not await self.confirm(text + "\nmove?"):
            self.say("not confirmed: nothing sent")
            return False
        start = (st.alt_deg, st.az_deg)
        self.say(f"offset ra {dra} dec {ddec}")
        await self.pwi.offset_radec(dra, ddec)
        return await self._watch(start, planned, target=None)

    async def _watch(
        self, start: tuple[float, float], planned: float, target: tuple[float, float] | None
    ) -> bool:
        """Poll until the move ends; stop on any surprise. True if it arrived."""
        cfg = self.cfg
        t0 = time.monotonic()
        settled_since: float | None = None
        last: PWIStatus | None = None
        try:
            while True:
                await asyncio.sleep(self.poll)
                elapsed = time.monotonic() - t0
                try:
                    st = await self.pwi.status()
                except PWIError as exc:
                    self.say(f"status failed: {exc}")
                    if elapsed > cfg.jog_timeout:
                        await self.stop("timeout, PWI not answering")
                        return False
                    continue
                moved = _sep_deg(st.alt_deg, st.az_deg, *start)
                # Tracking drifts alt/az at up to ~15"/s.
                allowed = planned + self.slack + (0.005 * elapsed if target is None else 0.0)
                line = f"t {elapsed:5.1f}s  {fmt(st)}  moved {moved:.4f}"
                if target is not None:
                    line += f"  to go {_sep_deg(st.alt_deg, st.az_deg, *target):.4f}"
                self.say(line)
                if moved > allowed:
                    await self.stop(f"moved {moved:.3f} deg, more than planned {planned:.3f}")
                    return False
                margin = cfg.limit_margin
                if not cfg.min_alt - margin <= st.alt_deg <= cfg.max_alt + margin:
                    await self.stop(f"altitude {st.alt_deg:.3f} outside the limits")
                    return False
                if elapsed > cfg.jog_timeout:
                    await self.stop(f"no arrival within jog_timeout {cfg.jog_timeout} s")
                    return False
                still = last is not None and (
                    _sep_deg(st.alt_deg, st.az_deg, last.alt_deg, last.az_deg) < 0.001
                    or (target is None and not st.slewing)
                )
                close = target is None or _sep_deg(st.alt_deg, st.az_deg, *target) < 0.01
                if still and close and st.slewing is not True:
                    settled_since = settled_since or time.monotonic()
                    if time.monotonic() - settled_since >= 1.0:
                        self.say("arrived")
                        return True
                else:
                    settled_since = None
                last = st
        except (KeyboardInterrupt, asyncio.CancelledError):
            await self.stop("interrupted")
            raise


async def _ask(prompt: str) -> bool:
    """Ask on the terminal without blocking the event loop, so Ctrl-C at the
    prompt cancels the move before anything is sent."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def read() -> None:
        try:
            text = input(prompt + " type yes to go: ")
        except EOFError:
            text = ""
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(text))

    threading.Thread(target=read, name="jog-confirm", daemon=True).start()
    return (await fut).strip().lower() == "yes"


async def _yes(prompt: str) -> bool:
    return True


def _log_path(cfg: PWIConfig, override: str | None) -> Path:
    if override:
        return Path(override)
    from robofast_core.zenodo import data_dir

    return data_dir() / "pwi-jog" / f"{cfg.name}.log"


async def run(args: argparse.Namespace, cfg: PWIConfig) -> int:
    allow = hwgate.gate_open(cfg.allow_hardware, args.hardware, what="PWI hardware")
    ip = hwgate.check_endpoint(
        cfg.pwi.host, cfg.pwi_port, allow_hardware=allow, what=f"{cfg.backend} PWI"
    )
    client = make_client(
        cfg.backend,
        ip,
        cfg.pwi_port,
        timeout=cfg.pwi.timeout,
        connect_timeout=cfg.pwi.connect_timeout,
    )
    log.info(
        "robofast-pwi-jog %s on %s %s:%d (hardware gate %s)",
        args.command,
        cfg.backend,
        ip,
        cfg.pwi_port,
        "OPEN" if allow else "closed",
    )
    confirm = _yes if args.yes else None
    jog = Jog(cfg, client, confirm=confirm, poll=args.poll, slack=args.slack)
    task: asyncio.Task | None = None
    try:
        if args.command == "status":
            while True:
                await jog.status()
                if not args.watch:
                    return 0
                await asyncio.sleep(args.watch)
        if args.command == "stop":
            await jog.stop("requested")
            await jog.status()
            return 0
        if args.command in ("connect", "enable", "disable", "tracking-off"):
            await jog.simple(args.command)
            return 0
        if args.command == "altaz":
            task = asyncio.ensure_future(jog.altaz(args.dalt, args.daz))
        else:
            task = asyncio.ensure_future(jog.radec(args.dra, args.ddec))
        return 0 if await task else 1
    except JogRefused as exc:
        jog.say(f"REFUSED: {exc}")
        return 3
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="robofast-pwi-jog", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="the PWI server's JSON config")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a real PWI (the config must also say allow_hardware)",
    )
    ap.add_argument("--yes", action="store_true", help="do not ask before moving")
    ap.add_argument(
        "--poll", type=float, default=0.25, help="status poll interval during a move (s)"
    )
    ap.add_argument(
        "--slack",
        type=float,
        default=0.5,
        help="stop if it moves this much more than planned (deg)",
    )
    ap.add_argument("--log", help="log file (default: $ROBOFAST_DATA_DIR/pwi-jog/<name>.log)")
    sub = ap.add_subparsers(dest="command")
    st = sub.add_parser("status", help="read only (the default)")
    st.add_argument("--watch", type=float, default=0.0, help="repeat every S seconds")
    sub.add_parser("stop", help="mount stop, at once")
    sub.add_parser("connect", help="PWI connects to the mount controller")
    sub.add_parser("enable", help="enable both axis motors (asks first)")
    sub.add_parser("disable", help="disable both axis motors")
    sub.add_parser("tracking-off", help="turn tracking off")
    aa = sub.add_parser("altaz", help="relative alt/az move, degrees")
    aa.add_argument("--dalt", type=float, default=0.0)
    aa.add_argument("--daz", type=float, default=0.0)
    rd = sub.add_parser("radec", help="relative RA/Dec offset while tracking, arcsec")
    rd.add_argument("--dra", type=float, default=0.0, help="RA coordinate arcsec")
    rd.add_argument("--ddec", type=float, default=0.0)
    args = ap.parse_args(argv)
    if args.command is None:
        args.command, args.watch = "status", 0.0
    cfg = load_device_config(args.config, PWIConfig.from_dict)
    path = _log_path(cfg, args.log)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info(
        "argv %s (log %s, %s)",
        sys.argv[1:] if argv is None else argv,
        path,
        datetime.datetime.now(datetime.UTC).isoformat(),
    )
    print(f"log: {path}", flush=True)
    try:
        code = asyncio.run(run(args, cfg))
    except hwgate.HardwareNotAllowed as exc:
        log.error("%s", exc)
        print(f"REFUSED: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    except PWIError as exc:
        log.error("%s", exc)
        print(f"PWI error: {exc}", file=sys.stderr)
        code = 4
    sys.exit(code)


if __name__ == "__main__":
    main()
