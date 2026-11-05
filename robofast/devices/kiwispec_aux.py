"""KiwiSpec's auxiliary hardware: the chiller, and the aux devices' Alpaca server.

**The aux Alpaca server.** The iodine cell heater, the iodine stage, the
fiber backlight and the vacuum gauges can be driven by robofast's own
Alpaca server (:mod:`robofast.devices.kiwispec_aux_server`) instead
of the legacy ``spectrograph_server.py``. With ``"aux"`` in the KiwiSpec
service's config, :class:`AuxRouted` sends those devices' calls there and
everything else (camera files, headers, SI Image) to the legacy server as
before. Each device can stay on the legacy server (``"heater": null``,
``"stage": false``, ...). See :class:`AuxConfig`.

**The chiller** (a Solid State Cooling Systems Oasis 3 at MINERVA) cools
the spectrograph's camera. robofast reads it for telemetry only: it never
sets it. Its serial protocol is in legacy ``chiller.py`` (``TEMP?``,
``SETTEMP?``, ``STAT1A?``, ``FLTS1A?``); the driver is
:class:`~robofast.devices.kiwispec_serial.OasisChiller`, served by
the aux server's telemetry, and the service reads it with ``"aux":
{"chiller": true}`` (:class:`AuxChiller`). The other backends here: the
simulator (``{"kind": "sim"}``), and ``{"kind": "oasis3"}``, which names
it without reading it (each read says to use the aux server; ``/status``
shows that as a warning, not an alarm). A driver registers its ``kind`` in
:data:`CHILLERS` (a plug-in can too, docs/plugins.md) and returns a
:class:`Chiller`.

A reading is a dict: ``temp`` (C, the coolant), ``setpoint`` (C), ``flow``
(l/min, if the unit reports it), ``fault`` (the unit's own alarm text or
None). The KiwiSpec service warns when ``temp`` is outside ``setpoint +-
tolerance`` and alarms on a ``fault``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from robofast import hwgate
from robofast.alpaca_client import AlpacaDevice, AlpacaTransportError
from robofast.devices.kiwispec_client import (
    KiwiSpecError,
    KiwiSpecFailed,
    KiwiSpecTimeout,
    KiwiSpecUnreachable,
    StagePosition,
)
from robofast.owner import DeviceError, DeviceTimeout

log = logging.getLogger(__name__)


class Chiller(Protocol):
    #: Degrees C either side of the set point before a warning.
    tolerance: float

    async def read(self) -> dict[str, Any]: ...


@dataclass
class SimChiller:
    """A chiller holding its set point; ``fault`` and ``temp`` can be set by a test."""

    setpoint: float = 15.0
    temp: float = 15.0
    flow: float | None = 2.0
    fault: str | None = None
    tolerance: float = 1.0

    async def read(self) -> dict[str, Any]:
        return {
            "temp": self.temp,
            "setpoint": self.setpoint,
            "flow": self.flow,
            "fault": self.fault,
        }


@dataclass
class MissingChiller:
    """A chiller named in the config whose driver robofast does not have yet."""

    kind: str
    tolerance: float = 1.0

    async def read(self) -> dict[str, Any]:
        raise NotImplementedError(
            f"the {self.kind} chiller is read through the aux server: set aux.chiller"
            " (docs/kiwispec_aux.md)"
        )


#: kind -> factory(spec without "kind").
CHILLERS: dict[str, Callable[[dict[str, Any]], Chiller]] = {
    "sim": lambda spec: SimChiller(**spec),
    # Connection settings are ignored until there is a driver.
    "oasis3": lambda spec: MissingChiller("oasis3", float(spec.get("tolerance", 1.0))),
}


def build_chiller(spec: dict[str, Any] | None) -> Chiller | None:
    if spec is None:
        return None
    if not isinstance(spec, dict) or "kind" not in spec:
        raise ValueError(f"chiller: an object with a 'kind', not {spec!r}")
    s = {k: v for k, v in spec.items() if not k.startswith("_")}
    kind = str(s.pop("kind"))
    if kind not in CHILLERS:
        raise ValueError(f"chiller kind {kind!r}: one of {sorted(CHILLERS)}")
    return CHILLERS[kind](s)


# -- the aux devices' Alpaca server ----------------------------------------------------


@dataclass
class AuxConfig:
    """``"aux"`` in the KiwiSpec service's config."""

    #: The aux Alpaca server, ``host:port``.
    address: str
    #: Switch device numbers of the heater, the fiber backlight and the
    #: telemetry (pressures, chiller), as the aux server numbers them;
    #: None: that device stays on the legacy server.
    heater: int | None = 0
    backlight: int | None = 1
    telemetry: int | None = 2
    #: The iodine cell selector (filterwheel/0) through the aux server.
    stage: bool = True
    #: Read the chiller through the aux server's telemetry.
    chiller: bool = False
    #: Degrees C either side of the chiller's set point before a warning.
    chiller_tolerance: float = 1.0
    #: Bound on one Alpaca request.
    timeout: float = 6.0
    #: Bound on the backlight arm's swing.
    arm_timeout: float = 10.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuxConfig:
        if not isinstance(d, dict) or "address" not in d:
            raise ValueError("aux: an object with an 'address' (host:port of the aux server)")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"aux: unknown keys {sorted(set(d) - known)}")
        cfg = cls(**d)
        if cfg.chiller and cfg.telemetry is None:
            raise ValueError("aux.chiller needs aux.telemetry")
        return cfg


