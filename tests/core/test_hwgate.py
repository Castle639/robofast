"""The hardware gate: loopback is not a simulator (an SSH tunnel is loopback).

Every server that speaks a vendor protocol must, without ``allow_hardware``
in the config AND ``--hardware``, refuse an endpoint on 127.0.0.1 that is not
a verified robofast simulator -- and send it nothing. :class:`FakeTunnel`
plays the tunnel (``ssh -L``): a plain loopback listener that records every
connection and byte.
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from robofast_core import hwgate
from robofast_core.devices.aqawan_server import AqawanConfig, AqawanServer
from robofast_core.devices.aqawan_sim import AqawanSim
from robofast_core.devices.kiwispec_client import SICameraClient, SpecServerClient
from robofast_core.devices.pdu_server import PDUConfig, PDUServer
from robofast_core.devices.snmp import SNMPClient, SNMPError
from robofast_core.devices.tcsgo_client import TCSClient
from robofast_core.devices.tcsgo_server import TCSGOConfig, TCSGOServer
from robofast_core.devices.tcsgo_sim import SIM_SERVER_SITE
from robofast_core.devices.tres_frontend_server import TRESFrontEndConfig, TRESFrontEndServer
from robofast_core.guider.base import check_address
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.tunnel import FakeTunnel

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="simulator verification reads /proc"
)

GATE_CLOSED = ((False, False), (True, False), (False, True))


@pytest.fixture
def tunnel():
    with FakeTunnel() as t:
        yield t


# -- the mechanism ---------------------------------------------------------------------


def test_plain_loopback_listener_is_not_a_simulator(tunnel):
    assert not hwgate.is_simulator("127.0.0.1", tunnel.port)
    with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
        hwgate.check_endpoint("127.0.0.1", tunnel.port, allow_hardware=False, what="PAC")
    # The gate open: allowed (a real device through a tunnel is the point).
    assert (
        hwgate.check_endpoint("127.0.0.1", tunnel.port, allow_hardware=True, what="PAC")
        == "127.0.0.1"
    )
    assert not tunnel.touched


def test_registered_listener_is_a_simulator_until_it_unregisters():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        reg = hwgate.SimulatorRegistration(port)
        assert reg.registered
        assert hwgate.is_simulator("127.0.0.1", port)
        # Registered for TCP only; and ::1 is a different endpoint.
        assert not hwgate.is_simulator("127.0.0.1", port, proto="udp")
        assert not hwgate.is_simulator("::1", port)
        reg.close()
        assert not hwgate.is_simulator("127.0.0.1", port)


def test_registration_for_another_port_does_not_count(tunnel):
    with hwgate.SimulatorRegistration(tunnel.port + 1 if tunnel.port < 65535 else 1):
        assert not hwgate.is_simulator("127.0.0.1", tunnel.port)


def test_simulator_in_another_process_is_verified():
    code = (
        "import socket, sys, time\n"
        "from robofast_core import hwgate\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "r = hwgate.SimulatorRegistration(s.getsockname()[1])\n"
        "print(s.getsockname()[1], flush=True); sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(proc.stdout.readline())
        assert hwgate.is_simulator("127.0.0.1", port)
    finally:
        proc.kill()
        proc.wait()


def test_a_non_loopback_address_is_never_a_simulator():
    assert not hwgate.is_simulator("192.0.2.1", 22004)
    with pytest.raises(hwgate.HardwareNotAllowed, match="not loopback"):
        hwgate.check_endpoint("192.0.2.1", 22004, allow_hardware=False, what="PAC")


def test_guard_notices_a_tunnel_taking_over_the_port():
    """A simulator goes away and something else listens on its port: the next
    connection is refused (the guard re-verifies before each one)."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    reg = hwgate.SimulatorRegistration(port)
    guard = hwgate.SimulatorGuard("127.0.0.1", port, hardware=False, what="PAC")
    guard.check()
    reg.close()
    s.close()
    with socket.socket() as t:
        t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        t.bind(("127.0.0.1", port))
        t.listen()
        with pytest.raises(hwgate.HardwareNotAllowed):
            guard.check()


def test_gate_open_needs_both():
    assert hwgate.gate_open(True, True)
    for allow, hw in GATE_CLOSED:
        assert not hwgate.gate_open(allow, hw)


