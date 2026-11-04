"""KiwiSpec's small ASCII serial devices: the backlight LED, the vacuum gauges, the chiller.

All three protocols come from legacy MINERVA's code on the spectrograph PC
(``spectrograph_server.py``, ``com.py``, ``com.ini``, ``chiller.py``);
none has been checked against the hardware by robofast yet
(``docs/kiwispec_aux.md`` lists what to confirm). Each driver has an
allowlist and sends nothing else.

* **Backlight LED controller** (its own FTDI port; legacy ``backlight``,
  9600 8N1, CR): ``n8`` turns the LED on, ``f8`` off. Legacy ignored the
  answer; so does this driver (it reads and logs whatever comes back). The
  LED's state cannot be read back.
* **Vacuum gauge controller** (legacy ``gaugeController``, 19200 8N1, CR):
  an InstruTech / Granville-Phillips-style controller with two convection
  gauges, queried ``#  RDCG1`` (CG1, the pump line) and ``#  RDCG2`` (CG2,
  the spectrograph); ``#`` + a two-character address (two spaces: none) +
  the command. A good answer is ``*`` + the address + the value (``*
  1.23E-03``); an error starts with ``?``. Values are taken as mbar, as
  legacy did. **Read only**: this driver can send exactly those two
  queries. robofast never controls the vacuum pump, the pump valve or the
  vent valve, or their power.
* **Chiller** (Solid State Cooling Systems Oasis; legacy ``chiller.py``,
  9600 8N1, CR LF): ``TEMP?`` (coolant, C), ``SETTEMP?``, ``PUMPTEMP?``,
  ``STAT1A?`` (status bits: run, remote lock, ready, below / above set
  point, TEC heating, general warning, general alarm, autotune) and
  ``FLTS1A?`` (fault bits: RTD, tank low, pump fail, fan fail); each answer
  a bare number. **Read only**: the set-point, run and stop commands the
  unit has are never sent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, ClassVar

from robofast_core.devices.serial_url import Port, PortError, PortTimeout

log = logging.getLogger(__name__)

LED_BAUD = 9600
GAUGE_BAUD = 19200
CHILLER_BAUD = 9600
_FLOAT = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class SerialDeviceError(PortError):
    """A device refused a command, or answered something unreadable."""


class _Ascii:
    """One command at a time, each bounded."""

    allowed: frozenset[str] = frozenset()
    terminator = b"\r"

    def __init__(self, port: Port, *, timeout: float = 1.5, quiet: float = 0.15) -> None:
        self.port = port
        self.timeout = timeout
        self.quiet = quiet
        self._lock = asyncio.Lock()

    async def ask(self, command: str) -> str:
        if command not in self.allowed:
            raise SerialDeviceError(f"refusing to send {command!r} to {self.port.name}")
        async with self._lock:
            self.port.discard()
            await self.port.write(command.encode("ascii") + self.terminator)
            raw = await self.port.drain(self.quiet, self.timeout)
        if not raw.strip():
            raise PortTimeout(f"{self.port.name}: no answer to {command!r} within {self.timeout} s")
        return raw.decode("ascii", "replace").strip()


class BacklightLED(_Ascii):
    """The LED (module doc). :attr:`on` is the last commanded state (None
    until one is sent)."""

    allowed = frozenset({"n8", "f8"})

    def __init__(self, port: Port, *, timeout: float = 0.3, quiet: float = 0.1) -> None:
        super().__init__(port, timeout=timeout, quiet=quiet)
        self.on: bool | None = None

    async def set(self, on: bool) -> None:
        command = "n8" if on else "f8"
        async with self._lock:
            self.port.discard()
            await self.port.write(command.encode("ascii") + self.terminator)
            answer = await self.port.drain(self.quiet, self.timeout)
        self.on = on
        log.info("backlight LED %s (answer %r)", "ON" if on else "off", answer)


class GaugeController(_Ascii):
    """Two convection gauges, read only (module doc)."""

    #: Gauge number -> what it measures.
    GAUGES: ClassVar[dict[int, str]] = {1: "pump line", 2: "spectrograph"}

    def __init__(self, port: Port, *, address: str = "  ", timeout: float = 1.5) -> None:
        if len(address) != 2 or not address.isprintable():
            raise ValueError("gauge controller address: two printable characters")
        super().__init__(port, timeout=timeout)
        self.address = address
        self.allowed = frozenset(f"#{address}RDCG{n}" for n in self.GAUGES)

    async def pressure(self, gauge: int) -> float:
        """Gauge ``gauge`` (1 or 2), mbar."""
        if gauge not in self.GAUGES:
            raise ValueError(f"no gauge {gauge}")
        command = f"#{self.address}RDCG{gauge}"
        answer = await self.ask(command)
        if not answer.startswith("*"):
            raise SerialDeviceError(f"{command!r}: the controller answered {answer!r}")
        m = _FLOAT.search(answer[1 + len(self.address.strip()) :])
        if m is None:
            raise SerialDeviceError(f"{command!r}: no value in {answer!r}")
        return float(m.group())


CHILLER_STATUS_BITS = (
    "run",
    "remote_lock",
    "ready",
    "below_setpoint",
    "above_setpoint",
    "tec_heating",
    "general_warning",
    "general_alarm",
    "autotune",
)
CHILLER_FAULT_BITS = ("rtd", "tank_low", "pump_fail", "fan_fail")


class OasisChiller(_Ascii):
    """The chiller, read only (module doc)."""

    allowed = frozenset({"TEMP?", "SETTEMP?", "PUMPTEMP?", "STAT1A?", "FLTS1A?"})
    terminator = b"\r\n"

    async def number(self, command: str) -> float:
        answer = await self.ask(command)
        m = _FLOAT.search(answer)
        if m is None:
            raise SerialDeviceError(f"{command!r}: no number in {answer!r}")
        return float(m.group())

    async def read(self) -> dict[str, Any]:
        """``temp``, ``setpoint``, ``pump_temp`` (C), ``status`` and
        ``faults`` (named bits), and ``fault``: the faults set, as text, or
        None. ``flow``: None (the unit does not report it)."""
        temp = await self.number("TEMP?")
        setpoint = await self.number("SETTEMP?")
        pump = await self.number("PUMPTEMP?")
        stat = int(await self.number("STAT1A?"))
        flts = int(await self.number("FLTS1A?"))
        status = {n: bool(stat & (1 << i)) for i, n in enumerate(CHILLER_STATUS_BITS)}
        faults = {n: bool(flts & (1 << i)) for i, n in enumerate(CHILLER_FAULT_BITS)}
        bad = [n for n, v in faults.items() if v]
        if status["general_alarm"]:
            bad.append("general alarm")
        return {
            "temp": temp,
            "setpoint": setpoint,
            "pump_temp": pump,
            "flow": None,
            "status": status,
            "faults": faults,
            "fault": ", ".join(bad) or None,
        }
