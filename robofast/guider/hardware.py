"""The guider's hardware backends, by name (docs/guider.md, docs/plugins.md).

A guider config names each part's backend by its ``kind``::

    "camera":    {"kind": "sim" | "alpaca" | "zwo" | "framegrabber", ...}
    "tiptilt":   {"kind": "sim" | "sxao" | "sxao-sim" | "pi" | "pi-sim", ...}
    "mount":     {"kind": "alpaca" | "sim", ...}
    "backlight": {"kind": "sim" | <a plug-in's>, ...}

and each kind is an entry in one of four registries (:data:`CAMERAS`,
:data:`TIPTILTS`, :data:`MOUNTS`, :data:`BACKLIGHTS`; each a
:class:`robofast.plugins.Registry`). An entry is a factory
``factory(spec, ctx)``: ``spec`` the part's config without ``kind``, ``ctx``
the :class:`BuildContext` (the hardware gate, the clock, the simulated sky
when there is one). Core registers the backends above; a plug-in adds its
own the same way (a spectrograph's fiber backlight is registered by the
plug-in that drives that spectrograph: KiwiSpec's by the built-in
:mod:`robofast.devices`).

Every backend that reaches real hardware passes ``ctx.allowed`` to its
driver, which refuses unless the hardware gate is open (module doc of
:mod:`robofast.guider.service`). Building a part never moves it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robofast.plugins import Registry

__all__ = ["BACKLIGHTS", "CAMERAS", "MOUNTS", "TIPTILTS", "BuildContext", "Factory", "spec_kind"]


@dataclass
class BuildContext:
    """What a backend factory may use."""

    #: The hardware gate (``allow_hardware`` and ``--hardware``).
    allowed: bool
    clock: Any
    #: The guider's ``sim`` section (the simulated sky's settings).
    sim: dict[str, Any]
    #: The simulated sky, once a simulated camera made it (None: real sky).
    world: Any = None


#: ``factory(spec, ctx) -> part``.
Factory = Callable[[dict[str, Any], BuildContext], Any]

CAMERAS: Registry[Factory] = Registry("guide camera kind")
TIPTILTS: Registry[Factory] = Registry("tip/tilt kind")
MOUNTS: Registry[Factory] = Registry("guider mount kind")
BACKLIGHTS: Registry[Factory] = Registry("fiber backlight kind")


def spec_kind(spec: Any, part: str, default: str | None = None) -> tuple[str, dict[str, Any]]:
    """``(kind, the rest)`` of a part's config. A backlight may also be
    written ``{<kind>: <url>}`` or ``{<kind>: true}`` (the earlier form)."""
    if not isinstance(spec, dict):
        raise ValueError(f"{part}: an object with a 'kind', not {spec!r}")
    s = dict(spec)
    if "kind" in s:
        return str(s.pop("kind")), s
    if part == "backlight" and len(s) == 1:
        ((kind, value),) = s.items()
        return kind, {} if value is True else {"url": value}
    if default is None:
        raise ValueError(f"{part}: 'kind' is required")
    return default, s


# -- core's backends ---------------------------------------------------------------------


def _limits(v: Any) -> Any:
    return (
        None if v is None else ((float(v[0][0]), float(v[0][1])), (float(v[1][0]), float(v[1][1])))
    )


def _sim_camera(spec: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.service import build_world
    from robofast.guider.sim import SimCamera

    ctx.world = build_world(ctx.sim, ctx.clock)
    return SimCamera(ctx.world, readout=float(spec.get("readout", 0.0)))


def _alpaca_camera(spec: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.camera import AlpacaCamera

    return AlpacaCamera.at(
        spec["address"], int(spec.get("number", 0)), allow_hardware=ctx.allowed, clock=ctx.clock
    )


def _zwo_camera(spec: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.camera import ZWOCamera

    return ZWOCamera(
        index=int(spec.get("index", 0)),
        library=spec.get("library"),
        allow_hardware=ctx.allowed,
        clock=ctx.clock,
    )


def _framegrabber_camera(spec: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.camera import FrameGrabberCamera

    return FrameGrabberCamera(device=str(spec.get("device", "")), allow_hardware=ctx.allowed)


def _sim_tiptilt(t: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.sim import SimTipTilt

    tt = SimTipTilt(
        limits=_limits(t.get("limits", [[-100, 100], [-100, 100]])),
        tau=float(t.get("tau", 2e-3)),
        delay=float(t.get("delay", 0.0)),
        latency=float(t.get("latency", 0.0)),
        clock=ctx.clock,
    )
    if ctx.world is not None:
        ctx.world.tiptilt = tt
    return tt


def _sxao(simulated: bool) -> Factory:
    def build(t: dict[str, Any], ctx: BuildContext) -> Any:
        from robofast.guider.sim import PlantView
        from robofast.guider.tiptilt import SXAO, SerialTransport, SXAOSim

        term = str(t.get("terminator", "")).encode().decode("unicode_escape").encode("latin-1")
        transport: Any
        if not simulated:
            transport = SerialTransport(
                t["port"], baudrate=int(t.get("baudrate", 9600)), allow_hardware=ctx.allowed
            )
        else:
            sim = SXAOSim(travel=int(t.get("travel", 1000)), terminator=term, clock=ctx.clock)
            transport = sim
            if ctx.world is not None:
                ctx.world.tiptilt = PlantView(lambda _t, s=sim: s.position)
        sx = SXAO(
            transport,
            limits=_limits(t.get("limits", [[-500, 500], [-500, 500]])),
            terminator=term,
            timeout=float(t.get("timeout", 0.5)),
        )
        sx.clock = ctx.clock
        return sx

    return build


def _pi(simulated: bool) -> Factory:
    def build(t: dict[str, Any], ctx: BuildContext) -> Any:
        from robofast.guider.sim import PlantView
        from robofast.guider.tiptilt import PIGCS, SimGCS, open_pi_gcs

        gcs: Any
        if not simulated:
            gcs = open_pi_gcs(
                usb_serial=t.get("usb_serial"),
                host=t.get("host"),
                port=int(t.get("tcp_port", 50000)),
                allow_hardware=ctx.allowed,
            )
        else:
            gcs = SimGCS(tau=float(t.get("tau", 1e-3)), clock=ctx.clock)
            gcs.ConnectUSB("simulated")
            if ctx.world is not None:
                ctx.world.tiptilt = PlantView(gcs.position_at)
        pi = PIGCS(gcs, margin=float(t.get("margin", 0.0)), in_thread=not simulated)
        pi.clock = ctx.clock
        return pi

    return build


def _sim_mount(m: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.sim import SimMount

    m.setdefault("guide_rate", 7.5)  # the simulator's own rate
    driver = SimMount(
        tau=float(m.get("tau", 0.5)),
        delay=float(m.get("delay", 0.1)),
        guide_rate=float(m["guide_rate"]),
        clock=ctx.clock,
    )
    if ctx.world is not None:
        ctx.world.mount = driver
    return driver


def _alpaca_mount(m: dict[str, Any], ctx: BuildContext) -> Any:
    from robofast.guider.tiptilt import alpaca_telescope

    return alpaca_telescope(m["address"], int(m.get("number", 0)), allow_hardware=ctx.allowed)


def _sim_backlight(spec: dict[str, Any], ctx: BuildContext) -> Any:
    """The simulated sky's backlight (None without a simulated sky)."""
    from robofast.guider.loop import Backlight

    w = ctx.world
    if w is None:
        return None

    async def _set(on: bool) -> None:
        w.backlit = on

    return Backlight(_set)


CAMERAS.register("sim", _sim_camera)
CAMERAS.register("alpaca", _alpaca_camera)
CAMERAS.register("zwo", _zwo_camera)
CAMERAS.register("framegrabber", _framegrabber_camera)
TIPTILTS.register("sim", _sim_tiptilt)
TIPTILTS.register("sxao", _sxao(False))
TIPTILTS.register("sxao-sim", _sxao(True))
TIPTILTS.register("pi", _pi(False))
TIPTILTS.register("pi-sim", _pi(True))
#: A mount factory returns the driver; the guider wraps it in a MountOffset.
MOUNTS.register("sim", _sim_mount)
MOUNTS.register("alpaca", _alpaca_mount)
BACKLIGHTS.register("sim", _sim_backlight)