def test_url_check_substitutes_the_literal(tunnel):
    with pytest.raises(hwgate.HardwareNotAllowed):
        hwgate.check_url(f"http://localhost:{tunnel.port}/status", allow_hardware=False, what="x")
    url = hwgate.check_url(f"http://127.0.0.1:{tunnel.port}/s", allow_hardware=True, what="x")
    assert url == f"http://127.0.0.1:{tunnel.port}/s"


# -- every server refuses a tunnel without the gate, and sends it nothing ---------------


def test_aqawan_server_refuses_a_tunnelled_pac(tunnel):
    for allow, hardware in GATE_CLOSED:
        cfg = AqawanConfig.from_dict(
            {
                "name": "t",
                "open_order": [1, 2],
                "listen": "127.0.0.1:0",
                "allow_hardware": allow,
                "pac": {"host": "127.0.0.1", "port": tunnel.port},
            }
        )
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            AqawanServer(cfg, hardware=hardware)
    assert not tunnel.touched


def test_aqawan_server_accepts_its_simulator():
    sim = AqawanSim().start()
    try:
        cfg = AqawanConfig.from_dict(
            {
                "name": "t",
                "open_order": [1, 2],
                "listen": "127.0.0.1:0",
                "pac": {"host": "127.0.0.1", "port": sim.port},
            }
        )
        server = AqawanServer(cfg)
        server.server_close()
    finally:
        sim.stop()


def test_tcsgo_refuses_a_tunnelled_tcs(tunnel):
    for allow, hardware in GATE_CLOSED:
        cfg = TCSGOConfig.from_dict(
            {
                "name": "t",
                **SIM_SERVER_SITE,
                "listen": "127.0.0.1:0",
                "allow_hardware": allow,
                "tcs": {"host": "127.0.0.1", "port": tunnel.port},
            }
        )
        with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
            TCSGOServer(cfg, hardware=hardware)
    with pytest.raises(hwgate.HardwareNotAllowed):
        TCSClient("127.0.0.1", tunnel.port)
    assert not tunnel.touched


def test_kiwispec_clients_refuse_a_tunnel(tunnel):
    with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
        SpecServerClient("127.0.0.1", tunnel.port)
    with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
        SICameraClient("127.0.0.1", tunnel.port)
    # The gate open: constructed (nothing is sent until a command).
    SpecServerClient("127.0.0.1", tunnel.port, allow_hardware=True)
    assert not tunnel.touched


async def test_pdu_refuses_a_udp_forward():
    from tests.core.test_pdu import CREDS

    with FakeTunnel(proto="udp") as t:
        for allow, hardware in GATE_CLOSED:
            cfg = PDUConfig.from_dict(
                {"pdu": {"host": "127.0.0.1", "port": t.port}, "allow_hardware": allow}
            )
            with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
                PDUServer(cfg, hardware=hardware, credentials=CREDS)
        snmp = SNMPClient("127.0.0.1", t.port, read_community="r", timeout=0.2, retries=0)
        with pytest.raises(SNMPError, match="tunnel"):
            await snmp.get("1.3.6.1.2.1.1.1.0")
        assert not t.touched


def test_tres_frontend_refuses_a_tunnelled_redis(tunnel):
    d = {
        "name": "t",
        "positions": {"out": 0.0},
        "listen": "127.0.0.1:0",
        "port": {"type": "sim"},
        "legacy_redis": {"host": "127.0.0.1", "port": tunnel.port},
    }
    with pytest.raises(hwgate.HardwareNotAllowed, match="legacy_redis"):
        TRESFrontEndServer(TRESFrontEndConfig.from_dict(d))
    assert not tunnel.touched


def test_guider_refuses_a_tunnelled_alpaca_device(tunnel):
    with pytest.raises(hwgate.HardwareNotAllowed, match="tunnel"):
        check_address(tunnel.address, "Alpaca camera", allowed=False)
    check_address(tunnel.address, "Alpaca camera", allowed=True)
    fake = FakeAlpacaServer({}).start()
    try:
        check_address(fake.address, "Alpaca camera", allowed=False)
    finally:
        fake.stop()
    assert not tunnel.touched