def _wrap(exc: BaseException, what: str) -> KiwiSpecError:
    if isinstance(exc, DeviceTimeout | TimeoutError):
        return KiwiSpecTimeout(f"aux {what}: {exc}")
    if isinstance(exc, AlpacaTransportError):
        return KiwiSpecUnreachable(f"aux {what}: {exc}")
    return KiwiSpecFailed(f"aux {what}: {exc}")


class AuxDevices:
    """The aux server's devices, as the KiwiSpec service needs them. Every
    call is bounded; every error is a :class:`KiwiSpecError`."""

    def __init__(self, cfg: AuxConfig, *, allow_hardware: bool = False) -> None:
        self.cfg = cfg
        hwgate.check_url(cfg.address, allow_hardware=allow_hardware, what="kiwispec aux server")
        a = cfg.address
        self.heater = None if cfg.heater is None else AlpacaDevice(a, "switch", cfg.heater)
        self.telemetry = None if cfg.telemetry is None else AlpacaDevice(a, "switch", cfg.telemetry)
        self.stage = AlpacaDevice(a, "filterwheel", 0) if cfg.stage else None
        self.backlight = None if cfg.backlight is None else AlpacaDevice(a, "switch", cfg.backlight)
        self._ids: dict[int, dict[str, int]] = {}
        self._names: list[str] | None = None
        self._connected: set[int] = set()

    async def _ready(self, dev: AlpacaDevice) -> None:
        if id(dev) not in self._connected:
            await dev.connect(timeout=self.cfg.timeout)
            self._connected.add(id(dev))

    async def _do(self, dev: AlpacaDevice | None, what: str, fn: Callable[[], Any]) -> Any:
        if dev is None:
            raise KiwiSpecFailed(f"aux {what}: not configured")
        try:
            async with asyncio.timeout(self.cfg.timeout * 4):
                await self._ready(dev)
                return await fn()
        except KiwiSpecError:
            raise
        except (DeviceError, TimeoutError) as exc:
            if isinstance(exc, DeviceError) and "not connected" in str(exc).lower():
                self._connected.discard(id(dev))
            raise _wrap(exc, what) from exc

    async def _switch_id(self, dev: AlpacaDevice, name: str) -> int:
        ids = self._ids.get(id(dev))
        if ids is None:
            t = self.cfg.timeout
            n = int(await dev.get("MaxSwitch", timeout=t))
            ids = {await dev.get("GetSwitchName", timeout=t, Id=i): i for i in range(n)}
            self._ids[id(dev)] = ids
        if name not in ids:
            raise KiwiSpecFailed(f"aux switch {dev.label} has no {name!r} (has {sorted(ids)})")
        return ids[name]

    async def _value(self, dev: AlpacaDevice | None, name: str) -> float:
        async def read() -> float:
            assert dev is not None
            i = await self._switch_id(dev, name)
            return float(await dev.get("GetSwitchValue", timeout=self.cfg.timeout, Id=i))

        return await self._do(dev, name, read)

    async def _set(self, dev: AlpacaDevice | None, name: str, value: float) -> None:
        async def write() -> None:
            assert dev is not None
            i = await self._switch_id(dev, name)
            await dev.put("SetSwitchValue", timeout=self.cfg.timeout, Id=i, Value=value)

        await self._do(dev, name, write)

    # -- heater -----------------------------------------------------------------------

    async def heater_temp(self) -> float:
        return await self._value(self.heater, "cell_temperature")

    async def heater_setpoint(self) -> float:
        return await self._value(self.heater, "heater_setpoint")

    async def heater_set_setpoint(self, celsius: float) -> None:
        await self._set(self.heater, "heater_setpoint", round(celsius, 1))

    async def heater_enable(self, on: bool) -> None:
        await self._set(self.heater, "heater_enabled", 1.0 if on else 0.0)

    # -- pressures ----------------------------------------------------------------------

    async def spec_pressure(self) -> float:
        return await self._value(self.telemetry, "spec_pressure")

    async def pump_pressure(self) -> float:
        return await self._value(self.telemetry, "pump_pressure")

    async def chiller(self) -> dict[str, Any]:
        async def read() -> dict[str, Any]:
            assert self.telemetry is not None
            text = await self.telemetry.put(
                "Action", timeout=self.cfg.timeout, Action="Status", Parameters=""
            )
            status = json.loads(text)
            if status.get("chiller") is None:
                raise KiwiSpecFailed(
                    f"aux chiller: {status.get('chiller_error') or 'not on the aux server'}"
                )
            return status["chiller"]

        return await self._do(self.telemetry, "chiller", read)

    # -- stage -------------------------------------------------------------------------

    async def stage_status(self) -> dict[str, Any]:
        async def read() -> dict[str, Any]:
            assert self.stage is not None
            text = await self.stage.put(
                "Action", timeout=self.cfg.timeout, Action="Status", Parameters=""
            )
            return json.loads(text)

        return await self._do(self.stage, "stage status", read)

    async def stage_position(self) -> StagePosition:
        s = await self.stage_status()
        if s.get("position_mm") is None:
            raise KiwiSpecFailed("aux stage: no position (counts_per_mm not configured?)")
        name = None if s.get("moving") else s.get("name")
        return StagePosition(float(s["position_mm"]), name or "unknown")

    async def stage_move(self, position: str) -> None:
        async def move() -> None:
            assert self.stage is not None
            if self._names is None:
                self._names = list(await self.stage.get("Names", timeout=self.cfg.timeout))
            if position not in self._names:
                raise KiwiSpecFailed(f"aux stage: no position {position!r} ({self._names})")
            n = self._names.index(position)
            await self.stage.put("Position", timeout=self.cfg.timeout, Position=n)

        await self._do(self.stage, f"stage move {position}", move)

    async def stage_move_mm(self, mm: float) -> None:
        async def move() -> None:
            assert self.stage is not None
            await self.stage.put(
                "Action", timeout=self.cfg.timeout, Action="MoveMM", Parameters=f"{mm:.4f}"
            )

        await self._do(self.stage, f"stage move {mm} mm", move)

    # -- backlight ----------------------------------------------------------------------

    async def set_backlight(self, on: bool) -> None:
        """The guider's on/off (the aux server's ``backlight`` switch): arm in
        then LED on; LED off then arm out. Returns once the arm is there."""
        async with asyncio.timeout(self.cfg.arm_timeout + self.cfg.timeout * 4):
            await self._set(self.backlight, "backlight", 1.0 if on else 0.0)

    async def close(self) -> None:
        for dev in (self.heater, self.telemetry, self.stage, self.backlight):
            if dev is not None:
                await dev.aclose()


