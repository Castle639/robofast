"""Serial ports by URL, for drivers whose device is a serial port somewhere.

A driver here never opens ``COM7`` or ``/dev/ttyUSB0`` by name: it opens a
**URL** with pyserial's :func:`serial.serial_for_url`, so the same driver
reaches

* a port on another computer through a serial-to-TCP bridge:
  ``rfc2217://host:port`` (RFC 2217: the bridge applies the baud rate,
  framing and flow control this side asks for) or ``socket://host:port``
  (raw TCP: the bridge's own port settings apply);
* a local port (``/dev/ttyUSB0``) or pyserial's ``loop://``;
* ``sim``: the device's in-process simulator (no pyserial needed).

pyserial is an optional dependency (``pip install pyserial``); only a real
URL needs it.

The hardware gate (:mod:`robofast.hwgate`)
-----------------------------------------------
``sim`` and ``loop://`` are always allowed. A ``socket://`` or
``rfc2217://`` URL must be a verified robofast simulator on loopback
(:mod:`robofast.devices.kiwispec_aux_sim` serves its simulators that
way) unless the gate is open (``allow_hardware`` in the config AND
``--hardware``). Anything else is a local device: gate open, or refused.

The async interface
-------------------
:class:`Port`: ``write``, ``read_until`` (a terminator), ``read_exact`` (n
bytes), ``drain`` (whatever arrives until the line goes quiet), each bounded
by its own timeout; ``discard`` drops unread input. Blocking pyserial calls
run in a worker thread, each bounded by pyserial's own short timeout, so a
dead bridge never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from robofast import hwgate

log = logging.getLogger(__name__)

#: URL schemes that reach a TCP endpoint (checked by the hardware gate).
TCP_SCHEMES = ("socket", "rfc2217")


class PortError(Exception):
    """A serial port could not be opened, or failed."""


class PortTimeout(PortError):
    """The device did not answer within the bound."""


class HardwareNotAllowed(PortError, hwgate.HardwareNotAllowed):
    """A port that may be real hardware, without the hardware gate."""


@dataclass
class PortConfig:
    """Where a device's serial port is, and how to drive it."""

    #: ``sim``, ``rfc2217://host:port``, ``socket://host:port``, ``loop://``
    #: or a local device path.
    url: str = "sim"
    baud: int = 9600
    #: RTS/CTS hardware flow control (the Thorlabs APT controllers want it).
    rtscts: bool = False
    #: Simulator parameters when ``url`` is ``sim``.
    sim: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | str, *, baud: int, what: str) -> PortConfig:
        if isinstance(d, str):
            d = {"url": d}
        if not isinstance(d, dict):
            raise ValueError(f"{what}.port: an object or a URL string, not {d!r}")
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        unknown = set(d) - {"url", "baud", "rtscts", "sim"}
        if unknown:
            raise ValueError(f"{what}.port: unknown keys {sorted(unknown)}")
        cfg = cls(**{"baud": baud, **d})
        if not isinstance(cfg.url, str) or not cfg.url:
            raise ValueError(f"{what}.port.url: a URL string")
        if not isinstance(cfg.baud, int) or cfg.baud <= 0:
            raise ValueError(f"{what}.port.baud: a positive integer")
        if not isinstance(cfg.sim, dict):
            raise ValueError(f"{what}.port.sim: an object")
        return cfg

    @property
    def is_sim(self) -> bool:
        return self.url == "sim"


def check_url(url: str, *, allow_hardware: bool, what: str) -> str:
    """Apply the hardware gate to ``url``; return the URL to open (a TCP
    URL with its host replaced by the IP literal that was checked)."""
    if url == "sim" or url.startswith("loop://"):
        return url
    parts = urlsplit(url)
    if parts.scheme in TCP_SCHEMES:
        if parts.hostname is None or parts.port is None:
            raise PortError(f"{what}: {url!r} needs a host and a port")
        try:
            ip = hwgate.check_endpoint(
                parts.hostname, parts.port, allow_hardware=allow_hardware, what=what
            )
        except hwgate.HardwareNotAllowed as exc:
            raise HardwareNotAllowed(str(exc)) from None
        host = f"[{ip}]" if ":" in ip else ip
        rest = url.split("://", 1)[1]
        tail = rest[len(parts.netloc) :]
        return f"{parts.scheme}://{host}:{parts.port}{tail}"
    if not allow_hardware:
        raise HardwareNotAllowed(
            f"{what}: {url!r} is a local serial port: it needs allow_hardware in the config"
            " AND --hardware (url 'sim' runs the simulator)"
        )
    return url


class Port(Protocol):
    """A byte stream to one device (module doc)."""

    name: str

    async def write(self, data: bytes) -> None: ...

    async def read_until(self, terminator: bytes, timeout: float) -> bytes: ...

    async def read_exact(self, n: int, timeout: float) -> bytes: ...

    async def drain(self, quiet: float, timeout: float, end: bytes | None = None) -> bytes: ...

    def discard(self) -> None: ...

    def close(self) -> None: ...


