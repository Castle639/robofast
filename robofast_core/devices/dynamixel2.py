"""Robotis Dynamixel servos, protocol 2.0 (XM series): KiwiSpec's backlight arm.

The backlight is an LED disk on an arm that a Dynamixel XM servo swings in
over the fiber input and out again. Legacy MINERVA (``dynamixel.py``, the
``lib_robotis_v2`` port, at 57600 baud, servo ID 1) did, for every move:
torque off, write address 11 (operating mode) = 3 (position control),
torque on, write the goal position (address 116), wait until it stops
moving (address 122); and after the move **out**, wait 0.25 s and turn the
torque off again. Angles map to counts as legacy did:
``2047 + round(radians(deg) / (2 pi / 4095))`` (so -45 deg -> 1535, -90 deg
-> 1023).

The packet (Robotis e-Manual, "Protocol 2.0"):

* instruction: ``FF FF FD 00 | ID | LEN_L LEN_H | INST | PARAMS | CRC_L CRC_H``,
  ``LEN`` = len(INST + PARAMS + CRC);
* status: ``FF FF FD 00 | ID | LEN | 0x55 | ERR | PARAMS | CRC``;
* CRC-16, polynomial 0x8005, initial value 0, over everything before it;
* byte stuffing: ``FF FF FD`` inside INST..PARAMS is sent as ``FF FF FD FD``.

Instructions sent (:data:`INSTRUCTIONS`): PING 0x01, READ 0x02, WRITE 0x03.
Addresses written (:data:`WRITABLE`): 11 operating mode, 64 torque enable,
116 goal position; nothing else (no ID, baud or EEPROM limits are changed).

A servo that does not answer fails each call with :class:`ServoTimeout`
naming the ID, port and baud, never hangs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time

from robofast_core.devices.serial_url import Port, PortError, PortTimeout

log = logging.getLogger(__name__)

BAUD = 57600
HEADER = b"\xff\xff\xfd\x00"
PING, READ, WRITE, STATUS = 0x01, 0x02, 0x03, 0x55
INSTRUCTIONS = frozenset({PING, READ, WRITE})

ADDR_OPERATING_MODE = 11
ADDR_TORQUE_ENABLE = 64
ADDR_HARDWARE_ERROR = 70
ADDR_GOAL_POSITION = 116
ADDR_MOVING = 122
ADDR_PRESENT_POSITION = 132
WRITABLE = frozenset({ADDR_OPERATING_MODE, ADDR_TORQUE_ENABLE, ADDR_GOAL_POSITION})
POSITION_MODE = 3

ERRORS = {
    1: "result fail",
    2: "instruction error",
    3: "CRC error",
    4: "data range error",
    5: "data length error",
    6: "data limit error",
    7: "access error",
}


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc, c = 0, i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) if (crc ^ c) & 0x8000 else (crc << 1)
            c <<= 1
        table.append(crc & 0xFFFF)
    return table


_TABLE = _crc_table()


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def stuff(body: bytes) -> bytes:
    return body.replace(b"\xff\xff\xfd", b"\xff\xff\xfd\xfd")


def unstuff(body: bytes) -> bytes:
    return body.replace(b"\xff\xff\xfd\xfd", b"\xff\xff\xfd")


def packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = stuff(bytes([instruction]) + params)
    head = HEADER + bytes([servo_id]) + struct.pack("<H", len(body) + 2)
    msg = head + body
    return msg + struct.pack("<H", crc16(msg))


def degrees_to_counts(deg: float) -> int:
    """Legacy's angle -> position (module doc)."""
    return 2047 + round(math.radians(deg) / (2 * math.pi / 4095))


def counts_to_degrees(counts: int) -> float:
    return math.degrees((counts - 2047) * 2 * math.pi / 4095)


class ServoError(PortError):
    """The servo answered with an error, or something unreadable."""


class ServoTimeout(ServoError, PortTimeout):
    """The servo did not answer."""


