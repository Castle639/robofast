"""Simulators of KiwiSpec's auxiliary serial devices, byte for byte.

Each simulator is a :class:`~robofast_core.devices.serial_url.ByteSim`:
``feed(bytes) -> bytes`` takes what the driver wrote and returns the
device's answer. The Alpaca server runs one in process for ``"url":
"sim"``; :func:`serve` (and ``python -m
robofast_core.devices.kiwispec_aux_sim``) puts each on its own loopback TCP
port, registered with :mod:`robofast_core.hwgate`, so the real pyserial
path (``socket://127.0.0.1:<port>``) can be exercised end to end without
hardware.

=================  ===========================================================
:class:`TC200Sim`  iodine cell heater: echo + answer + ``>`` prompt; the cell
                   warms or cools toward its set point (or ambient when off)
:class:`APTSim`    iodine stage: Thorlabs APT binary protocol, timed moves and
                   homing, completion messages, limit and fault hooks
:class:`XMServoSim`  backlight arm: Dynamixel protocol 2.0, control table,
                   timed moves; the operating mode is refused with torque on
:class:`LEDSim`    backlight LED controller: ``n8`` / ``f8``, answers nothing
:class:`GaugeSim`  vacuum gauge controller: ``#  RDCG1`` / ``#  RDCG2``
:class:`OasisSim`  chiller: ``TEMP?``, ``SETTEMP?``, ``PUMPTEMP?``,
                   ``STAT1A?``, ``FLTS1A?``
=================  ===========================================================

Every simulator records what it received (``commands``) and has
``silent`` (answer nothing: a dead device or bridge).
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import socket
import socketserver
import struct
import threading
import time
from collections.abc import Callable
from typing import Any

from robofast_core import hwgate
from robofast_core.devices import dynamixel2 as dx
from robofast_core.devices import thorlabs_apt as apt


def _now() -> float:
    return time.monotonic()


class _LineSim:
    """A line-oriented ASCII device."""

    terminator = b"\r"

    def __init__(self) -> None:
        self._buf = b""
        self.commands: list[str] = []
        self.silent = False
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> bytes:
        with self._lock:
            self._buf += data
            out = b""
            while True:
                i = min(
                    (j for j in (self._buf.find(b"\r"), self._buf.find(b"\n")) if j >= 0),
                    default=-1,
                )
                if i < 0:
                    break
                line, self._buf = self._buf[:i], self._buf[i + 1 :]
                text = line.decode("ascii", "replace")
                if not text.strip():
                    continue
                self.commands.append(text)
                answer = self.answer(text)
                if not self.silent and answer is not None:
                    out += answer
            return out

    def answer(self, line: str) -> bytes | None:  # pragma: no cover - abstract
        raise NotImplementedError


class TC200Sim(_LineSim):
    """The iodine cell heater's controller (:mod:`robofast_core.devices.tc200`)."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        setpoint: float = 55.0,
        temp: float = 54.6,
        ambient: float = 20.0,
        tau: float = 120.0,
        alarm: bool = False,
    ) -> None:
        super().__init__()
        self.enabled = enabled
        self.setpoint = setpoint
        self._temp = temp
        self.ambient = ambient
        self.tau = tau
        self.alarm = alarm
        #: Test hook: ``ens`` does nothing (a toggle that did not take).
        self.ens_broken = False
        self._at = _now()

    @property
    def temp(self) -> float:
        now = _now()
        target = self.setpoint if self.enabled else self.ambient
        self._temp = target + (self._temp - target) * math.exp(-(now - self._at) / self.tau)
        self._at = now
        return self._temp

    def answer(self, line: str) -> bytes:
        cmd = line.strip()
        echo = cmd.encode() + b"\r"
        if cmd == "stat?":
            bits = (0x01 if self.enabled else 0) | 0x10 | (0x40 if self.alarm else 0)
            reply = f"{bits:x}"
        elif cmd == "tact?":
            reply = f"{self.temp:.1f} C"
        elif cmd == "tset?":
            reply = f"{self.setpoint:.1f} C"
        elif cmd.startswith("tset="):
            try:
                value = float(cmd[5:])
            except ValueError:
                value = math.nan
            if not 20.0 <= value <= 200.0:
                reply = "CMD_ARG_INVALID"
            else:
                _ = self.temp  # settle the model at the old set point
                self.setpoint = value
                reply = ""
        elif cmd == "ens":
            if not self.ens_broken:
                _ = self.temp
                self.enabled = not self.enabled
            reply = ""
        elif cmd == "*idn?":
            reply = "THORLABS TC200 VERSION 2.0 (simulated)"
        else:
            reply = "CMD_NOT_DEFINED"
        return echo + (reply.encode() + b"\r" if reply else b"") + b"> "


