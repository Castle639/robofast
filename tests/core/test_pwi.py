"""The PWI server, client, simulators and jog tool (docs/pwi.md).

Fast tier: the simulators and the server run in this process (on their own
threads). Nothing here leaves loopback; the "tunnel" tests show that a
loopback endpoint that is not a registered simulator is refused without the
hardware gate.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest

from robofast_core import hwgate
from robofast_core.alpaca_client import (
    ActionNotImplementedException,
    AlpacaDevice,
    InvalidOperationException,
    InvalidValueException,
    NotConnectedException,
    ParkedException,
)
from robofast_core.devices.pwi_client import (
    PWI2Client,
    PWI4Client,
    PWICommandError,
    PWIError,
    PWIProtocolError,
    PWITimeout,
    make_client,
    parse_pwi2_status,
    parse_pwi4_status,
)
from robofast_core.devices.pwi_jog import Jog, JogRefused, plan_altaz, plan_radec
from robofast_core.devices.pwi_jog import main as jog_main
from robofast_core.devices.pwi_server import PWIConfig, PWIServer, target_altaz
from robofast_core.devices.pwi_sim import SIM_LATITUDE, SIM_LONGITUDE, PWISim, pwi2_status
from robofast_core.testing.checks import wait_until
from robofast_core.testing.tunnel import FakeTunnel

ROOT = Path(__file__).resolve().parents[2]
BACKENDS = ("pwi4", "pwi2")
T = 5.0


def config(port: int, backend: str, **kw) -> PWIConfig:
    base = {
        "name": "test",
        "backend": backend,
        "latitude": SIM_LATITUDE,
        "longitude": SIM_LONGITUDE,
        "elevation": 2000.0,
        "min_alt": 20.0,
        "max_alt": 85.0,
        "park_alt": 30.0,
        "park_az": 90.0,
        "focus_min": 100,
        "focus_max": 33000,
        "ports": {"imager": 1, "fau": 2},
        "listen": "127.0.0.1:0",
        "pwi": {"host": "127.0.0.1", "port": port, "timeout": 2.0},
        "status_max_age": 0.05,
        "slew_report_min": 0.2,
        "watch_interval": 0.1,
        "m3_timeout": 5.0,
        "request_timeout": 5.0,
    }
    return PWIConfig.from_dict({**base, **kw})


@pytest.fixture
def make():
    made: list = []

    def build(backend: str, *, ready: bool = True, hardware: bool = False, sim_kw=None, **kw):
        skw = {
            "slew_rate": 20.0,
            "home_time": 0.3,
            "m3_time": 0.3,
            "ready": ready,
            **(sim_kw or {}),
        }
        sim = PWISim(backend, **skw)
        sim.start()
        made.append(sim)
        server = PWIServer(config(sim.port, backend, **kw), hardware=hardware).start()
        made.append(server)
        return sim, server

    yield build
    for x in reversed(made):
        x.stop()


async def device(server: PWIServer, kind: str = "telescope") -> AlpacaDevice:
    d = AlpacaDevice(server.address, kind)
    await d.connect(timeout=T)
    return d


async def until(fn, timeout: float = 5.0, what: str = "") -> None:
    t0 = time.monotonic()
    while not await fn():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timed out: {what}")
        await asyncio.sleep(0.05)


# -- the gate -------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_tunnel_on_loopback_is_refused_without_the_gate(backend):
    tunnel = FakeTunnel()
    try:
        for allow, hw in ((False, False), (True, False), (False, True)):
            cfg = config(tunnel.port, backend, allow_hardware=allow)
            with pytest.raises(hwgate.HardwareNotAllowed, match="not a simulator"):
                PWIServer(cfg, hardware=hw)
        time.sleep(0.2)
        assert not tunnel.touched  # not even a connection
        # The gate open (config AND --hardware): allowed, still nothing sent
        # until a client asks.
        server = PWIServer(config(tunnel.port, backend, allow_hardware=True), hardware=True)
        server.stop()
        assert not tunnel.touched
    finally:
        tunnel.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tunnel_taking_the_port_later_is_never_sent_anything(make, backend):
    """The simulator goes away and something else (a tunnel) listens on its
    port: the server refuses before sending a byte."""
    sim, server = make(backend)
    tel = await device(server)
    await tel.get("Altitude", timeout=T)
    port = sim.port
    sim.stop()
    tunnel = socket.socket()
    tunnel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tunnel.bind(("127.0.0.1", port))
    tunnel.listen()
    tunnel.settimeout(0.5)
    try:
        await asyncio.sleep(0.2)  # past the status cache
        with pytest.raises(Exception, match="not a simulator"):
            await tel.get("Altitude", timeout=T)
        with pytest.raises(TimeoutError):
            tunnel.accept()  # never connected to
    finally:
        tunnel.close()
        await tel.aclose()


def test_not_loopback_is_refused_without_the_gate():
    cfg = config(8220, "pwi4", pwi={"host": "192.0.2.1", "port": 8220})
    with pytest.raises(hwgate.HardwareNotAllowed, match="not loopback"):
        PWIServer(cfg)


@pytest.mark.parametrize("name", ["t1_pwi2", "t2_pwi2", "t1_pwi4", "t2_pwi4"])
def test_remote_configs_need_hardware_flag(name):
    path = ROOT / "sites" / "minerva" / "remote" / f"{name}.json"
    d = json.loads(path.read_text())
    cfg = PWIConfig.from_dict(d)
    assert cfg.allow_hardware and cfg.pwi.host == "127.0.0.1"
    assert cfg.min_alt == 21.0 and cfg.max_alt == 83.0 and cfg.max_jog_deg <= 1.0
    with FakeTunnel() as tunnel:  # the tunnel, on a free port
        d["pwi"]["port"] = tunnel.port
        with pytest.raises(hwgate.HardwareNotAllowed):
            PWIServer(PWIConfig.from_dict(d))  # no --hardware
        assert not tunnel.touched


def test_jog_refuses_tunnel_without_hardware(tmp_path, capsys):
    tunnel = FakeTunnel()
    try:
        cfg = json.loads((ROOT / "sites/minerva/remote/t1_pwi2.json").read_text())
        cfg["pwi"]["port"] = tunnel.port
        p = tmp_path / "c.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(SystemExit) as e:
            jog_main(["--config", str(p), "--log", str(tmp_path / "j.log"), "stop"])
        assert e.value.code == 2
        assert not tunnel.touched
        assert "not a simulator" in (tmp_path / "j.log").read_text()
    finally:
        tunnel.close()


# -- the client ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_client_status_and_allowlist(backend):
    sim = PWISim(backend).start()
    c = make_client(backend, "127.0.0.1", sim.port, timeout=1.0)
    try:
        st = await c.status()
        assert not st.connected and not st.motors_enabled and st.m3_port == 1
        assert st.lst_hours is not None and abs(st.alt_deg - 30.0) < 1e-6
        assert sim.model.commands == []  # a status read commands nothing
        with pytest.raises(PWIError, match="not an allowed"):
            if backend == "pwi4":
                await c._cmd("/mount/sync")  # type: ignore[attr-defined]
            else:
                await c._cmd("mount", "sync")  # type: ignore[attr-defined]
        sim.set_mode("garble")
        with pytest.raises(PWIProtocolError):
            await c.status()
        sim.set_mode("http500")
        with pytest.raises(PWICommandError):
            await c.status()
        sim.set_mode("hang")
        t0 = time.monotonic()
        with pytest.raises(PWITimeout):
            await c.status()
        assert time.monotonic() - t0 < 3.0
    finally:
        await c.aclose()
        sim.stop()


def test_pwi2_moving_quirk_and_parsing():
    from robofast_core.devices.pwi_sim import CDK700Model

    m = CDK700Model(ready=True)
    st = parse_pwi2_status(pwi2_status(m))
    assert st.raw["mount.moving"] == "True"  # not tracking: PWI2 says moving
    assert st.slewing is None  # ... which means nothing
    m.set_tracking(True)
    st = parse_pwi2_status(pwi2_status(m))
    assert st.tracking and st.slewing is False
    with pytest.raises(PWIProtocolError):
        parse_pwi2_status("<html/>")
    with pytest.raises(PWIProtocolError):
        parse_pwi4_status("mount.is_connected=true\n")


# -- the server -------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
async def test_connecting_and_reading_never_commands(make, backend):
    sim, server = make(backend, ready=False)
    tel = await device(server)
    foc = await device(server, "focuser")
    rot = await device(server, "rotator")
    for member in ("Altitude", "Azimuth", "RightAscension", "Slewing", "Tracking", "AtPark"):
        await tel.get(member, timeout=T)
    await foc.get("Position", timeout=T)
    await rot.get("Position", timeout=T)
    json.loads(await tel.call("Action", "Status", "", timeout=T))
    await asyncio.sleep(0.3)  # the limit watch reads too
    assert sim.model.commands == []
    assert not sim.model.connected and sim.model.enabled == [False, False]
    # Motion is refused until connected and enabled -- explicitly.
    with pytest.raises(InvalidOperationException, match="MountConnect"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Action", "MountConnect", "", timeout=T)
    with pytest.raises(InvalidOperationException, match="MotorsEnable"):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Action", "MotorsEnable", "", timeout=T)
    await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), what="slew end")
    assert abs(await tel.get("Altitude", timeout=T) - 40.0) < 0.01
    for c in (tel, foc, rot):
        await c.aclose()


async def _not(aw):
    return not await aw


@pytest.mark.parametrize("backend", BACKENDS)
async def test_altitude_limits_are_enforced(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    for alt in (19.9, 85.1):
        with pytest.raises(InvalidOperationException, match="outside"):
            await tel.put("SlewToAltAzAsync", Altitude=alt, Azimuth=100.0, timeout=T)
    await tel.set("Tracking", True, timeout=T)
    lst = await tel.get("SiderealTime", timeout=T)
    # Below the horizon limit: hour angle 6 h at Dec -40.
    with pytest.raises(InvalidOperationException, match="outside"):
        await tel.call("SlewToCoordinatesAsync", (lst - 6.0) % 24, -40.0, timeout=T)
    # Near the zenith: above max_alt.
    with pytest.raises(InvalidOperationException, match="outside"):
        await tel.call("SlewToCoordinatesAsync", lst, SIM_LATITUDE, timeout=T)
    assert not any("goto" in c or "move" in c for c in sim.model.commands)
    ra, dec = (lst - 1.0) % 24, 20.0
    alt, _ = target_altaz(ra, dec, lst, SIM_LATITUDE)
    assert 20 < alt < 85
    await tel.call("SlewToCoordinatesAsync", ra, dec, timeout=T)
    assert await tel.get("Slewing", timeout=T)
    await until(lambda: _not(tel.get("Slewing", timeout=T)), timeout=30, what="slew end")
    assert abs(await tel.get("Declination", timeout=T) - dec) < 0.5
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_abort_park_home(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 2.0})
    tel = await device(server)
    await tel.put("SlewToAltAzAsync", Altitude=60.0, Azimuth=120.0, timeout=T)
    await asyncio.sleep(0.3)
    await tel.call("AbortSlew", timeout=T)
    alt = sim.model.state()["alt"]
    await asyncio.sleep(0.3)
    assert abs(sim.model.state()["alt"] - alt) < 1e-6  # stopped
    assert alt < 59.0
    sim.model.slew_rate = 20.0
    await tel.call("Park", timeout=T)
    await until(lambda: tel.get("AtPark", timeout=T), what="parked")
    assert not await tel.get("Tracking", timeout=T)
    with pytest.raises(ParkedException):
        await tel.put("SlewToAltAzAsync", Altitude=40.0, Azimuth=100.0, timeout=T)
    await tel.call("Unpark", timeout=T)
    await tel.put("FindHome", timeout=T)
    await until(lambda: tel.get("AtHome", timeout=T), what="home")
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_stop_action_needs_no_connection(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 1.0})
    sim.model.goto_altaz(60.0, 120.0)
    tel = AlpacaDevice(server.address, "telescope")
    with pytest.raises(NotConnectedException):
        await tel.get("Altitude", timeout=T)
    await tel.call("Action", "Stop", "", timeout=T)
    assert not sim.model.state()["slewing"]
    await tel.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_limit_watch_stops_a_mount_going_low(make, backend):
    sim, server = make(backend, sim_kw={"slew_rate": 5.0, "alt": 24.0})
    # Something else (not robofast) slews it toward the horizon.
    with sim.model.lock:
        sim.model.goto_altaz(5.0, 90.0)
    await until(lambda: _async(server.mount.watch_stops >= 1), timeout=8, what="watch stop")
    # The count goes up just before the stop is sent.
    await until(lambda: _async(not sim.model.state()["slewing"]), timeout=3, what="stopped")
    st = sim.model.state()
    assert not st["slewing"] and 17.0 < st["alt"] < 19.0


async def _async(v):
    return v


@pytest.mark.parametrize("backend", BACKENDS)
async def test_select_port_focuser_rotator(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    foc = await device(server, "focuser")
    rot = await device(server, "rotator")
    with pytest.raises(InvalidValueException):
        await tel.call("Action", "SelectPort", "3", timeout=T)
    with pytest.raises(ActionNotImplementedException):
        await tel.call("Action", "Nope", "", timeout=T)
    await foc.call("Move", 20000, timeout=T)
    await until(lambda: _not(foc.get("IsMoving", timeout=T)), what="focus")
    assert await foc.get("Position", timeout=T) == 20000
    with pytest.raises(InvalidValueException):
        await foc.call("Move", 50, timeout=T)  # below focus_min
    await rot.put("MoveMechanical", Position=45.0, timeout=T)
    await until(lambda: _not(rot.get("IsMoving", timeout=T)), what="rotator")
    assert abs(await rot.get("MechanicalPosition", timeout=T) - 45.0) < 0.01
    out = json.loads(await tel.call("Action", "SelectPort", "fau", timeout=T))
    assert out == {"port": 2, "moved": True}
    assert sim.model.state()["m3_port"] == 2
    await foc.call("Move", 21000, timeout=T)
    await until(lambda: _not(foc.get("IsMoving", timeout=T)), what="focus 2")
    assert sim.model.focusers[2].pos == 21000 and sim.model.focusers[1].pos == 20000
    if backend == "pwi4":
        # PWI4 reports only Rotator index 0 (port 1).
        with pytest.raises(Exception, match="only port 1"):
            await rot.get("MechanicalPosition", timeout=T)
    else:
        assert abs(await rot.get("MechanicalPosition", timeout=T)) < 0.01
    out = json.loads(await tel.call("Action", "SelectPort", "2", timeout=T))
    assert out == {"port": 2, "moved": False}
    for c in (tel, foc, rot):
        await c.aclose()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_offset_and_pulse_guide(make, backend):
    sim, server = make(backend)
    tel = await device(server)
    with pytest.raises(InvalidOperationException, match="tracking"):
        await tel.call("Action", "Offset", '{"east": 10}', timeout=T)
    await tel.set("Tracking", True, timeout=T)
    ra0, dec0 = sim.model.radec
    await tel.call("Action", "Offset", '{"east": 0, "north": 36}', timeout=T)
    assert abs(sim.model.radec[1] - dec0 - 0.01) < 1e-9
    with pytest.raises(InvalidValueException):
        await tel.call("Action", "Offset", '{"north": 6000}', timeout=T)
    await tel.call("PulseGuide", 1, 2000, timeout=T)  # south, 2 s at 7.5"/s
    assert await tel.get("IsPulseGuiding", timeout=T)
    assert abs(sim.model.radec[1] - dec0 - 0.01 + 15 / 3600) < 1e-9
    await tel.call("PulseGuide", 2, 1000, timeout=T)  # east: RA coordinate + 7.5"
    assert abs((sim.model.radec[0] - ra0) * 15 * 3600 - 7.5) < 1e-6
    await tel.aclose()


async def test_hung_pwi_is_bounded(make):
    sim, server = make("pwi4")
    tel = await device(server)
    sim.set_mode("hang")
    await asyncio.sleep(0.3)  # past the status cache
    t0 = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 -- the timeout, as an Alpaca error
        await tel.get("Altitude", timeout=10)
    assert time.monotonic() - t0 < 6.0
    sim.set_mode("ok")
    await tel.aclose()


# -- the jog tool ----------------------------------------------------------------------------


class _Out(list):
    def __call__(self, text: str) -> None:
        self.append(text)


async def _no(prompt: str) -> bool:
    return False


async def _yes(prompt: str) -> bool:
    return True


def jog_for(sim: PWISim, backend: str, confirm=_yes, **kw) -> tuple[Jog, _Out, PWIConfig]:
    cfg = config(sim.port, backend, **kw)
    client = make_client(backend, "127.0.0.1", sim.port, timeout=1.0)
    out = _Out()
    return Jog(cfg, client, out=out, confirm=confirm, poll=0.05), out, cfg


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_altaz_arrives(backend):
    sim = PWISim(backend, ready=True, slew_rate=2.0, alt=40.0, az=100.0).start()
    jog, out, _ = jog_for(sim, backend)
    try:
        assert await jog.altaz(0.5, -0.3)
        st = sim.model.state()
        assert abs(st["alt"] - 40.5) < 1e-3 and abs(st["az"] - 99.7) < 1e-3
        assert any(line.startswith("PLAN") for line in out) and out[-1] == "arrived"
    finally:
        await jog.pwi.aclose()
        sim.stop()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_not_confirmed_sends_nothing(backend):
    sim = PWISim(backend, ready=True).start()
    jog, _out, _ = jog_for(sim, backend, confirm=_no)
    try:
        assert not await jog.altaz(0.5, 0.0)
        assert sim.model.commands == []
    finally:
        await jog.pwi.aclose()
        sim.stop()


def test_jog_plans_refuse():
    from robofast_core.devices.pwi_client import parse_pwi4_status
    from robofast_core.devices.pwi_sim import CDK700Model, pwi4_status

    m = CDK700Model(ready=True, alt=20.5, az=359.8)
    cfg = config(1, "pwi4")
    st = parse_pwi4_status(pwi4_status(m))
    with pytest.raises(JogRefused, match="max_jog_deg"):
        plan_altaz(cfg, st, 1.5, 0)
    with pytest.raises(JogRefused, match="outside"):
        plan_altaz(cfg, st, -0.6, 0)  # 19.9 < min_alt 20
    with pytest.raises(JogRefused, match="crosses 0/360"):
        plan_altaz(cfg, st, 0.2, 0.5)
    assert plan_altaz(cfg, st, 0.5, -0.5) == (21.0, pytest.approx(359.3))
    with pytest.raises(JogRefused, match="tracking"):
        plan_radec(cfg, st, 10, 0)
    m.set_tracking(True)
    st = parse_pwi4_status(pwi4_status(m))
    with pytest.raises(JogRefused, match="tracking is on"):
        plan_altaz(cfg, st, 0.1, 0)
    assert plan_radec(cfg, st, 0, 36) == pytest.approx(0.01)
    m2 = CDK700Model(ready=False)
    with pytest.raises(JogRefused, match="not connected"):
        plan_altaz(cfg, parse_pwi4_status(pwi4_status(m2)), 0.1, 0)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_jog_stops_a_runaway(backend):
    """PWI goes further than planned: the jog sends stop."""
    sim = PWISim(backend, ready=True, slew_rate=3.0, alt=40.0, az=100.0).start()
    jog, out, _ = jog_for(sim, backend)
    real = jog.pwi.goto_altaz

    async def wrong(alt, az):  # as if PWI took the long way round
        return await real(alt + 5.0, az)

    jog.pwi.goto_altaz = wrong  # type: ignore[method-assign]
    try:
        assert not await jog.altaz(0.5, 0.0)
        assert any(line.startswith("STOP") for line in out)
        st = sim.model.state()
        assert not st["slewing"] and st["alt"] < 41.5
    finally:
        await jog.pwi.aclose()
        sim.stop()


async def test_jog_cancel_sends_stop():
    sim = PWISim("pwi4", ready=True, slew_rate=0.5, alt=40.0, az=100.0).start()
    jog, out, _ = jog_for(sim, "pwi4")
    try:
        task = asyncio.ensure_future(jog.altaz(1.0, 0.0))
        await wait_until(lambda: sim.model.state()["slewing"], timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "STOP (interrupted)" in out
        assert not sim.model.state()["slewing"]
    finally:
        await jog.pwi.aclose()
        sim.stop()


def test_jog_cli_status_and_stop(tmp_path, capsys):
    sim = PWISim("pwi2", ready=True).start()
    try:
        p = tmp_path / "c.json"
        cfg = json.loads((ROOT / "sites/minerva/remote/t1_pwi2.json").read_text())
        cfg["pwi"]["port"] = sim.port
        cfg["allow_hardware"] = False  # a simulator needs no gate
        p.write_text(json.dumps(cfg))
        log = tmp_path / "jog.log"
        for args in (["status"], ["stop"]):
            with pytest.raises(SystemExit) as e:
                jog_main(["--config", str(p), "--log", str(log), *args])
            assert e.value.code == 0
        assert sim.model.commands == ["mount:stop"]
        text = log.read_text()
        assert "STOP (requested)" in text and "alt " in text
    finally:
        sim.stop()


def test_pwi4_client_and_pwi2_client_are_backends():
    assert PWI4Client.backend == "pwi4" and PWI2Client.backend == "pwi2"
