"""A stand-in for an SSH tunnel: a plain listener on loopback.

``ssh -L 7100:pac:22004`` makes real hardware answer on ``127.0.0.1:7100``.
To a robofast server that looks exactly like a simulator on loopback, so the
hardware gate (:mod:`robofast_core.hwgate`) must refuse it unless hardware
use is allowed. :class:`FakeTunnel` is such an endpoint, never registered as
a simulator, which records every connection and byte it receives, so a test
can show that a server without the gate refused it *and sent it nothing*.
"""

from __future__ import annotations

import socket
import threading


class FakeTunnel:
    """A TCP (or UDP) listener on ``127.0.0.1`` that accepts, records and
    never answers. Use as a context manager."""

    def __init__(self, *, proto: str = "tcp", host: str = "127.0.0.1") -> None:
        self.proto = proto
        kind = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
        self._sock = socket.socket(socket.AF_INET, kind)
        self._sock.bind((host, 0))
        if proto == "tcp":
            self._sock.listen()
        self._sock.settimeout(0.1)
        self.host, self.port = self._sock.getsockname()[:2]
        #: Connections accepted (TCP) and bytes received.
        self.connections = 0
        self.received = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-tunnel", daemon=True)
        self._thread.start()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def touched(self) -> bool:
        return bool(self.connections or self.received)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.proto == "udp":
                    data, _ = self._sock.recvfrom(65536)
                    self.received += data
                    continue
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            conn.settimeout(0.2)
            try:
                while data := conn.recv(65536):
                    self.received += data
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def __enter__(self) -> FakeTunnel:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