class XMServo:
    """One servo on a Dynamixel bus. One exchange at a time, each bounded."""

    def __init__(self, port: Port, servo_id: int = 1, *, timeout: float = 0.5) -> None:
        if not 0 <= servo_id <= 252:
            raise ValueError("servo_id must be 0..252")
        self.port = port
        self.id = servo_id
        self.timeout = timeout
        self._lock = asyncio.Lock()

    def _no_answer(self, what: str) -> ServoTimeout:
        return ServoTimeout(
            f"Dynamixel ID {self.id} did not answer {what} within {self.timeout:g} s on"
            f" {self.port.name}: check the servo's power and cable, its ID ({self.id}) and"
            f" baud rate ({BAUD}), and the serial bridge"
        )

    async def _exchange(self, instruction: int, params: bytes, what: str) -> bytes:
        if instruction not in INSTRUCTIONS:
            raise ServoError(f"refusing instruction 0x{instruction:02x}")
        async with self._lock:
            self.port.discard()
            await self.port.write(packet(self.id, instruction, params))
            deadline = time.monotonic() + self.timeout
            try:
                return await self._read_status(deadline, what)
            except PortTimeout:
                raise self._no_answer(what) from None

    async def _read_status(self, deadline: float, what: str) -> bytes:
        window = b""
        while window != HEADER:  # resync on the header
            b = await self.port.read_exact(1, max(0.0, deadline - time.monotonic()))
            window = (window + b)[-4:]
        head = await self.port.read_exact(3, max(0.0, deadline - time.monotonic()))
        servo_id, length = head[0], struct.unpack("<H", head[1:])[0]
        if length < 4 or length > 1024:
            raise ServoError(f"{what}: bad status length {length}")
        rest = await self.port.read_exact(length, max(0.0, deadline - time.monotonic()))
        msg = HEADER + head + rest[:-2]
        if crc16(msg) != struct.unpack("<H", rest[-2:])[0]:
            raise ServoError(f"{what}: status packet CRC mismatch")
        body = unstuff(rest[:-2])
        if body[0] != STATUS:
            raise ServoError(f"{what}: not a status packet (0x{body[0]:02x})")
        if servo_id != self.id:
            raise ServoError(f"{what}: answer from ID {servo_id}, expected {self.id}")
        err = body[1]
        if err & 0x7F:
            raise ServoError(f"{what}: servo error {ERRORS.get(err & 0x7F, err & 0x7F)}")
        if err & 0x80:
            log.error("Dynamixel ID %d reports a hardware alert (address 70)", self.id)
        return body[2:]

    # -- instructions ----------------------------------------------------------------

    async def ping(self) -> dict[str, int]:
        p = await self._exchange(PING, b"", "PING")
        if len(p) < 3:
            raise ServoError(f"PING: {len(p)} parameter bytes, expected 3")
        model, fw = struct.unpack("<HB", p[:3])
        return {"model": model, "firmware": fw}

    async def read(self, addr: int, n: int) -> bytes:
        p = await self._exchange(READ, struct.pack("<HH", addr, n), f"READ {addr}")
        if len(p) != n:
            raise ServoError(f"READ {addr}: {len(p)} bytes, expected {n}")
        return p

    async def write(self, addr: int, data: bytes) -> None:
        if addr not in WRITABLE:
            raise ServoError(f"refusing to write address {addr}")
        await self._exchange(WRITE, struct.pack("<H", addr) + data, f"WRITE {addr}")

    # -- the control table -----------------------------------------------------------

    async def torque(self, on: bool) -> None:
        await self.write(ADDR_TORQUE_ENABLE, bytes([1 if on else 0]))

    async def position_mode(self) -> None:
        """Operating mode 3; the torque must be off (EEPROM area)."""
        await self.write(ADDR_OPERATING_MODE, bytes([POSITION_MODE]))

    async def goal(self, counts: int) -> None:
        await self.write(ADDR_GOAL_POSITION, struct.pack("<i", max(0, min(4095, counts))))

    async def position(self) -> int:
        return struct.unpack("<i", await self.read(ADDR_PRESENT_POSITION, 4))[0]

    async def moving(self) -> bool:
        return (await self.read(ADDR_MOVING, 1))[0] != 0

    async def hardware_error(self) -> int:
        return (await self.read(ADDR_HARDWARE_ERROR, 1))[0]
