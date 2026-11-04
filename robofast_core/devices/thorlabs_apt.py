"""Thorlabs APT motion controllers over their binary serial protocol (no APT.dll).

KiwiSpec's iodine stage is a Thorlabs stage on an APT stepper controller
(legacy MINERVA opened it through ``APT.dll`` with ``HWTYPE`` 12, BSC101: a
one-channel benchtop stepper driver; serial number in the live
``spectrograph_server.ini``). Its USB port is an FTDI virtual COM port
(VID:PID 0403:FAF0), which speaks the APT protocol directly, so this driver
needs no Windows DLL and runs anywhere pyserial does.

The protocol (Thorlabs "APT Communications Protocol", issue 2x; the serial
settings it gives: 115200 baud, 8N1, RTS/CTS flow control):

* Every message starts with a 6-byte header, little-endian: ``message id``
  (u16), then either two parameter bytes, ``dest``, ``source`` (a short
  message), or ``data length`` (u16), ``dest | 0x80``, ``source`` followed
  by that many data bytes (a long message). The host is ``source`` 0x01; a
  single-channel controller is ``dest`` 0x50 (``"dest"`` in the config).
* Messages used (nothing else is ever sent; :data:`SENT`):

  ===========================  ======  ===================================
  ``HW_NO_FLASH_PROGRAMMING``  0x0018  sent once at connect (as the manual asks)
  ``HW_REQ_INFO``              0x0005  -> ``HW_GET_INFO`` 0x0006 (serial, model, firmware)
  ``MOD_SET_CHANENABLESTATE``  0x0210  energize the channel before a move
  ``MOT_MOVE_HOME``            0x0443  -> ``MOT_MOVE_HOMED`` 0x0444 when done
  ``MOT_MOVE_ABSOLUTE``        0x0453  (long: channel u16, position i32 counts)
                                       -> ``MOT_MOVE_COMPLETED`` 0x0464 when done
  ``MOT_MOVE_STOP``            0x0465  (profiled stop) -> ``MOT_MOVE_STOPPED`` 0x0466
  ``MOT_REQ_STATUSUPDATE``     0x0480  -> ``MOT_GET_STATUSUPDATE`` 0x0481
                                       (channel, position, encoder, status bits)
  ``MOT_REQ_POSCOUNTER``       0x0411  -> ``MOT_GET_POSCOUNTER`` 0x0412 (``status_query: "bits"``)
  ``MOT_REQ_STATUSBITS``       0x0429  -> ``MOT_GET_STATUSBITS`` 0x042A (``status_query: "bits"``)
  ===========================  ======  ===================================

  Unsolicited ``HW_RESPONSE`` 0x0080 / ``HW_RICHRESPONSE`` 0x0081 (an error
  the controller reports) are logged and kept as the stage's fault.
* Status bits (stepper controllers): 0x01 forward limit, 0x02 reverse
  limit, 0x10 / 0x20 moving forward / reverse, 0x40 / 0x80 jogging, 0x200
  homing, 0x400 homed, 0x80000000 channel enabled.

Positions are counts; ``counts_per_mm`` converts (it depends on the
controller's microstepping and the stage's lead screw, and must be
confirmed on the hardware: ``docs/kiwispec_aux.md``).

Motion is supervised here, not by a DLL call that blocks: a move or a home
is started, then the status is polled until it ends, bounded by
``move_timeout`` / ``home_timeout`` (generous: homing alone takes ~13 s;
legacy's 10 s client timeout was too short). A move that overruns its bound
is stopped (``MOT_MOVE_STOP``) and reported. Homing first moves to 0 when
the stage is already homed (as legacy did: a short approach to the home
switch).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from robofast_core.devices.serial_url import Port, PortError, PortTimeout

log = logging.getLogger(__name__)

BAUD = 115200
HOST = 0x01
DEST = 0x50

HW_REQ_INFO = 0x0005
HW_GET_INFO = 0x0006
HW_NO_FLASH_PROGRAMMING = 0x0018
HW_RESPONSE = 0x0080
HW_RICHRESPONSE = 0x0081
MOD_SET_CHANENABLESTATE = 0x0210
MOT_REQ_POSCOUNTER = 0x0411
MOT_GET_POSCOUNTER = 0x0412
MOT_REQ_STATUSBITS = 0x0429
MOT_GET_STATUSBITS = 0x042A
MOT_MOVE_HOME = 0x0443
MOT_MOVE_HOMED = 0x0444
MOT_MOVE_ABSOLUTE = 0x0453
MOT_MOVE_COMPLETED = 0x0464
MOT_MOVE_STOP = 0x0465
MOT_MOVE_STOPPED = 0x0466
MOT_REQ_STATUSUPDATE = 0x0480
MOT_GET_STATUSUPDATE = 0x0481

#: Every message id this driver sends.
SENT = frozenset(
    {
        HW_REQ_INFO,
        HW_NO_FLASH_PROGRAMMING,
        MOD_SET_CHANENABLESTATE,
        MOT_REQ_POSCOUNTER,
        MOT_REQ_STATUSBITS,
        MOT_MOVE_HOME,
        MOT_MOVE_ABSOLUTE,
        MOT_MOVE_STOP,
        MOT_REQ_STATUSUPDATE,
    }
)

FWD_LIMIT = 0x01
REV_LIMIT = 0x02
MOVING_BITS = 0x10 | 0x20 | 0x40 | 0x80 | 0x200
HOMING = 0x200
HOMED = 0x400
ENABLED = 0x80000000

#: The longest data block a message may carry (HW_GET_INFO is 84 bytes).
MAX_DATA = 255


class APTError(PortError):
    """The controller reported an error, or sent something unreadable."""


def short_message(msg_id: int, p1: int = 0, p2: int = 0, *, dest: int = DEST) -> bytes:
    return struct.pack("<HBBBB", msg_id, p1, p2, dest, HOST)


def long_message(msg_id: int, data: bytes, *, dest: int = DEST, source: int = HOST) -> bytes:
    return struct.pack("<HHBB", msg_id, len(data), dest | 0x80, source) + data


@dataclass
class Message:
    id: int
    p1: int = 0
    p2: int = 0
    data: bytes = b""


def parse_header(header: bytes) -> tuple[int, int, int, int, bool]:
    """``(id, p1_or_len_lo, p2_or_len_hi, dest, is_long)``."""
    msg_id, p1, p2, dest, _source = struct.unpack("<HBBBB", header)
    return msg_id, p1, p2, dest & 0x7F, bool(dest & 0x80)


async def read_message(port: Port, timeout: float) -> Message:
    header = await port.read_exact(6, timeout)
    msg_id, p1, p2, _dest, is_long = parse_header(header)
    if not is_long:
        return Message(msg_id, p1, p2)
    n = p1 | (p2 << 8)
    if n > MAX_DATA:
        port.discard()
        raise APTError(f"{port.name}: message 0x{msg_id:04x} claims {n} data bytes; resyncing")
    return Message(msg_id, data=await port.read_exact(n, timeout))


@dataclass
class StageStatus:
    counts: int
    bits: int
    at: float

    @property
    def moving(self) -> bool:
        return bool(self.bits & MOVING_BITS)

    @property
    def homed(self) -> bool:
        return bool(self.bits & HOMED)


@dataclass
class Job:
    kind: str  # "move" or "home"
    target: int | None
    started: float
    deadline: float
    seen_moving: bool = False


@dataclass
class APTConfig:
    counts_per_mm: float
    #: Travel the stage may be sent to (mm). Legacy refused outside 0..150.
    travel: tuple[float, float] = (0.0, 150.0)
    channel: int = 1
    dest: int = DEST
    #: ``"update"`` (MOT_REQ_STATUSUPDATE) or ``"bits"`` (MOT_REQ_POSCOUNTER
    #: + MOT_REQ_STATUSBITS, for firmware without the former).
    status_query: str = "update"
    #: Bound on one request/answer exchange.
    timeout: float = 2.0
    #: Bounds on a move and on homing (generous; module doc).
    move_timeout: float = 120.0
    home_timeout: float = 180.0
    #: Arrived: within this of the target (mm).
    tolerance: float = 0.05
    poll_interval: float = 0.25
    #: A command not yet seen moving may read "stopped" for this long.
    start_grace: float = 1.0
    #: Reads reuse a status this recent.
    status_max_age: float = 0.2
    extra: dict[str, Any] = field(default_factory=dict)


class APTStage:
    """One APT channel with its motion supervised (module doc). Runs on
    one event loop; one exchange at a time."""

    def __init__(self, port: Port, config: APTConfig) -> None:
        if config.counts_per_mm <= 0:
            raise ValueError("counts_per_mm must be positive")
        self.port = port
        self.config = config
        self.info: dict[str, Any] | None = None
        self.job: Job | None = None
        #: The last failed move or home, or a controller error message.
        self.fault: str | None = None
        self.events: list[tuple[float, int]] = []
        self._snap: StageStatus | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None
        #: Homing's second leg, after the pre-move to 0 (module doc).
        self._pending_home: asyncio.Task | None = None

    # -- the wire --------------------------------------------------------------------

    def _send_allowed(self, msg: bytes) -> None:
        msg_id = struct.unpack_from("<H", msg)[0]
        if msg_id not in SENT:
            raise APTError(f"refusing to send message 0x{msg_id:04x}")

    def _note(self, m: Message) -> None:
        """An unsolicited or other message read while waiting for an answer."""
        if m.id in (HW_RESPONSE, HW_RICHRESPONSE):
            text = m.data[4:].split(b"\0", 1)[0].decode("ascii", "replace") if m.data else ""
            code = struct.unpack_from("<H", m.data, 2)[0] if len(m.data) >= 4 else None
            self.fault = f"controller error {code}: {text}".strip()
            log.error("iodine stage: %s", self.fault)
        elif m.id in (MOT_MOVE_COMPLETED, MOT_MOVE_HOMED, MOT_MOVE_STOPPED):
            self.events.append((time.monotonic(), m.id))
            del self.events[:-20]
            self._snap = None
        else:
            log.debug("iodine stage: message 0x%04x ignored", m.id)

    async def _send(self, msg: bytes) -> None:
        self._send_allowed(msg)
        async with self._lock:
            await self.port.write(msg)

    async def _request(self, msg: bytes, answer: int) -> Message:
        self._send_allowed(msg)
        timeout = self.config.timeout
        async with self._lock:
            await self.port.write(msg)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PortTimeout(
                        f"{self.port.name}: no answer 0x{answer:04x} within {timeout:g} s"
                    )
                try:
                    m = await read_message(self.port, left)
                except PortTimeout:
                    raise PortTimeout(
                        f"{self.port.name}: no answer 0x{answer:04x} within {timeout:g} s"
                        " (controller off, wrong port, or not an APT controller?)"
                    ) from None
                if m.id == answer:
                    return m
                self._note(m)

    def _short(self, msg_id: int, p1: int = 0, p2: int = 0) -> bytes:
        return short_message(msg_id, p1, p2, dest=self.config.dest)

    # -- reads -----------------------------------------------------------------------

    async def identify(self) -> dict[str, Any]:
        m = await self._request(self._short(HW_REQ_INFO), HW_GET_INFO)
        d = m.data
        if len(d) < 84:
            raise APTError(f"HW_GET_INFO: {len(d)} bytes, expected 84")
        serial_no, model, hw_type = struct.unpack_from("<I8sH", d, 0)
        fw = d[14:18]
        notes = d[18:66].split(b"\0", 1)[0].decode("ascii", "replace").strip()
        nchs = struct.unpack_from("<H", d, 82)[0]
        return {
            "serial": serial_no,
            "model": model.split(b"\0", 1)[0].decode("ascii", "replace").strip(),
            "type": hw_type,
            "firmware": f"{fw[2]}.{fw[1]}.{fw[0]}",
            "notes": notes,
            "channels": nchs,
        }

    async def connect(self) -> None:
        """Identify the controller and read the status. Moves nothing."""
        if self.info is None:
            await self._send(self._short(HW_NO_FLASH_PROGRAMMING))
            self.info = await self.identify()
            log.info("iodine stage controller: %s", self.info)
        await self.snapshot(0)

    async def _read_status(self) -> StageStatus:
        ch = self.config.channel
        if self.config.status_query == "bits":
            pos = await self._request(self._short(MOT_REQ_POSCOUNTER, ch), MOT_GET_POSCOUNTER)
            bits = await self._request(self._short(MOT_REQ_STATUSBITS, ch), MOT_GET_STATUSBITS)
            if len(pos.data) < 6 or len(bits.data) < 6:
                raise APTError("short POSCOUNTER/STATUSBITS answer")
            counts = struct.unpack_from("<i", pos.data, 2)[0]
            word = struct.unpack_from("<I", bits.data, 2)[0]
        else:
            m = await self._request(self._short(MOT_REQ_STATUSUPDATE, ch), MOT_GET_STATUSUPDATE)
            if len(m.data) < 14:
                raise APTError(f"MOT_GET_STATUSUPDATE: {len(m.data)} bytes, expected 14")
            _ch, counts, _enc, word = struct.unpack_from("<HiiI", m.data, 0)
        return StageStatus(counts, word, time.monotonic())

    async def snapshot(self, max_age: float | None = None) -> StageStatus:
        max_age = self.config.status_max_age if max_age is None else max_age
        snap = self._snap
        if snap is None or time.monotonic() - snap.at > max_age:
            snap = await self._read_status()
            self._snap = snap
            await self._check_job(snap)
        return snap

    def mm(self, counts: int) -> float:
        return counts / self.config.counts_per_mm

    def counts(self, mm: float) -> int:
        return round(mm * self.config.counts_per_mm)

    async def moving(self) -> bool:
        snap = await self.snapshot()
        pending = self._pending_home is not None and not self._pending_home.done()
        return snap.moving or self.job is not None or pending

    async def position_mm(self) -> float:
        return self.mm((await self.snapshot()).counts)

    # -- motion supervision ----------------------------------------------------------

    def start(self) -> None:
        self._monitor = asyncio.ensure_future(self._run_monitor())

    async def shutdown(self) -> None:
        if self._pending_home is not None:
            self._pending_home.cancel()
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._monitor

    async def _run_monitor(self) -> None:
        while True:
            if self.job is not None:
                try:
                    await self.snapshot(0)
                except PortError as exc:
                    log.error("iodine stage monitor: %s", exc)
            await asyncio.sleep(self.config.poll_interval)

    async def _check_job(self, snap: StageStatus) -> None:
        job = self.job
        if job is None:
            return
        cfg = self.config
        now = time.monotonic()
        if snap.moving:
            job.seen_moving = True
            if now > job.deadline:
                self.job = None
                limit = cfg.home_timeout if job.kind == "home" else cfg.move_timeout
                self.fault = f"{job.kind} did not finish within {limit:g} s; stopped"
                log.error("iodine stage: %s", self.fault)
                with contextlib.suppress(PortError):
                    await self._send(self._short(MOT_MOVE_STOP, cfg.channel, 0x02))
            return
        if job.kind == "home":
            # A controller that was homed before may still say so for a
            # moment after MOVE_HOME: not done until it has moved or the
            # start grace has passed.
            done = snap.homed and (job.seen_moving or now - job.started > cfg.start_grace)
            why = "homing ended without the stage homed"
        else:
            assert job.target is not None
            done = abs(snap.counts - job.target) <= self.counts(cfg.tolerance)
            why = f"stopped at {self.mm(snap.counts):.3f} mm, target {self.mm(job.target):.3f} mm"
            if snap.bits & (FWD_LIMIT | REV_LIMIT):
                why += " (at a limit switch)"
        if done:
            self.job = None
            log.info("iodine stage: %s complete after %.1f s", job.kind, now - job.started)
        elif job.seen_moving or now - job.started > cfg.start_grace:
            self.job = None
            self.fault = why
            log.error("iodine stage: %s", why)

    async def _enable(self) -> None:
        snap = await self.snapshot(0)
        if not snap.bits & ENABLED:
            await self._send(self._short(MOD_SET_CHANENABLESTATE, self.config.channel, 0x01))

    async def move_mm(self, mm: float) -> None:
        """Start a move to ``mm`` (returns at once; :meth:`moving` until it ends)."""
        cfg = self.config
        lo, hi = cfg.travel
        if not lo <= mm <= hi:
            raise ValueError(f"{mm:g} mm is outside the travel [{lo:g}, {hi:g}]")
        if self.job is not None:
            raise RuntimeError(f"the stage is busy ({self.job.kind})")
        snap = await self.snapshot(0)
        if not snap.homed:
            raise RuntimeError("the stage is not homed: home it first")
        await self._enable()
        target = self.counts(mm)
        log.info("iodine stage: move %.3f -> %.3f mm", self.mm(snap.counts), mm)
        self.fault = None
        now = time.monotonic()
        self.job = Job("move", target, now, now + cfg.move_timeout)
        try:
            data = struct.pack("<Hi", cfg.channel, target)
            await self._send(long_message(MOT_MOVE_ABSOLUTE, data, dest=cfg.dest))
        except BaseException:
            self.job = None
            raise
        self._snap = None

    async def home(self) -> None:
        """Start homing (module doc). Returns at once."""
        if await self.moving():
            raise RuntimeError("not homing: the stage is moving")
        snap = await self.snapshot(0)
        await self._enable()
        self.fault = None
        log.warning("iodine stage: homing")
        if snap.homed and snap.counts != 0:
            await self.move_mm(0.0)
            self._pending_home = asyncio.ensure_future(self._home_after_move())
            return
        await self._start_home()

    async def _home_after_move(self) -> None:
        deadline = time.monotonic() + self.config.move_timeout + 5
        while self.job is not None and time.monotonic() < deadline:
            await asyncio.sleep(self.config.poll_interval)
        if self.fault is None:
            with contextlib.suppress(PortError):
                await self._start_home()

    async def _start_home(self) -> None:
        cfg = self.config
        now = time.monotonic()
        self.job = Job("home", None, now, now + cfg.home_timeout)
        try:
            await self._send(self._short(MOT_MOVE_HOME, cfg.channel))
        except BaseException:
            self.job = None
            raise
        self._snap = None

    async def stop(self) -> None:
        if self._pending_home is not None:
            self._pending_home.cancel()
        self.job = None
        await self._send(self._short(MOT_MOVE_STOP, self.config.channel, 0x02))
        self._snap = None

    async def status(self) -> dict[str, Any]:
        snap = await self.snapshot(0)
        job = self.job
        return {
            "position_mm": round(self.mm(snap.counts), 4),
            "counts": snap.counts,
            "status_bits": f"0x{snap.bits:08x}",
            "moving": snap.moving or job is not None,
            "homed": snap.homed,
            "limits": {
                "forward": bool(snap.bits & FWD_LIMIT),
                "reverse": bool(snap.bits & REV_LIMIT),
            },
            "job": None
            if job is None
            else {
                "kind": job.kind,
                "target_mm": None if job.target is None else self.mm(job.target),
                "elapsed": round(time.monotonic() - job.started, 1),
            },
            "controller": self.info,
            "fault": self.fault,
        }