class _BufferedPort:
    """The reads, over a ``_chunk(wait)`` that returns what has arrived
    (possibly nothing) within about ``wait`` seconds."""

    name = "port"

    def __init__(self) -> None:
        self._buf = bytearray()

    async def _chunk(self, wait: float) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    async def read_until(self, terminator: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            i = self._buf.find(terminator)
            if i >= 0:
                out = bytes(self._buf[: i + len(terminator)])
                del self._buf[: i + len(terminator)]
                return out
            left = deadline - time.monotonic()
            if left <= 0:
                got = bytes(self._buf)
                raise PortTimeout(
                    f"{self.name}: no {terminator!r} within {timeout:g} s (got {got[:80]!r})"
                )
            self._buf += await self._chunk(min(left, 0.05))

    async def read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PortTimeout(
                    f"{self.name}: {len(self._buf)} of {n} bytes within {timeout:g} s"
                )
            self._buf += await self._chunk(min(left, 0.05))
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def drain(self, quiet: float, timeout: float, end: bytes | None = None) -> bytes:
        """An answer of unknown length: wait up to ``timeout`` s for its
        first byte, then take everything until nothing more arrives for
        ``quiet`` s, or until it ends (trailing whitespace aside) with
        ``end``. Empty if nothing arrived."""
        deadline = time.monotonic() + timeout
        last: float | None = time.monotonic() if self._buf else None
        while True:
            now = time.monotonic()
            done = end is not None and bytes(self._buf).rstrip().endswith(end)
            if done or (last is not None and now - last >= quiet) or now >= deadline:
                out = bytes(self._buf)
                self._buf.clear()
                return out
            wait = deadline - now if last is None else min(quiet, deadline - now)
            chunk = await self._chunk(min(wait, 0.05))
            if chunk:
                self._buf += chunk
                last = time.monotonic()

    def discard(self) -> None:
        self._buf.clear()


class SerialURLPort(_BufferedPort):
    """A port opened with :func:`serial.serial_for_url` (module doc).
    Construct it only with a URL that passed :func:`check_url`."""

    def __init__(self, url: str, *, baud: int, rtscts: bool = False, name: str = "") -> None:
        super().__init__()
        try:
            import serial  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - only where pyserial is missing
            raise PortError("pyserial is not installed (pip install pyserial)") from exc
        self.name = name or url
        self.url = url
        try:
            self._ser = serial.serial_for_url(
                url, baudrate=baud, rtscts=rtscts, timeout=0.05, write_timeout=2.0
            )
        except (serial.SerialException, ValueError, OSError) as exc:
            raise PortError(f"{self.name}: cannot open {url!r}: {exc}") from exc
        self._serial_error = serial.SerialException

    async def write(self, data: bytes) -> None:
        try:
            await asyncio.to_thread(self._ser.write, data)
        except (self._serial_error, OSError) as exc:
            raise PortError(f"{self.name}: write failed: {exc}") from exc

    async def _chunk(self, wait: float) -> bytes:
        def read() -> bytes:
            n = max(1, self._ser.in_waiting)
            return bytes(self._ser.read(n))  # bounded by the port's 0.05 s timeout

        try:
            return await asyncio.to_thread(read)
        except (self._serial_error, OSError) as exc:
            raise PortError(f"{self.name}: read failed: {exc}") from exc

    def discard(self) -> None:
        super().discard()
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ser.close()


class ByteSim(Protocol):
    """A simulated device: bytes in, bytes out (its answers, at once)."""

    def feed(self, data: bytes) -> bytes: ...


class SimPort(_BufferedPort):
    """An in-process port to a :class:`ByteSim`."""

    def __init__(self, sim: ByteSim, *, name: str = "sim") -> None:
        super().__init__()
        self.sim = sim
        self.name = name
        self._pending = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise PortError(f"{self.name}: closed")
        self._pending += self.sim.feed(bytes(data))

    async def _chunk(self, wait: float) -> bytes:
        if self._pending:
            out = bytes(self._pending)
            self._pending.clear()
            return out
        await asyncio.sleep(min(wait, 0.01))
        return b""

    def discard(self) -> None:
        super().discard()
        self._pending.clear()

    def close(self) -> None:
        self.closed = True


def open_port(
    cfg: PortConfig,
    make_sim: Callable[..., ByteSim],
    *,
    allow_hardware: bool,
    what: str,
) -> Port:
    """Open ``cfg`` through the gate: the simulator (``make_sim(**cfg.sim)``)
    for ``sim``, else the URL (module doc)."""
    if cfg.is_sim:
        return SimPort(make_sim(**cfg.sim), name=f"{what} (simulator)")
    url = check_url(cfg.url, allow_hardware=allow_hardware, what=what)
    if allow_hardware:
        log.warning("HARDWARE MODE: %s on %s", what, url)
    return SerialURLPort(url, baud=cfg.baud, rtscts=cfg.rtscts, name=what)
