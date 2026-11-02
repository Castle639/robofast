"""The hardware gate: when a device server may talk to an endpoint.

Every robofast server that speaks a vendor protocol (the Aqawan PAC, the
TCSGO, the spectrograph PC, a PDU, PWI, a guide camera's Alpaca server, a
legacy Redis) refuses real hardware unless the site config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``
(``docs/design.md`` section 3.1, "The hardware gate").

Without the gate, the server talks only to a **simulator it has verified**:

* every address the host resolves to is loopback, **and**
* the socket listening on that loopback port belongs to a process that has
  registered itself as a robofast simulator for that port
  (:class:`SimulatorRegistration`), or is ASCOM's OmniSim.

Loopback alone is not proof. An SSH tunnel (``ssh -L 7100:pac:22004``) puts
real hardware on ``127.0.0.1``, and so would ``socat``, a VPN helper or a
container port mapping. The check is made locally, from ``/proc``, before
the first byte goes on the wire, so an unverified endpoint is never sent
anything, not even a status query.

How a simulator registers: it binds a Linux *abstract* Unix socket named
``robofast-sim:<proto>:<port>`` in the same process that owns the listening
socket. The kernel removes it when the process exits, so a registration
cannot go stale. The check finds the listening socket's inode in
``/proc/net/{tcp,tcp6,udp,udp6}``, the process(es) holding it in
``/proc/<pid>/fd``, and requires that one of them also holds the
registration socket (``/proc/net/unix``). ``ssh``, ``socat`` and the rest
never do.

Limits, all of which fail closed (refuse): only Linux (no ``/proc/net``
elsewhere); only processes whose ``/proc/<pid>/fd`` this user can read (a
simulator run by another user, or in another container, is not verified);
and only endpoints on this host. Use the gate for anything else.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Prefix of the abstract Unix socket a simulator holds.
REGISTRATION_PREFIX = "robofast-sim"

#: ``/proc/<pid>/maps`` entries that identify ASCOM OmniSim (the Alpaca
#: simulators CI uses for cameras, mounts and domes).
OMNISIM_MARKERS = ("ascom.alpaca.simulators.dll",)

_PROC = Path("/proc")


class HardwareNotAllowed(Exception):
    """An endpoint that may be real hardware, without the hardware gate."""


def gate_open(config_allow: bool, cli_hardware: bool, *, what: str = "hardware") -> bool:
    """The gate is open only with ``allow_hardware`` in the config AND
    ``--hardware`` on the command line. Logs when exactly one is given."""
    if config_allow != cli_hardware:
        log.info(
            "%s use needs allow_hardware in the config AND --hardware;"
            " only a verified simulator is allowed now",
            what,
        )
    return bool(config_allow and cli_hardware)


def resolve(host: str, *, what: str, proto: str = "tcp") -> list[ipaddress._BaseAddress]:
    kind = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    try:
        infos = socket.getaddrinfo(host, None, type=kind)
    except socket.gaierror as exc:
        raise HardwareNotAllowed(f"cannot resolve {what} host {host!r}: {exc}") from exc
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def check_endpoint(
    host: str, port: int, *, allow_hardware: bool, what: str, proto: str = "tcp"
) -> str:
    """Resolve ``host`` once and return the IP literal to connect to.

    With ``allow_hardware`` (the gate open) any address is allowed. Without
    it, every address must be loopback and ``port`` must be served by a
    verified simulator (:func:`is_simulator`); otherwise
    :class:`HardwareNotAllowed`. The caller connects to the returned literal,
    so a later DNS change cannot redirect it.
    """
    addrs = resolve(host, what=what, proto=proto)
    ip = str(addrs[0])
    if allow_hardware:
        if is_simulator(ip, port, proto=proto):
            log.info("%s at %s:%d is a robofast simulator (hardware gate open)", what, ip, port)
        return ip
    if not all(a.is_loopback for a in addrs):
        raise HardwareNotAllowed(
            f"{what} host {host!r} ({', '.join(map(str, addrs))}) is not loopback: refusing to"
            " talk to real hardware without allow_hardware in the config AND --hardware"
        )
    require_simulator(ip, port, what=what, proto=proto)
    return ip


def require_simulator(ip: str, port: int, *, what: str, proto: str = "tcp") -> None:
    """:class:`HardwareNotAllowed` unless ``ip:port`` is a verified simulator."""
    if not is_simulator(ip, port, proto=proto):
        raise HardwareNotAllowed(
            f"{what} at {ip}:{port}/{proto} is loopback but not a simulator robofast could verify"
            " (nothing registered as a robofast simulator owns that port; an SSH tunnel to real"
            " hardware looks exactly like this): refusing without allow_hardware in the config"
            " AND --hardware"
        )


def check_url(url: str, *, allow_hardware: bool, what: str) -> str:
    """:func:`check_endpoint` for an ``http://host:port[/...]`` URL (or a bare
    ``host:port``); returns the URL with the host replaced by its IP literal."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url if "://" in url else f"http://{url}")
    if parts.hostname is None:
        raise HardwareNotAllowed(f"{what}: no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip = check_endpoint(parts.hostname, port, allow_hardware=allow_hardware, what=what)
    netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# -- registration (the simulator's side) ---------------------------------------------


class SimulatorRegistration:
    """Held by a simulator for as long as it serves ``port``.

    Binds the abstract Unix socket ``robofast-sim:<proto>:<port>``. Create it
    in the process that owns the listening socket, after binding; close it
    (or let the process exit) when the simulator stops. On a system without
    abstract sockets it does nothing, and the simulator is not verifiable.
    """

    def __init__(self, port: int, *, proto: str = "tcp") -> None:
        self.name = f"{REGISTRATION_PREFIX}:{proto}:{int(port)}"
        self._sock: socket.socket | None = None
        if not sys.platform.startswith("linux"):
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind("\0" + self.name)
        except OSError as exc:
            sock.close()
            log.warning("simulator registration %s failed: %s", self.name, exc)
            return
        self._sock = sock

    @property
    def registered(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> SimulatorRegistration:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- verification (the server's side) ------------------------------------------------


def _listening_inodes(ip: str, port: int, proto: str) -> set[str]:
    """Inodes of sockets bound to ``port`` that would receive traffic for ``ip``
    (TCP: listening; UDP: unconnected)."""
    addr = ipaddress.ip_address(ip)
    want_state = "0A" if proto == "tcp" else "07"
    inodes: set[str] = set()
    for fam in ("", "6"):
        path = _PROC / "net" / f"{proto}{fam}"
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 10 or f[3] != want_state:
                continue
            local_hex, port_hex = f[1].rsplit(":", 1)
            if int(port_hex, 16) != port:
                continue
            local = _hex_ip(local_hex)
            if local is None:
                continue
            if local == addr or local.is_unspecified or _same_v4(local, addr):
                inodes.add(f[9])
    return inodes


def _hex_ip(h: str) -> ipaddress._BaseAddress | None:
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return None
    if len(raw) == 4:
        return ipaddress.IPv4Address(raw[::-1])
    if len(raw) == 16:
        # Four host-order 32-bit words.
        words = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        return ipaddress.IPv6Address(words)
    return None


def _same_v4(a: ipaddress._BaseAddress, b: ipaddress._BaseAddress) -> bool:
    """``::ffff:127.0.0.1`` and ``127.0.0.1`` are the same endpoint."""
    a4 = a.ipv4_mapped if isinstance(a, ipaddress.IPv6Address) else a
    b4 = b.ipv4_mapped if isinstance(b, ipaddress.IPv6Address) else b
    return a4 is not None and a4 == b4


def _socket_owners(inodes: set[str]) -> dict[int, set[str]]:
    """``{pid: {socket inodes it holds}}`` for processes holding any of
    ``inodes`` (searching this process first), with every socket each holds."""
    targets = {f"socket:[{i}]" for i in inodes}
    owners: dict[int, set[str]] = {}
    me = os.getpid()
    pids = [me] + [int(p) for p in os.listdir(_PROC) if p.isdigit() and int(p) != me]
    for pid in pids:
        held: set[str] = set()
        try:
            fds = os.listdir(_PROC / str(pid) / "fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(_PROC / str(pid) / "fd" / fd)
            except OSError:
                continue
            if link.startswith("socket:["):
                held.add(link)
        if held & targets:
            owners[pid] = {h[8:-1] for h in held}
    return owners


def _registration_inodes(name: str) -> set[str]:
    try:
        lines = (_PROC / "net" / "unix").read_text().splitlines()[1:]
    except OSError:
        return set()
    out = set()
    for line in lines:
        f = line.split()
        if len(f) >= 8 and f[7] == "@" + name:
            out.add(f[6])
    return out


def _is_omnisim(pid: int) -> bool:
    try:
        maps = (_PROC / str(pid) / "maps").read_text()
    except OSError:
        return False
    return any(m in maps for m in OMNISIM_MARKERS)


def is_simulator(ip: str, port: int, *, proto: str = "tcp") -> bool:
    """Whether ``ip:port`` (loopback) is served by a verified simulator."""
    try:
        if not ipaddress.ip_address(ip).is_loopback:
            return False
    except ValueError:
        return False
    if not (_PROC / "net" / proto).exists():
        return False
    inodes = _listening_inodes(ip, port, proto)
    if not inodes:
        return False
    owners = _socket_owners(inodes)
    if not owners:
        return False
    reg = _registration_inodes(f"{REGISTRATION_PREFIX}:{proto}:{port}")
    # Every process holding the port must be the simulator (no tunnel
    # sharing the port through SO_REUSEPORT).
    return all(held & reg or _is_omnisim(pid) for pid, held in owners.items())