@dataclass
class AuxChiller:
    """The chiller, read through the aux server's telemetry."""

    aux: AuxDevices
    tolerance: float = 1.0

    async def read(self) -> dict[str, Any]:
        r = await self.aux.chiller()
        return {
            "temp": r.get("temp"),
            "setpoint": r.get("setpoint"),
            "flow": r.get("flow"),
            "fault": r.get("fault"),
        }


class AuxRouted:
    """The legacy :class:`~robofast.devices.kiwispec_client.SpecServerClient`
    with the aux devices' calls sent to the aux server (module doc).
    Anything not routed goes to the legacy client unchanged."""

    def __init__(self, legacy: Any, aux: AuxDevices) -> None:
        self._legacy = legacy
        self.aux = aux

    def __getattr__(self, name: str) -> Any:
        return getattr(self._legacy, name)

    async def cell_heater_temp(self) -> float:
        if self.aux.heater is None:
            return await self._legacy.cell_heater_temp()
        return await self.aux.heater_temp()

    async def cell_heater_get_set_temp(self) -> float:
        if self.aux.heater is None:
            return await self._legacy.cell_heater_get_set_temp()
        return await self.aux.heater_setpoint()

    async def cell_heater_set_temp(self, celsius: float) -> Any:
        if self.aux.heater is None:
            return await self._legacy.cell_heater_set_temp(celsius)
        await self.aux.heater_set_setpoint(celsius)

    async def cell_heater_on(self) -> None:
        if self.aux.heater is None:
            await self._legacy.cell_heater_on()
        else:
            await self.aux.heater_enable(True)

    async def cell_heater_off(self) -> None:
        if self.aux.heater is None:
            await self._legacy.cell_heater_off()
        else:
            await self.aux.heater_enable(False)

    async def spec_pressure(self) -> float:
        if self.aux.telemetry is None:
            return await self._legacy.spec_pressure()
        return await self.aux.spec_pressure()

    async def pump_pressure(self) -> float:
        if self.aux.telemetry is None:
            return await self._legacy.pump_pressure()
        return await self.aux.pump_pressure()

    async def stage_position(self) -> StagePosition:
        if self.aux.stage is None:
            return await self._legacy.stage_position()
        return await self.aux.stage_position()

    async def stage_move(self, position: str) -> None:
        if self.aux.stage is None:
            await self._legacy.stage_move(position)
        else:
            await self.aux.stage_move(position)

    async def stage_move_mm(self, mm: float) -> None:
        if self.aux.stage is None:
            await self._legacy.stage_move_mm(mm)
        else:
            await self.aux.stage_move_mm(mm)

    async def call(self, command: str, *args: Any, **kw: Any) -> Any:
        if command in ("backlight_on", "backlight_off") and self.aux.backlight is not None:
            await self.aux.set_backlight(command == "backlight_on")
            return []
        return await self._legacy.call(command, *args, **kw)