class LEDSim(_LineSim):
    """The backlight LED controller: ``n8`` on, ``f8`` off; answers nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.on = False
        #: (time.time(), on) for every switch.
        self.history: list[tuple[float, bool]] = []

    def answer(self, line: str) -> None:
        cmd = line.strip()
        if cmd in ("n8", "f8"):
            self.on = cmd == "n8"
            self.history.append((time.time(), self.on))
        return None


class GaugeSim(_LineSim):
    """The vacuum gauge controller: CG1 the pump line, CG2 the spectrograph (mbar)."""

    def __init__(self, *, pump: float = 1.0e3, spec: float = 2.3e-3, address: str = "  ") -> None:
        super().__init__()
        self.pressures = {1: pump, 2: spec}
        self.address = address

    def answer(self, line: str) -> bytes:
        a = self.address
        for n, p in self.pressures.items():
            if line == f"#{a}RDCG{n}":
                return f"*{a}{p:.2E}\r".encode()
        return f"?{a}SYNTX ER\r".encode()


class OasisSim(_LineSim):
    """The chiller (read only queries)."""

    def __init__(
        self,
        *,
        temp: float = 15.0,
        setpoint: float = 15.0,
        pump_temp: float = 22.0,
        status: int = 0b101,
        faults: int = 0,
    ) -> None:
        super().__init__()
        self.values: dict[str, float] = {
            "TEMP?": temp,
            "SETTEMP?": setpoint,
            "PUMPTEMP?": pump_temp,
            "STAT1A?": status,
            "FLTS1A?": faults,
        }

    def answer(self, line: str) -> bytes:
        cmd = line.strip()
        if cmd in self.values:
            v = self.values[cmd]
            text = str(int(v)) if cmd in ("STAT1A?", "FLTS1A?") else f"{v:.1f}"
            return text.encode() + b"\r\n"
        return b"ERR\r\n"


# -- Thorlabs APT ----------------------------------------------------------------------


class APTSim:
    """A one-channel APT stepper controller with a stage (``thorlabs_apt``)."""

    def __init__(
        self,
        *,
        counts_per_mm: float = 25600.0,
        position: float = 11.0,
        homed: bool = True,
        enabled: bool = True,
        velocity: float = 10.0,
        home_time: float = 13.0,
        travel: tuple[float, float] = (0.0, 150.0),
        serial: int = 40000000,
        model: str = "BSC101",
        status_query: str = "update",
    ) -> None:
        self.cpm = float(counts_per_mm)
        self.travel = (float(travel[0]), float(travel[1]))
        self._pos = round(position * self.cpm) if homed else 0
        self.homed = homed
        self.enabled = enabled
        self.velocity = float(velocity)
        self.home_time = float(home_time)
        self.serial = serial
        self.model = model
        #: ``"update"``: answers MOT_REQ_STATUSUPDATE; ``"bits"``: only
        #: POSCOUNTER and STATUSBITS (older firmware).
        self.status_query = status_query
        self._motion: dict[str, Any] | None = None
        self._buf = b""
        self._out = b""
        self.commands: list[int] = []
        self.silent = False
        #: Test hook: moves never progress (a jammed stage).
        self.stuck = False
        self._lock = threading.Lock()

    # -- the mechanism ------------------------------------------------------------

    def _settle(self) -> None:
        m = self._motion
        if m is None:
            return
        if self.stuck:
            m["t0"] = _now()
            return
        elapsed = _now() - m["t0"]
        if elapsed >= m["duration"]:
            self._pos = m["target"]
            self._motion = None
            if m["kind"] == "home":
                self.homed = True
                self._emit(apt.short_message(apt.MOT_MOVE_HOMED, 1, 0, dest=apt.HOST))
            else:
                self._emit(self._status_long(apt.MOT_MOVE_COMPLETED))
        else:
            frac = elapsed / m["duration"]
            self._pos = round(m["origin"] + (m["target"] - m["origin"]) * frac)

    def _start(self, kind: str, target: int, duration: float) -> None:
        self._motion = {
            "kind": kind,
            "origin": self._pos,
            "target": target,
            "t0": _now(),
            "duration": max(duration, 0.01),
        }

    @property
    def position_mm(self) -> float:
        with self._lock:
            self._settle()
            return self._pos / self.cpm

    @property
    def moving(self) -> bool:
        with self._lock:
            self._settle()
            return self._motion is not None

    def _bits(self) -> int:
        bits = apt.ENABLED if self.enabled else 0
        m = self._motion
        if m is not None:
            bits |= 0x10 if m["target"] > m["origin"] else 0x20
            if m["kind"] == "home":
                bits |= apt.HOMING
        if self.homed:
            bits |= apt.HOMED
        lo, hi = (round(t * self.cpm) for t in self.travel)
        if self._pos <= lo and self.homed:
            bits |= apt.REV_LIMIT
        if self._pos >= hi:
            bits |= apt.FWD_LIMIT
        return bits

    def _status_long(self, msg_id: int) -> bytes:
        data = struct.pack("<HiiI", 1, self._pos, self._pos, self._bits())
        return apt.long_message(msg_id, data, dest=apt.HOST, source=apt.DEST)

    def _emit(self, msg: bytes) -> None:
        self._out += msg

    # -- the wire -----------------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        with self._lock:
            self._settle()
            self._buf += data
            while len(self._buf) >= 6:
                msg_id, p1, p2, _dest, is_long = apt.parse_header(self._buf[:6])
                n = (p1 | (p2 << 8)) if is_long else 0
                if len(self._buf) < 6 + n:
                    break
                body, self._buf = self._buf[6 : 6 + n], self._buf[6 + n :]
                self.commands.append(msg_id)
                self._handle(msg_id, p1, p2, body)
            out, self._out = self._out, b""
            return b"" if self.silent else out

    def _handle(self, msg_id: int, p1: int, p2: int, body: bytes) -> None:
        if msg_id == apt.HW_REQ_INFO:
            data = struct.pack("<I8sH", self.serial, self.model.encode()[:8], 16)
            data += bytes([0, 1, 2, 0]) + b"simulated".ljust(48, b"\0") + b"\0" * 12
            data += struct.pack("<HHH", 1, 0, 1)
            self._emit(apt.long_message(apt.HW_GET_INFO, data, dest=apt.HOST, source=apt.DEST))
        elif msg_id == apt.MOD_SET_CHANENABLESTATE:
            self.enabled = p2 == 0x01
        elif msg_id == apt.MOT_REQ_STATUSUPDATE and self.status_query == "update":
            self._emit(self._status_long(apt.MOT_GET_STATUSUPDATE))
        elif msg_id == apt.MOT_REQ_POSCOUNTER:
            data = struct.pack("<Hi", 1, self._pos)
            msg = apt.long_message(apt.MOT_GET_POSCOUNTER, data, dest=apt.HOST, source=apt.DEST)
            self._emit(msg)
        elif msg_id == apt.MOT_REQ_STATUSBITS:
            data = struct.pack("<HI", 1, self._bits())
            msg = apt.long_message(apt.MOT_GET_STATUSBITS, data, dest=apt.HOST, source=apt.DEST)
            self._emit(msg)
        elif msg_id == apt.MOT_MOVE_HOME:
            if self.enabled:
                self._start("home", 0, self.home_time)
        elif msg_id == apt.MOT_MOVE_ABSOLUTE and len(body) >= 6:
            target = struct.unpack_from("<i", body, 2)[0]
            lo, hi = (round(t * self.cpm) for t in self.travel)
            target = max(lo, min(hi, target))  # the limit switches stop it
            if self.enabled:
                distance = abs(target - self._pos) / self.cpm
                self._start("move", target, distance / self.velocity)
        elif msg_id == apt.MOT_MOVE_STOP:
            self._motion = None
            self._emit(apt.short_message(apt.MOT_MOVE_STOPPED, 1, 0, dest=apt.HOST))


# -- Dynamixel --------------------------------------------------------------------------


class XMServoSim:
    """A Dynamixel XM servo on protocol 2.0 (``dynamixel2``)."""

    MODEL = 1020  # XM430-W350

    def __init__(
        self,
        *,
        servo_id: int = 1,
        position: int = 1023,
        speed: float = 4000.0,
        operating_mode: int = 3,
    ) -> None:
        self.id = servo_id
        self.mode = operating_mode
        self.torque = False
        self._pos = float(position)
        self.goal = position
        self.speed = speed
        self._at = _now()
        self._buf = b""
        self.commands: list[tuple[int, bytes]] = []
        self.silent = False
        self._lock = threading.Lock()

    def _settle(self) -> None:
        now = _now()
        if self.torque and self.mode == dx.POSITION_MODE:
            step = self.speed * (now - self._at)
            d = self.goal - self._pos
            self._pos = float(self.goal) if abs(d) <= step else self._pos + math.copysign(step, d)
        self._at = now

    @property
    def position(self) -> int:
        with self._lock:
            self._settle()
            return round(self._pos)

    def _status(self, err: int = 0, params: bytes = b"") -> bytes:
        body = dx.stuff(bytes([dx.STATUS, err]) + params)
        msg = dx.HEADER + bytes([self.id]) + struct.pack("<H", len(body) + 2) + body
        return msg + struct.pack("<H", dx.crc16(msg))

    def _table(self, addr: int, n: int) -> bytes:
        self._settle()
        values = {
            dx.ADDR_OPERATING_MODE: (self.mode, 1),
            dx.ADDR_TORQUE_ENABLE: (int(self.torque), 1),
            dx.ADDR_HARDWARE_ERROR: (0, 1),
            dx.ADDR_GOAL_POSITION: (self.goal, 4),
            dx.ADDR_MOVING: (int(round(self._pos) != self.goal and self.torque), 1),
            dx.ADDR_PRESENT_POSITION: (round(self._pos), 4),
        }
        value, size = values.get(addr, (0, n))
        return int(value).to_bytes(size, "little", signed=True)[:n].ljust(n, b"\0")

    def feed(self, data: bytes) -> bytes:
        with self._lock:
            self._buf += data
            out = b""
            while True:
                i = self._buf.find(dx.HEADER)
                if i < 0 or len(self._buf) < i + 7:
                    break
                length = struct.unpack_from("<H", self._buf, i + 5)[0]
                end = i + 7 + length
                if len(self._buf) < end:
                    break
                pkt, self._buf = self._buf[i:end], self._buf[end:]
                if dx.crc16(pkt[:-2]) != struct.unpack("<H", pkt[-2:])[0]:
                    continue
                servo_id = pkt[4]
                body = dx.unstuff(pkt[7:-2])
                self.commands.append((body[0], body[1:]))
                if servo_id != self.id or self.silent:
                    continue
                out += self._handle(body[0], body[1:])
            return out

    def _handle(self, inst: int, params: bytes) -> bytes:
        if inst == dx.PING:
            return self._status(params=struct.pack("<HB", self.MODEL, 45))
        if inst == dx.READ and len(params) == 4:
            addr, n = struct.unpack("<HH", params)
            return self._status(params=self._table(addr, n))
        if inst == dx.WRITE and len(params) >= 3:
            addr = struct.unpack_from("<H", params)[0]
            value = int.from_bytes(params[2:], "little", signed=True)
            self._settle()
            if addr == dx.ADDR_OPERATING_MODE:
                if self.torque:
                    return self._status(err=7)  # EEPROM area: torque must be off
                self.mode = value
            elif addr == dx.ADDR_TORQUE_ENABLE:
                self.torque = bool(value)
            elif addr == dx.ADDR_GOAL_POSITION:
                if not 0 <= value <= 4095:
                    return self._status(err=6)
                self.goal = value
            else:
                return self._status(err=7)
            return self._status()
        return self._status(err=2)


# -- loopback TCP ------------------------------------------------------------------------


class _SimTCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, sim: Any, host: str) -> None:
        self.sim = sim
        super().__init__((host, 0), _SimHandler)
        self.registration = hwgate.SimulatorRegistration(self.server_address[1])


class _SimHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(0.05)
        sim = self.server.sim  # type: ignore[attr-defined]
        while True:
            try:
                data = sock.recv(4096)
            except TimeoutError:
                data = sim.feed(b"")  # completion messages due by now
                if data:
                    sock.sendall(data)
                continue
            except OSError:
                return
            if not data:
                return
            out = sim.feed(data)
            if out:
                sock.sendall(out)


#: Device name -> simulator class, for :func:`serve` and the command line.
SIMS: dict[str, Callable[..., Any]] = {
    "heater": TC200Sim,
    "stage": APTSim,
    "servo": XMServoSim,
    "led": LEDSim,
    "gauges": GaugeSim,
    "chiller": OasisSim,
}


class SimServers:
    """Each simulator on its own registered loopback TCP port."""

    def __init__(self, sims: dict[str, Any], host: str = "127.0.0.1") -> None:
        self.sims = sims
        self.servers = {name: _SimTCP(sim, host) for name, sim in sims.items()}
        self._threads = [
            threading.Thread(target=s.serve_forever, name=f"sim-{n}", daemon=True)
            for n, s in self.servers.items()
        ]
        for t in self._threads:
            t.start()

    def url(self, name: str) -> str:
        host, port = self.servers[name].server_address[:2]
        return f"socket://{host}:{port}"

    def urls(self) -> dict[str, str]:
        return {name: self.url(name) for name in self.servers}

    def close(self) -> None:
        for s in self.servers.values():
            s.shutdown()
            s.server_close()
            s.registration.close()


def serve(names: list[str] | None = None, host: str = "127.0.0.1", **params: Any) -> SimServers:
    """Start the named simulators (default all), ``params[name]`` their arguments."""
    names = list(SIMS) if names is None else names
    return SimServers({n: SIMS[n](**params.get(n, {})) for n in names}, host)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--devices", default=",".join(SIMS), help="comma-separated: " + ",".join(SIMS))
    args = ap.parse_args(argv)
    servers = serve(args.devices.split(","))
    print(json.dumps(servers.urls()), flush=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    servers.close()


if __name__ == "__main__":
    main()
