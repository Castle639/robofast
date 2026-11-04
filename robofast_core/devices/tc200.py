"""Thorlabs TC200 temperature controller (ASCII serial): KiwiSpec's iodine cell heater.

The protocol, from legacy MINERVA's ``spectrograph_server.py`` (the
``I2Heater`` port: 115200 8N1, commands terminated by CR) and the TC200
manual:

* ``stat?`` -- the status byte, in hex: bit 0 enabled, 1 cycle mode, 2-3
  sensor type, 4-5 display unit (bit 4 set: C; else bit 5 set: F; else K),
  6 sensor alarm, 7 cycle paused.
* ``tact?`` / ``tset?`` -- the actual and set temperatures (``54.6 C``).
* ``tset=<T>`` -- the set point.
* ``ens`` -- **toggles** the output (there is no "on" or "off" command).
  So :meth:`TC200.set_enabled` always reads ``stat?`` first, toggles only
  when the state differs, then reads it again to verify, all under one lock
  (two clients cannot both toggle).

The unit echoes each command and ends its answer with a ``>`` prompt (the
legacy code just waited 1 s and parsed the first number); this driver
takes the answer up to the prompt or until the line goes quiet, drops the
echo, and parses what is left. An answer starting ``CMD_`` (the TC200's
``CMD_NOT_DEFINED`` / ``CMD_ARG_INVALID``) is an error.

Nothing else is ever sent (:data:`COMMANDS`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from robofast_core.devices.serial_url import Port, PortError, PortTimeout

log = logging.getLogger(__name__)

BAUD = 115200
#: Every command this driver sends (``tset=`` takes a value).
COMMANDS = ("stat?", "tact?", "tset?", "tset=", "ens", "*idn?")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")


class TC200Error(PortError):
    """The controller refused a command or gave an answer this driver cannot read."""


@dataclass(frozen=True)
class HeaterStatus:
    enabled: bool
    cycle_mode: bool
    unit: str
    alarm: bool
    paused: bool
    raw: int


def parse_status(value: int) -> HeaterStatus:
    unit = "C" if value & 0x10 else ("F" if value & 0x20 else "K")
    return HeaterStatus(
        enabled=bool(value & 0x01),
        cycle_mode=bool(value & 0x02),
        unit=unit,
        alarm=bool(value & 0x40),
        paused=bool(value & 0x80),
        raw=value,
    )


class TC200:
    """The driver. One exchange at a time; every exchange bounded."""

    def __init__(self, port: Port, *, timeout: float = 2.0, quiet: float = 0.2) -> None:
        self.port = port
        self.timeout = timeout
        self.quiet = quiet
        self._lock = asyncio.Lock()
        self._toggle_lock = asyncio.Lock()

    async def _ask(self, command: str) -> str:
        if not any(command == c or (c.endswith("=") and command.startswith(c)) for c in COMMANDS):
            raise TC200Error(f"refusing to send {command!r}")
        async with self._lock:
            self.port.discard()
            await self.port.write(command.encode("ascii") + b"\r")
            raw = await self.port.drain(self.quiet, self.timeout, end=b">")
        if not raw:
            raise PortTimeout(f"{self.port.name}: no answer to {command!r} within {self.timeout} s")
        text = raw.decode("ascii", "replace").replace("\n", "\r")
        lines = [ln.strip() for ln in text.split("\r")]
        lines = [ln.rstrip(">").strip() for ln in lines]
        lines = [ln for ln in lines if ln and ln != command]
        answer = " ".join(lines)
        if answer.upper().startswith("CMD_"):
            raise TC200Error(f"{command!r}: the controller answered {answer!r}")
        return answer

    async def _number(self, command: str) -> float:
        answer = await self._ask(command)
        m = _NUMBER.search(answer)
        if m is None:
            raise TC200Error(f"{command!r}: no number in {answer!r}")
        return float(m.group())

    async def identify(self) -> str:
        return await self._ask("*idn?")

    async def status(self) -> HeaterStatus:
        answer = await self._ask("stat?")
        token = answer.split()[0] if answer.split() else ""
        token = token[2:] if token.lower().startswith("0x") else token
        try:
            return parse_status(int(token, 16))
        except ValueError:
            raise TC200Error(f"'stat?': not a hex status: {answer!r}") from None

    async def temperature(self) -> float:
        return await self._number("tact?")

    async def setpoint(self) -> float:
        return await self._number("tset?")

    async def set_setpoint(self, celsius: float) -> float:
        """Set and verify (read back within 0.05 C); returns the read-back."""
        await self._ask(f"tset={celsius:.1f}")
        got = await self.setpoint()
        if abs(got - round(celsius, 1)) > 0.05:
            raise TC200Error(f"set point {celsius:.1f} C not taken: the controller has {got} C")
        log.info("iodine heater set point %.1f C", got)
        return got

    async def set_enabled(self, on: bool) -> HeaterStatus:
        """Turn the output on or off: read, toggle only if needed, verify."""
        async with self._toggle_lock:
            before = await self.status()
            if before.enabled == on:
                return before
            log.warning("iodine heater output %s (ens toggle)", "ON" if on else "OFF")
            await self._ask("ens")
            after = await self.status()
            if after.enabled != on:
                raise TC200Error(
                    f"the 'ens' toggle did not turn the output {'on' if on else 'off'}"
                    f" (status 0x{after.raw:02x}); not toggling again"
                )
            return after
