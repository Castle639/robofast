"""Alpaca server for KiwiSpec's auxiliary devices, driven directly over their serial ports.

The iodine cell heater, the iodine stage, the fiber backlight, the vacuum
gauges and the chiller hang off serial ports on the spectrograph PC. Legacy
MINERVA reached them only through ``spectrograph_server.py`` on that PC.
This server drives them itself, each through a serial port named by a
**URL** (:mod:`robofast_core.devices.serial_url`): ``rfc2217://host:port``
for a serial-to-TCP bridge on the PC, a local device, or ``sim``. It runs
on Linux, anywhere robofast does. Hardware, protocols and what still needs
confirming: ``docs/kiwispec_aux.md``.

Devices (each optional: a device whose section is absent is not served)
------------------------------------------------------------------------
* ``switch/0`` -- **the iodine cell heater** (Thorlabs TC200,
  :mod:`robofast_core.devices.tc200`; ISwitchV2):

  ====  ======================  ========  ====================================
  id    name                    writable  value
  ====  ======================  ========  ====================================
  0     ``heater_enabled``      yes       0/1; written by read status, toggle
                                          ``ens`` only if it differs, verify
  1     ``heater_setpoint``     yes       C, ``setpoint_min..setpoint_max``,
                                          step 0.1; read back and verified
  2     ``cell_temperature``    no        C (``tact?``)
  3     ``heater_alarm``        no        0/1: the controller's sensor alarm
  ====  ======================  ========  ====================================

* ``filterwheel/0`` -- **the iodine stage** by named position (Thorlabs
  APT, :mod:`robofast_core.devices.thorlabs_apt`; IFilterWheelV3):
  ``Names`` are the configured positions (``in``, ``flat``, ``out``);
  ``Position`` = n starts the move and returns; reading ``Position`` is -1
  while the stage moves, the index of the named position it rests at, or an
  ``InvalidOperation`` error (not homed, between positions, or the last move
  failed). Actions: ``Status`` (JSON), ``Home`` (**moves the stage**; needed
  once after the controller powers up), ``MoveMM`` (Parameters: millimetres,
  within ``travel``; legacy's ``i2stage_movef``), ``Stop``.
* ``covercalibrator/0`` -- **the fiber backlight** (ICoverCalibratorV2):
  the **cover** is the arm a Dynamixel servo swings in over the fiber
  (:mod:`robofast_core.devices.dynamixel2`): ``CloseCover`` = in the beam
  (``in_deg``), ``OpenCover`` = out of it (``out_deg``, then torque off);
  ``HaltCover`` is not implemented. The **calibrator** is the LED
  (:class:`~robofast_core.devices.kiwispec_serial.BacklightLED`):
  ``MaxBrightness`` 1. **Failsafe:** the LED heats the spectrograph, so it
  is turned off, and the arm swung out, ``max_on`` s (default 60, as
  legacy) after the last ``CalibratorOn``. A servo that does not answer
  makes ``CoverState`` ``Error`` and each cover command fail with a message
  naming the servo, the port and what to check; the LED still works.
* ``switch/1`` (``switch/0`` without a heater) -- **telemetry**, all
  read-only: ``spec_pressure`` and ``pump_pressure`` (mbar; the gauge
  controller's CG2 and CG1), ``chiller_temperature``, ``chiller_setpoint``,
  ``chiller_pump_temperature`` (C), ``chiller_running`` and
  ``chiller_fault`` (0/1). Action ``Status``: everything as JSON, with the
  chiller's status and fault bits.

**Vacuum: telemetry only.** robofast does not, and this server cannot,
open or close the pump valve or the vent valve, or switch the vacuum pump
or their PDU outlets: the gauge driver can send exactly two queries.

Safety
------
A URL other than ``sim`` or ``loop://`` needs ``"allow_hardware": true`` in
the config **and** ``--hardware``, unless it is a verified robofast
simulator on loopback (:mod:`robofast_core.devices.kiwispec_aux_sim`).
**Starting the server moves nothing**: connecting reads (identify, status,
position) and turns the backlight LED off; the stage is homed only on
``Action("Home")``. Stopping it sends nothing.

Running
-------
::

    python -m robofast_core.devices.kiwispec_aux_server --config kiwispec_aux.json [--hardware]

with a JSON config (``sites/minerva/kiwispec_aux.example.json``). It prints
one JSON line, ``{"url": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import math
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from typing import Any

from robofast_core import hwgate
from robofast_core.config import load_device_config
from robofast_core.devices import dynamixel2 as dx
from robofast_core.devices.aqawan_server import (
    ACTION_NOT_IMPLEMENTED,
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    UINT32_MAX,
    AlpacaException,
    _BadRequest,
    _bool_param,
    _Discovery,
)
from robofast_core.devices.kiwispec_aux_sim import (
    APTSim,
    GaugeSim,
    LEDSim,
    OasisSim,
    TC200Sim,
    XMServoSim,
)
from robofast_core.devices.kiwispec_serial import (
    CHILLER_BAUD,
    GAUGE_BAUD,
    LED_BAUD,
    BacklightLED,
    GaugeController,
    OasisChiller,
)
from robofast_core.devices.serial_url import (
    HardwareNotAllowed,
    Port,
    PortConfig,
    PortError,
    PortTimeout,
    open_port,
)
from robofast_core.devices.tc200 import BAUD as TC200_BAUD
from robofast_core.devices.tc200 import TC200
from robofast_core.devices.tcsgo_server import COMMON_NAMES, _float_param, _Handler, _now_stamp
from robofast_core.devices.thorlabs_apt import BAUD as APT_BAUD
from robofast_core.devices.thorlabs_apt import APTConfig, APTStage

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
TIMEOUT = 0x502
INTERFACE = {"switch": 2, "filterwheel": 3, "covercalibrator": 2}

# CalibratorStatus and CoverStatus
CAL_OFF, CAL_READY, CAL_UNKNOWN = 1, 3, 4
COVER_CLOSED, COVER_MOVING, COVER_OPEN, COVER_UNKNOWN, COVER_ERROR = 1, 2, 3, 4, 5


# -- configuration ----------------------------------------------------------------


def _fields(cls: type, d: dict[str, Any], what: str) -> dict[str, Any]:
    d = {k: v for k, v in d.items() if not k.startswith("_")}
    unknown = set(d) - {f.name for f in dataclasses.fields(cls)}
    if unknown:
        raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
    return d


def _positive(obj: Any, names: tuple[str, ...], what: str) -> None:
    for n in names:
        v = getattr(obj, n)
        if not isinstance(v, int | float) or not v > 0:
            raise ValueError(f"{what}.{n} must be a positive number")


@dataclass
class HeaterConfig:
    port: PortConfig
    #: Bounds on the set point a client may write (C). The cell is held at
    #: 55 C (legacy I2SETTEMP).
    setpoint_min: float = 40.0
    setpoint_max: float = 65.0
    timeout: float = 2.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HeaterConfig:
        d = _fields(cls, d, "heater")
        d["port"] = PortConfig.from_dict(d.get("port", "sim"), baud=TC200_BAUD, what="heater")
        cfg = cls(**d)
        if not cfg.setpoint_min < cfg.setpoint_max:
            raise ValueError("heater: setpoint_min must be below setpoint_max")
        _positive(cfg, ("timeout",), "heater")
        return cfg


@dataclass
class StageConfig:
    port: PortConfig
    #: Named positions (mm), in ``Names`` order: site values. ``null`` =
    #: not yet confirmed: listed but refused.
    positions: dict[str, float | None]
    #: Controller counts per millimetre; ``null`` until confirmed on the
    #: hardware: then the stage is served, but every position and move is
    #: refused (``Status`` still reads counts).
    counts_per_mm: float | None = None
    travel: tuple[float, float] = (0.0, 150.0)
    channel: int = 1
    dest: int = 0x50
    status_query: str = "update"
    timeout: float = 2.0
    move_timeout: float = 120.0
    home_timeout: float = 180.0
    tolerance: float = 0.05
    poll_interval: float = 0.25
    start_grace: float = 1.0
    status_max_age: float = 0.2

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StageConfig:
        d = _fields(cls, d, "stage")
        port = PortConfig.from_dict(d.get("port", "sim"), baud=APT_BAUD, what="stage")
        if "rtscts" not in (d.get("port") if isinstance(d.get("port"), dict) else {}):
            port.rtscts = True  # the APT protocol's handshake
        d["port"] = port
        if "positions" not in d:
            raise ValueError("stage: positions (named positions in mm) are required")
        d["positions"] = {k: v for k, v in d["positions"].items() if not k.startswith("_")}
        if "travel" in d:
            d["travel"] = (float(d["travel"][0]), float(d["travel"][1]))
        cfg = cls(**d)
        if not cfg.positions:
            raise ValueError("stage.positions: at least one named position")
        lo, hi = cfg.travel
        for name, mm in cfg.positions.items():
            if not name or not name.isprintable():
                raise ValueError(f"stage.positions: bad name {name!r}")
            if mm is not None and (
                not isinstance(mm, int | float) or not math.isfinite(mm) or not lo <= mm <= hi
            ):
                raise ValueError(f"stage.positions.{name}: {mm!r} is not a number in the travel")
        if cfg.counts_per_mm is not None:
            _positive(cfg, ("counts_per_mm",), "stage")
        if cfg.status_query not in ("update", "bits"):
            raise ValueError("stage.status_query: 'update' or 'bits'")
        _positive(
            cfg,
            ("timeout", "move_timeout", "home_timeout", "tolerance", "poll_interval"),
            "stage",
        )
        return cfg

    def apt(self) -> APTConfig:
        return APTConfig(
            counts_per_mm=self.counts_per_mm or 1.0,
            travel=self.travel,
            channel=self.channel,
            dest=self.dest,
            status_query=self.status_query,
            timeout=self.timeout,
            move_timeout=self.move_timeout,
            home_timeout=self.home_timeout,
            tolerance=self.tolerance,
            poll_interval=self.poll_interval,
            start_grace=self.start_grace,
            status_max_age=self.status_max_age,
        )


@dataclass
class BacklightConfig:
    led_port: PortConfig
    servo_port: PortConfig
    servo_id: int = 1
    #: Arm angles (legacy -45 deg in the beam, -90 deg out of it).
    in_deg: float = -45.0
    out_deg: float = -90.0
    #: The LED's failsafe: off (and the arm out) this long after the last
    #: ``CalibratorOn``.
    max_on: float = 60.0
    #: Bound on an arm move; arrived within this many counts of the goal.
    move_timeout: float = 5.0
    tolerance: int = 25
    servo_timeout: float = 0.5
    #: Turn the LED off on the first connect (a known, cool state).
    led_off_on_connect: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacklightConfig:
        d = _fields(cls, d, "backlight")
        d["led_port"] = PortConfig.from_dict(
            d.get("led_port", "sim"), baud=LED_BAUD, what="backlight.led"
        )
        d["servo_port"] = PortConfig.from_dict(
            d.get("servo_port", "sim"), baud=dx.BAUD, what="backlight.servo"
        )
        cfg = cls(**d)
        _positive(cfg, ("max_on", "move_timeout", "tolerance", "servo_timeout"), "backlight")
        return cfg


@dataclass
class TelemetryConfig:
    #: The vacuum gauge controller (read only); None: not read.
    gauges: PortConfig | None = None
    #: Its two-character address (two spaces: none, as legacy).
    gauge_address: str = "  "
    #: The chiller (read only); None: not read.
    chiller: PortConfig | None = None
    #: Readings are reused for this long (s).
    max_age: float = 2.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TelemetryConfig:
        d = _fields(cls, d, "telemetry")
        if d.get("gauges") is not None:
            d["gauges"] = PortConfig.from_dict(d["gauges"], baud=GAUGE_BAUD, what="gauges")
        if d.get("chiller") is not None:
            d["chiller"] = PortConfig.from_dict(d["chiller"], baud=CHILLER_BAUD, what="chiller")
        cfg = cls(**d)
        if cfg.gauges is None and cfg.chiller is None:
            raise ValueError("telemetry: name gauges and/or chiller")
        return cfg


@dataclass
class KiwiSpecAuxConfig:
    name: str = "kiwispec"
    listen: str = "127.0.0.1:11170"
    #: Must be true (and ``--hardware`` given) for a port that is not a
    #: simulator (module doc).
    allow_hardware: bool = False
    heater: HeaterConfig | None = None
    stage: StageConfig | None = None
    backlight: BacklightConfig | None = None
    telemetry: TelemetryConfig | None = None
    request_timeout: float = 4.5
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> KiwiSpecAuxConfig:
        d = _fields(cls, d, "kiwispec_aux")
        parsers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "heater": HeaterConfig.from_dict,
            "stage": StageConfig.from_dict,
            "backlight": BacklightConfig.from_dict,
            "telemetry": TelemetryConfig.from_dict,
        }
        for key, parse in parsers.items():
            if d.get(key) is not None:
                if not isinstance(d[key], dict):
                    raise ValueError(f"{key}: an object")
                d[key] = parse(d[key])
        cfg = cls(**d)
        if not any((cfg.heater, cfg.stage, cfg.backlight, cfg.telemetry)):
            raise ValueError("configure at least one of heater, stage, backlight, telemetry")
        _positive(cfg, ("request_timeout",), "kiwispec_aux")
        return cfg

    def ports(self) -> list[tuple[str, PortConfig]]:
        out: list[tuple[str, PortConfig]] = []
        if self.heater:
            out.append(("heater", self.heater.port))
        if self.stage:
            out.append(("stage", self.stage.port))
        if self.backlight:
            out += [("backlight LED", self.backlight.led_port)]
            out += [("backlight servo", self.backlight.servo_port)]
        if self.telemetry:
            if self.telemetry.gauges:
                out.append(("vacuum gauges", self.telemetry.gauges))
            if self.telemetry.chiller:
                out.append(("chiller", self.telemetry.chiller))
        return out


# -- switches -----------------------------------------------------------------------


@dataclass
class SwitchSpec:
    name: str
    description: str
    minimum: float
    maximum: float
    step: float
    writable: bool = False


class Heater:
    """The iodine cell heater as four switches (module doc)."""

    def __init__(self, tc: TC200, cfg: HeaterConfig) -> None:
        self.tc = tc
        self.cfg = cfg
        self.idn: str | None = None
        self.switches = [
            SwitchSpec("heater_enabled", "Iodine cell heater output (on/off)", 0.0, 1.0, 1.0, True),
            SwitchSpec(
                "heater_setpoint",
                "Iodine cell heater set point (C)",
                cfg.setpoint_min,
                cfg.setpoint_max,
                0.1,
                True,
            ),
            SwitchSpec("cell_temperature", "Iodine cell temperature (C)", -50.0, 250.0, 0.1),
            SwitchSpec("heater_alarm", "Heater sensor alarm (1: alarm)", 0.0, 1.0, 1.0),
        ]

    async def connect(self) -> None:
        await self.tc.status()
        if self.idn is None:
            with contextlib.suppress(PortError):
                self.idn = await self.tc.identify()

    async def value(self, i: int) -> float:
        if i == 0:
            return 1.0 if (await self.tc.status()).enabled else 0.0
        if i == 1:
            return await self.tc.setpoint()
        if i == 2:
            return await self.tc.temperature()
        return 1.0 if (await self.tc.status()).alarm else 0.0

    async def set(self, i: int, value: float) -> None:
        if i == 0:
            await self.tc.set_enabled(value >= 0.5)
        elif i == 1:
            await self.tc.set_setpoint(value)
        else:
            raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} is read-only")

    async def status(self) -> dict[str, Any]:
        s = await self.tc.status()
        return {
            "enabled": s.enabled,
            "alarm": s.alarm,
            "unit": s.unit,
            "status": f"0x{s.raw:02x}",
            "setpoint": await self.tc.setpoint(),
            "temperature": await self.tc.temperature(),
            "controller": self.idn,
        }


class Telemetry:
    """Vacuum gauges and chiller, read only, cached (module doc)."""

    def __init__(
        self, gauges: GaugeController | None, chiller: OasisChiller | None, max_age: float
    ) -> None:
        self.gauges = gauges
        self.chiller = chiller
        self.max_age = max_age
        self._cache: dict[str, tuple[float, Any]] = {}
        self.switches: list[SwitchSpec] = []
        self._read: list[Callable[[], Awaitable[float]]] = []
        if gauges is not None:
            self._add(
                SwitchSpec("spec_pressure", "Spectrograph pressure (mbar), CG2", 0.0, 2000.0, 1e-6),
                lambda: self._gauge(2),
            )
            self._add(
                SwitchSpec("pump_pressure", "Pump line pressure (mbar), CG1", 0.0, 2000.0, 1e-6),
                lambda: self._gauge(1),
            )
        if chiller is not None:
            for key, name, desc, lo, hi, step in (
                ("temp", "chiller_temperature", "Chiller coolant temperature (C)", -50, 100, 0.1),
                ("setpoint", "chiller_setpoint", "Chiller set point (C)", -50, 100, 0.1),
                ("pump_temp", "chiller_pump_temperature", "Chiller pump temp (C)", -50, 150, 0.1),
            ):
                self._add(SwitchSpec(name, desc, lo, hi, step), self._chill(key))
            self._add(
                SwitchSpec("chiller_running", "Chiller running (status bit)", 0, 1, 1),
                self._chill_flag(lambda r: r["status"]["run"]),
            )
            self._add(
                SwitchSpec("chiller_fault", "Chiller fault or alarm (1: fault)", 0, 1, 1),
                self._chill_flag(lambda r: bool(r["fault"])),
            )

    def _add(self, spec: SwitchSpec, read: Callable[[], Awaitable[float]]) -> None:
        self.switches.append(spec)
        self._read.append(read)

    async def _cached(self, key: str, read: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] <= self.max_age:
            return hit[1]
        value = await read()
        self._cache[key] = (time.monotonic(), value)
        return value

    async def _gauge(self, n: int) -> float:
        assert self.gauges is not None
        gauges = self.gauges
        return await self._cached(f"gauge{n}", lambda: gauges.pressure(n))

    async def chiller_reading(self) -> dict[str, Any]:
        assert self.chiller is not None
        return await self._cached("chiller", self.chiller.read)

    def _chill(self, key: str) -> Callable[[], Awaitable[float]]:
        async def read() -> float:
            return float((await self.chiller_reading())[key])

        return read

    def _chill_flag(self, pick: Callable[[dict[str, Any]], bool]) -> Callable[[], Awaitable[float]]:
        async def read() -> float:
            return 1.0 if pick(await self.chiller_reading()) else 0.0

        return read

    async def connect(self) -> None:
        return None  # each reading reports its own error

    async def value(self, i: int) -> float:
        return await self._read[i]()

    async def set(self, i: int, value: float) -> None:
        raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} is read-only (telemetry)")

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.gauges is not None:
            for n, key in ((2, "spec_pressure"), (1, "pump_pressure")):
                try:
                    out[key] = await self._gauge(n)
                except PortError as exc:
                    out[key] = None
                    out[f"{key}_error"] = str(exc)
        if self.chiller is not None:
            try:
                out["chiller"] = await self.chiller_reading()
            except PortError as exc:
                out["chiller"] = None
                out["chiller_error"] = str(exc)
        return out


# -- the stage ------------------------------------------------------------------------


class Stage:
    """The iodine stage by named position (module doc)."""

    def __init__(self, apt_stage: APTStage, cfg: StageConfig) -> None:
        self.apt = apt_stage
        self.cfg = cfg

    def _scale(self) -> None:
        if self.cfg.counts_per_mm is None:
            raise AlpacaException(
                INVALID_OPERATION,
                "stage.counts_per_mm is not configured (confirm it on the hardware:"
                " docs/kiwispec_aux.md); positions and moves are refused",
            )

    async def connect(self) -> None:
        await self.apt.connect()

    def name_at(self, mm: float) -> str | None:
        for name, pos in self.cfg.positions.items():
            if pos is not None and abs(mm - pos) <= self.cfg.tolerance:
                return name
        return None

    async def wheel_position(self) -> int:
        self._scale()
        if await self.apt.moving():
            return -1
        snap = await self.apt.snapshot()
        if not snap.homed:
            raise AlpacaException(INVALID_OPERATION, "stage not homed: Action('Home') first")
        mm = self.apt.mm(snap.counts)
        name = self.name_at(mm)
        if name is None:
            extra = f" (last move: {self.apt.fault})" if self.apt.fault else ""
            raise AlpacaException(
                INVALID_OPERATION, f"stage at {mm:.3f} mm, not at a named position{extra}"
            )
        return list(self.cfg.positions).index(name)

    async def move_mm(self, mm: float) -> None:
        self._scale()
        try:
            await self.apt.move_mm(mm)
        except ValueError as exc:
            raise AlpacaException(INVALID_VALUE, str(exc)) from None
        except RuntimeError as exc:
            raise AlpacaException(INVALID_OPERATION, str(exc)) from None

    async def move_to(self, index: int) -> None:
        name = list(self.cfg.positions)[index]
        mm = self.cfg.positions[name]
        if mm is None:
            raise AlpacaException(
                INVALID_OPERATION, f"position {name!r} is not configured (confirm its mm value)"
            )
        await self.move_mm(mm)

    async def home(self) -> str:
        try:
            await self.apt.home()
        except RuntimeError as exc:
            raise AlpacaException(INVALID_OPERATION, str(exc)) from None
        return "homing"

    async def status(self) -> dict[str, Any]:
        s = await self.apt.status()
        if self.cfg.counts_per_mm is None:
            s["position_mm"] = None
            s["name"] = None
            s["counts_per_mm"] = None
        else:
            s["name"] = None if s["moving"] else self.name_at(s["position_mm"])
            s["counts_per_mm"] = self.cfg.counts_per_mm
        s["positions"] = dict(self.cfg.positions)
        return s


# -- the backlight ------------------------------------------------------------------------


class Backlight:
    """The arm (cover) and the LED (calibrator), with the LED's failsafe."""

    def __init__(self, led: BacklightLED, servo: dx.XMServo, cfg: BacklightConfig) -> None:
        self.led = led
        self.servo = servo
        self.cfg = cfg
        self.in_counts = dx.degrees_to_counts(cfg.in_deg)
        self.out_counts = dx.degrees_to_counts(cfg.out_deg)
        self.cover = COVER_UNKNOWN
        self.cover_error: str | None = None
        self.brightness = 0
        self.calibrator = CAL_UNKNOWN
        self.on_requested: float | None = None
        self._connected = False
        self._move: asyncio.Task | None = None
        self._failsafe: asyncio.Task | None = None
        self.failsafe_events: list[float] = []

    def start(self) -> None:
        self._failsafe = asyncio.ensure_future(self._run_failsafe())

    async def shutdown(self) -> None:
        for task in (self._failsafe, self._move):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def connect(self) -> None:
        if self._connected:
            return
        if self.cfg.led_off_on_connect:
            await self.led.set(False)
            self.calibrator, self.brightness = CAL_OFF, 0
        try:
            await self.servo.ping()
            self.cover = self._state_at(await self.servo.position())
            self.cover_error = None
        except PortError as exc:
            self.cover, self.cover_error = COVER_ERROR, str(exc)
            log.error("backlight arm: %s", exc)
        self._connected = True

    def _state_at(self, counts: int) -> int:
        if abs(counts - self.in_counts) <= self.cfg.tolerance:
            return COVER_CLOSED
        if abs(counts - self.out_counts) <= self.cfg.tolerance:
            return COVER_OPEN
        return COVER_UNKNOWN

    # -- the arm ------------------------------------------------------------------

    def move_cover(self, closed: bool) -> None:
        if self._move is not None and not self._move.done():
            raise AlpacaException(INVALID_OPERATION, "the backlight arm is already moving")
        self.cover = COVER_MOVING
        self._move = asyncio.ensure_future(self._arm(closed))

    async def _arm(self, closed: bool) -> None:
        goal = self.in_counts if closed else self.out_counts
        try:
            async with asyncio.timeout(self.cfg.move_timeout):
                # Legacy's sequence: torque off, position mode, torque on, goal.
                await self.servo.torque(False)
                await self.servo.position_mode()
                await self.servo.torque(True)
                await self.servo.goal(goal)
                while True:
                    pos = await self.servo.position()
                    if abs(pos - goal) <= self.cfg.tolerance and not await self.servo.moving():
                        break
                    await asyncio.sleep(0.05)
                if not closed:
                    await asyncio.sleep(0.25)
                    await self.servo.torque(False)
            self.cover = COVER_CLOSED if closed else COVER_OPEN
            self.cover_error = None
            log.info("backlight arm %s", "in the beam" if closed else "out of the beam")
        except (PortError, TimeoutError) as exc:
            why = str(exc) or f"the arm did not arrive within {self.cfg.move_timeout:g} s"
            self.cover, self.cover_error = COVER_ERROR, why
            log.error("backlight arm: %s", why)

    async def wait_cover(self, timeout: float) -> None:
        if self._move is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._move), timeout)

    def cover_state(self) -> int:
        return self.cover

    # -- the LED --------------------------------------------------------------------

    async def calibrator_on(self, brightness: int) -> None:
        await self.led.set(brightness > 0)
        self.brightness = brightness
        self.calibrator = CAL_READY
        self.on_requested = time.monotonic() if brightness > 0 else None

    async def calibrator_off(self) -> None:
        await self.led.set(False)
        self.brightness = 0
        self.calibrator = CAL_OFF
        self.on_requested = None

    async def _run_failsafe(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            t = self.on_requested
            if t is None or time.monotonic() - t <= self.cfg.max_on:
                continue
            log.error(
                "backlight failsafe: LED on for %.0f s (limit %.0f s); turning it off and"
                " swinging the arm out",
                time.monotonic() - t,
                self.cfg.max_on,
            )
            self.failsafe_events.append(time.time())
            try:
                await self.calibrator_off()
            except PortError as exc:
                log.error("backlight failsafe: LED off failed: %s", exc)
                continue  # try again next tick
            if self.cover != COVER_ERROR and (self._move is None or self._move.done()):
                self.move_cover(False)

    async def status(self) -> dict[str, Any]:
        on_for = None if self.on_requested is None else time.monotonic() - self.on_requested
        return {
            "led_on": self.led.on,
            "led_on_for": None if on_for is None else round(on_for, 1),
            "max_on": self.cfg.max_on,
            "cover_state": self.cover,
            "cover_error": self.cover_error,
            "arm_counts": {"in": self.in_counts, "out": self.out_counts},
            "failsafe_events": self.failsafe_events[-5:],
        }


# -- Alpaca over HTTP ------------------------------------------------------------------


class KiwiSpecAuxServer(ThreadingHTTPServer):
    """The Alpaca server (module doc): the HTTP handler is the TCSGO
    server's; the devices run on a private event loop thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: KiwiSpecAuxConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        self.config = config
        self._ports: list[Port] = []
        sims = {
            "heater": TC200Sim,
            "stage": APTSim,
            "backlight LED": LEDSim,
            "backlight servo": XMServoSim,
            "vacuum gauges": GaugeSim,
            "chiller": OasisSim,
        }
        opened: dict[str, Port] = {}
        try:
            for what, pc in config.ports():
                opened[what] = open_port(pc, sims[what], allow_hardware=allow, what=what)
        except BaseException:
            for p in opened.values():
                p.close()
            raise
        self._ports = list(opened.values())
        host, _, port = config.listen.rpartition(":")
        try:
            super().__init__((host, int(port)), _Handler)  # type: ignore[arg-type]
        except BaseException:
            for p in self._ports:
                p.close()
            raise
        self.opened = opened
        # Every port simulated: this server is a simulator too, and says so
        # (a KiwiSpec service's hardware gate then accepts it on loopback).
        self.registration = (
            hwgate.SimulatorRegistration(self.server_address[1])
            if all(pc.is_sim for _, pc in config.ports())
            else None
        )
        self.devices: tuple[str, ...] = ()
        self.switch_owner: dict[str, Heater | Telemetry] = {}
        nswitch = 0
        for kind in ("heater", "stage", "backlight", "telemetry"):
            if getattr(config, kind) is None:
                continue
            if kind in ("heater", "telemetry"):
                dev = "switch" if nswitch == 0 else f"switch/{nswitch}"
                nswitch += 1
            else:
                dev = {"stage": "filterwheel", "backlight": "covercalibrator"}[kind]
            self.devices += (dev,)
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[tuple[str, int]] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="kiwispec-aux-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self._stopped = False
        self.heater: Heater | None = None
        self.telemetry: Telemetry | None = None
        self.stage: Stage | None = None
        self.backlight: Backlight | None = None
        self._run_sync(self._build())

    def sim(self, what: str) -> Any:
        """The in-process simulator behind ``what`` (tests)."""
        return getattr(self.opened[what], "sim", None)

    async def _build(self) -> None:
        cfg, op = self.config, self.opened
        devs = list(self.devices)
        if cfg.heater is not None:
            self.heater = Heater(TC200(op["heater"], timeout=cfg.heater.timeout), cfg.heater)
            self.switch_owner[devs.pop(0)] = self.heater
        if cfg.stage is not None:
            self.stage = Stage(APTStage(op["stage"], cfg.stage.apt()), cfg.stage)
            self.stage.apt.start()
            devs.pop(0)
        if cfg.backlight is not None:
            b = cfg.backlight
            servo = dx.XMServo(op["backlight servo"], b.servo_id, timeout=b.servo_timeout)
            self.backlight = Backlight(BacklightLED(op["backlight LED"]), servo, b)
            self.backlight.start()
            devs.pop(0)
        if cfg.telemetry is not None:
            t = cfg.telemetry
            gauges = (
                GaugeController(op["vacuum gauges"], address=t.gauge_address)
                if t.gauges is not None
                else None
            )
            chiller = OasisChiller(op["chiller"]) if t.chiller is not None else None
            self.telemetry = Telemetry(gauges, chiller, t.max_age)
            self.switch_owner[devs.pop(0)] = self.telemetry

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_thread.is_alive():
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)  # type: ignore[arg-type]

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def _call(self, fn: Callable[[], Awaitable[Any]], what: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AlpacaException(
                TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except PortTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(TIMEOUT, str(exc)) from None
        except PortError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management --------------------------------------------------------------------

    def _describe(self, device: str) -> tuple[str, str]:
        if device.startswith("switch"):
            owner = self.switch_owner[device]
            what = "iodine cell heater" if isinstance(owner, Heater) else "telemetry"
            return "Switch", what
        return {
            "filterwheel": ("FilterWheel", "iodine stage"),
            "covercalibrator": ("CoverCalibrator", "fiber backlight"),
        }[device]

    def management(self, tail: list[str]) -> Any:
        name = self.config.name
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast KiwiSpec auxiliary devices ({name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": name,
            }
        if tail == ["v1", "configureddevices"]:
            out = []
            for d in self.devices:
                kind, what = self._describe(d)
                number = int(d.split("/")[1]) if "/" in d else 0
                out.append(
                    {
                        "DeviceName": f"{name} {what}",
                        "DeviceType": kind,
                        "DeviceNumber": number,
                        "UniqueID": f"robofast-kiwispec-aux-{name}-{kind.lower()}-{number}",
                    }
                )
            return out
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>KiwiSpec aux</title>"
            f"</head><body><h1>robofast KiwiSpec auxiliary devices: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- members -----------------------------------------------------------------------

    def connected(self, device: str, client: int) -> bool:
        with self._clients_lock:
            return (device, client) in self._clients

    def _owner(self, device: str) -> Any:
        if device.startswith("switch"):
            return self.switch_owner[device]
        return self.stage if device == "filterwheel" else self.backlight

    def _set_connected(self, device: str, client: int, value: bool) -> None:
        if value:
            if not self.connected(device, client):
                self._call(self._owner(device).connect, "Connect")
                with self._clients_lock:
                    self._clients.add((device, client))
        else:
            with self._clients_lock:
                self._clients.discard((device, client))

    def _common(self, device: str, member: str, client: int) -> Any:
        kind, what = self._describe(device)
        return {
            "connected": self.connected(device, client),
            "connecting": False,
            "description": f"KiwiSpec {what} ({self.config.name})",
            "driverinfo": "robofast_core.devices.kiwispec_aux_server: KiwiSpec auxiliary devices"
            " over serial URLs",
            "driverversion": DRIVER_VERSION,
            "interfaceversion": INTERFACE[kind.lower()],
            "name": f"robofast KiwiSpec {what}",
            "supportedactions": ["Status", "Home", "MoveMM", "Stop"]
            if device == "filterwheel"
            else ["Status"],
        }[member]

    def member(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        if verb == "PUT":
            if member == "connected":
                self._set_connected(device, client, _bool_param(params, "Connected"))
                return None
            if member in ("connect", "disconnect"):
                self._set_connected(device, client, member == "connect")
                return None
        elif member in COMMON_NAMES and member not in ("connect", "disconnect"):
            return self._common(device, member, client)
        if member in COMMON_NAMES:
            raise _BadRequest(f"{verb} {member} is not allowed")
        if member in ("connect", "disconnect", "action") and verb == "GET":
            raise _BadRequest(f"{member} is a PUT method")
        if member in ("commandblind", "commandbool", "commandstring"):
            if verb != "PUT":
                raise _BadRequest(f"{member} is a PUT method")
            self._need(device, member, client)
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        if device.startswith("switch"):
            return self._switch(device, verb, member, params, client)
        if device == "filterwheel":
            return self._wheel(verb, member, params, client)
        return self._covercal(verb, member, params, client)

    def _need(self, device: str, member: str, client: int) -> None:
        if not self.connected(device, client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")

    @staticmethod
    def _unknown(verb: str, member: str) -> Any:
        raise _BadRequest(f"{verb} {member}: no such member")

    def _action(self, device: str, params: dict[str, str]) -> Any:
        name = params.get("Action", "").lower()
        owner = self._owner(device)
        if name == "status":
            return json.dumps(self._call(owner.status, "Status"))
        if device == "filterwheel":
            stage = self.stage
            assert stage is not None
            if name == "home":
                return self._call(stage.home, "Home")
            if name == "movemm":
                try:
                    mm = float(params.get("Parameters", ""))
                except ValueError:
                    raise AlpacaException(
                        INVALID_VALUE, "MoveMM: Parameters must be a number of mm"
                    ) from None
                if not math.isfinite(mm):
                    raise AlpacaException(INVALID_VALUE, "MoveMM: not a finite number")
                self._call(lambda: stage.move_mm(mm), "MoveMM")
                return f"moving to {mm:g} mm"
            if name == "stop":
                self._call(stage.apt.stop, "Stop")
                return "stopped"
        raise AlpacaException(
            ACTION_NOT_IMPLEMENTED, f"action {params.get('Action', '')!r} is not supported"
        )

    # -- switch ---------------------------------------------------------------------

    def _switch(self, device: str, verb: str, member: str, params: dict[str, str], client: int):
        owner = self.switch_owner[device]
        specs = owner.switches
        if member == "maxswitch" and verb == "GET":
            self._need(device, member, client)
            return len(specs)
        self._need(device, member, client)
        if member == "action":
            if verb != "PUT":
                raise _BadRequest("action is a PUT method")
            return self._action(device, params)
        key = "id" if verb == "GET" else "Id"
        if member in (
            "canwrite",
            "getswitch",
            "getswitchdescription",
            "getswitchname",
            "getswitchvalue",
            "minswitchvalue",
            "maxswitchvalue",
            "switchstep",
            "setswitch",
            "setswitchname",
            "setswitchvalue",
        ):
            if key not in params:
                raise _BadRequest("missing parameter Id")
            try:
                i = int(params[key])
            except ValueError:
                raise _BadRequest(f"Id={params[key]!r} is not an integer") from None
            if not 0 <= i < len(specs):
                raise AlpacaException(INVALID_VALUE, f"no switch {i}")
            spec = specs[i]
        else:
            return self._unknown(verb, member)
        if verb == "GET":
            if member == "canwrite":
                return spec.writable
            if member == "getswitchname":
                return spec.name
            if member == "getswitchdescription":
                return spec.description
            if member == "minswitchvalue":
                return spec.minimum
            if member == "maxswitchvalue":
                return spec.maximum
            if member == "switchstep":
                return spec.step
            if member in ("getswitch", "getswitchvalue"):
                v = self._call(lambda: owner.value(i), spec.name)
                return v if member == "getswitchvalue" else v > spec.minimum
            return self._unknown(verb, member)
        if member == "setswitchname":
            raise AlpacaException(NOT_IMPLEMENTED, "switch names come from the driver")
        if not spec.writable:
            raise AlpacaException(NOT_IMPLEMENTED, f"switch {i} ({spec.name}) is read-only")
        if member == "setswitch":
            value = spec.maximum if _bool_param(params, "State") else spec.minimum
        elif member == "setswitchvalue":
            value = _float_param(params, "Value")
            if not spec.minimum <= value <= spec.maximum:
                raise AlpacaException(
                    INVALID_VALUE,
                    f"switch {i} ({spec.name}): {value} outside {spec.minimum}..{spec.maximum}",
                )
        else:
            return self._unknown(verb, member)
        log.warning("switch %s %d (%s) := %s by client %d", device, i, spec.name, value, client)
        self._call(lambda: owner.set(i, value), spec.name)
        return None

    # -- filter wheel -----------------------------------------------------------------

    def _wheel(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        stage = self.stage
        assert stage is not None
        names = list(stage.cfg.positions)
        self._need("filterwheel", member, client)
        if verb == "GET":
            if member == "names":
                return names
            if member == "focusoffsets":
                return [0] * len(names)
            if member == "position":
                return self._call(stage.wheel_position, "Position")
            if member == "devicestate":
                state: list[dict[str, Any]] = []
                with contextlib.suppress(AlpacaException):
                    pos = self._call(stage.wheel_position, "Position")
                    state.append({"Name": "Position", "Value": pos})
                state.append({"Name": "TimeStamp", "Value": _now_stamp()})
                return state
            return self._unknown(verb, member)
        if member == "position":
            n = _float_param(params, "Position")
            if not n.is_integer():
                raise _BadRequest(f"Position={params['Position']!r} is not an integer")
            if not 0 <= n < len(names):
                raise AlpacaException(INVALID_VALUE, f"Position {n:g} outside 0..{len(names) - 1}")
            return self._call(lambda: stage.move_to(int(n)), "Position")
        if member == "action":
            return self._action("filterwheel", params)
        return self._unknown(verb, member)

    # -- cover calibrator ---------------------------------------------------------------

    def _covercal(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        bl = self.backlight
        assert bl is not None
        self._need("covercalibrator", member, client)
        if verb == "GET":
            if member == "maxbrightness":
                return 1
            if member == "brightness":
                return bl.brightness
            if member == "calibratorstate":
                return bl.calibrator
            if member == "coverstate":
                return bl.cover_state()
            if member == "calibratorchanging":
                return False
            if member == "covermoving":
                return bl.cover_state() == COVER_MOVING
            if member == "devicestate":
                return [
                    {"Name": "Brightness", "Value": bl.brightness},
                    {"Name": "CalibratorChanging", "Value": False},
                    {"Name": "CalibratorState", "Value": bl.calibrator},
                    {"Name": "CoverMoving", "Value": bl.cover_state() == COVER_MOVING},
                    {"Name": "CoverState", "Value": bl.cover_state()},
                    {"Name": "TimeStamp", "Value": _now_stamp()},
                ]
            return self._unknown(verb, member)
        if member == "calibratoron":
            b = _float_param(params, "Brightness")
            if not b.is_integer() or not 0 <= b <= 1:
                raise AlpacaException(INVALID_VALUE, f"Brightness {b:g} outside 0..1")
            log.warning("backlight LED on (brightness %d) by client %d", int(b), client)
            return self._call(lambda: bl.calibrator_on(int(b)), "CalibratorOn")
        if member == "calibratoroff":
            return self._call(bl.calibrator_off, "CalibratorOff")
        if member in ("opencover", "closecover"):
            closed = member == "closecover"
            log.warning(
                "backlight arm %s by client %d", "IN (over the fiber)" if closed else "out", client
            )
            return self._call(lambda: self._start_cover(closed), member)
        if member == "haltcover":
            raise AlpacaException(NOT_IMPLEMENTED, "the backlight arm cannot be halted")
        if member == "action":
            return self._action("covercalibrator", params)
        return self._unknown(verb, member)

    async def _start_cover(self, closed: bool) -> None:
        bl = self.backlight
        assert bl is not None
        if bl.cover == COVER_ERROR:
            # A servo that failed before must answer now, or the command
            # fails at once with the reason (it does not answer PINGs today).
            await bl.servo.ping()
        bl.move_cover(closed)

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> KiwiSpecAuxServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="kiwispec-aux-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        """Stop serving. Sends nothing (a stage move in progress completes)."""
        if self._stopped:
            return
        self._stopped = True
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)

        async def down() -> None:
            if self.stage is not None:
                await self.stage.apt.shutdown()
            if self.backlight is not None:
                await self.backlight.shutdown()

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(down(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        for p in self._ports:
            p.close()
        if self.registration is not None:
            self.registration.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow real serial ports (the config must also say allow_hardware)",
    )
    ap.add_argument("--simulate", action="store_true", help="every port on its simulator")
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_device_config(args.config, KiwiSpecAuxConfig.from_dict)
    if args.listen:
        config.listen = args.listen
    if args.simulate:
        for _, pc in config.ports():
            pc.url = "sim"
    try:
        server = KiwiSpecAuxServer(config, hardware=args.hardware)
    except (HardwareNotAllowed, PortError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    server.start()
    print(json.dumps({"url": f"http://{server.address}", "devices": server.devices}), flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
