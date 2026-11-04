"""KiwiSpec's auxiliary devices driven directly: the drivers, their
simulators, the Alpaca server, and the KiwiSpec service using it.

Fast tier: every device is a simulator, in process (``"url": "sim"``) or
on a registered loopback TCP port reached through pyserial's
``socket://`` URL (the same code path as an ``rfc2217://`` bridge). No
serial port is opened, nothing leaves the host.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time

import httpx
import pytest

from robofast_core.alpaca_client import (
    AlpacaDevice,
    InvalidOperationException,
    InvalidValueException,
    NotImplementedException,
)
from robofast_core.devices import dynamixel2 as dx
from robofast_core.devices import thorlabs_apt as apt
from robofast_core.devices.kiwispec_aux import AuxConfig
from robofast_core.devices.kiwispec_aux_server import KiwiSpecAuxConfig, KiwiSpecAuxServer
from robofast_core.devices.kiwispec_aux_sim import (
    APTSim,
    GaugeSim,
    LEDSim,
    OasisSim,
    TC200Sim,
    XMServoSim,
    serve,
)
from robofast_core.devices.kiwispec_serial import (
    BacklightLED,
    GaugeController,
    OasisChiller,
    SerialDeviceError,
)
from robofast_core.devices.kiwispec_service import KiwiSpecServer
from robofast_core.devices.kiwispec_sim import KiwiSpecSim, SimConfig
from robofast_core.devices.serial_url import (
    HardwareNotAllowed,
    PortConfig,
    PortTimeout,
    SerialURLPort,
    SimPort,
    check_url,
    open_port,
)
from robofast_core.devices.tc200 import TC200, TC200Error
from robofast_core.testing.checks import wait_until
from robofast_core.testing.processes import ROOT
from tests.core.test_kiwispec import FAST, run_job, service_config

serial = pytest.importorskip("serial")  # pyserial: the URL path

STAGE = {"in": 11.0, "flat": 68.0, "out": 102.0}
CPM = 25600.0


# -- the hardware gate -----------------------------------------------------------------


def test_gate_refuses_anything_that_may_be_hardware():
    with pytest.raises(HardwareNotAllowed, match="not loopback"):
        check_url("rfc2217://192.0.2.10:7001", allow_hardware=False, what="heater")
    with pytest.raises(HardwareNotAllowed, match="local serial port"):
        check_url("/dev/ttyUSB0", allow_hardware=False, what="heater")
    # Loopback alone is not enough: nothing verified listens there.
    with pytest.raises(HardwareNotAllowed):
        check_url("socket://127.0.0.1:9", allow_hardware=False, what="heater")
    assert check_url("sim", allow_hardware=False, what="x") == "sim"
    assert check_url("loop://", allow_hardware=False, what="x") == "loop://"
    # With the gate open the URL is used as given (resolved to an IP literal).
    assert check_url("rfc2217://192.0.2.10:7001", allow_hardware=True, what="h").startswith(
        "rfc2217://192.0.2.10:7001"
    )


def test_server_refuses_a_real_port_without_the_gate():
    cfg = KiwiSpecAuxConfig.from_dict(
        {"listen": "127.0.0.1:0", "heater": {"port": {"url": "rfc2217://192.0.2.10:7001"}}}
    )
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecAuxServer(cfg)
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecAuxServer(cfg, hardware=True)  # the config does not allow it


async def test_a_registered_simulator_passes_the_gate_through_pyserial():
    sims = serve(["heater"])
    try:
        url = check_url(sims.url("heater"), allow_hardware=False, what="heater")
        port = SerialURLPort(url, baud=115200, name="heater")
        try:
            tc = TC200(port)
            assert (await tc.status()).enabled
            assert await tc.temperature() == pytest.approx(54.6, abs=0.5)
        finally:
            port.close()
    finally:
        sims.close()


# -- the iodine cell heater ------------------------------------------------------------


async def test_heater_reads_and_verifies_the_set_point():
    sim = TC200Sim()
    tc = TC200(SimPort(sim), quiet=0.02)
    s = await tc.status()
    assert s.enabled and s.unit == "C" and not s.alarm
    assert await tc.setpoint() == 55.0
    assert await tc.set_setpoint(54.5) == 54.5
    assert sim.setpoint == 54.5
    with pytest.raises(TC200Error, match="CMD_ARG_INVALID"):
        await tc.set_setpoint(500.0)
    assert "*idn?" not in sim.commands


async def test_heater_enable_reads_status_before_the_toggle():
    sim = TC200Sim(enabled=True)
    tc = TC200(SimPort(sim), quiet=0.02)
    await tc.set_enabled(True)  # already on: no toggle
    assert "ens" not in sim.commands
    await tc.set_enabled(False)
    assert sim.commands[-3:] == ["stat?", "ens", "stat?"] and not sim.enabled
    await tc.set_enabled(False)
    assert sim.commands.count("ens") == 1


async def test_heater_toggle_that_does_not_take_is_not_repeated():
    sim = TC200Sim(enabled=False)
    sim.ens_broken = True
    tc = TC200(SimPort(sim), quiet=0.02)
    with pytest.raises(TC200Error, match="not toggling again"):
        await tc.set_enabled(True)
    assert sim.commands.count("ens") == 1


async def test_heater_silent_fails_within_its_bound():
    sim = TC200Sim()
    sim.silent = True
    tc = TC200(SimPort(sim), timeout=0.2, quiet=0.02)
    t0 = time.monotonic()
    with pytest.raises(PortTimeout):
        await tc.temperature()
    assert time.monotonic() - t0 < 1.0


async def test_heater_refuses_commands_off_its_allowlist():
    tc = TC200(SimPort(TC200Sim()))
    with pytest.raises(TC200Error, match="refusing"):
        await tc._ask("tmax=200")


# -- the iodine stage (Thorlabs APT) --------------------------------------------------------


def test_apt_message_layout():
    # MGMSG_MOT_MOVE_ABSOLUTE, 6 data bytes, dest 0x50 | 0x80, source 0x01.
    msg = apt.long_message(apt.MOT_MOVE_ABSOLUTE, struct.pack("<Hi", 1, 1000))
    assert msg.hex() == "53040600d0010100e8030000"
    assert apt.short_message(apt.MOT_MOVE_HOME, 1).hex() == "430401005001"


def apt_stage(sim: APTSim, **kw) -> apt.APTStage:
    cfg = apt.APTConfig(
        counts_per_mm=CPM, poll_interval=0.02, status_max_age=0.0, timeout=0.5, **kw
    )
    stage = apt.APTStage(SimPort(sim), cfg)
    stage.start()
    return stage


async def test_apt_identifies_and_moves():
    sim = APTSim(counts_per_mm=CPM, position=11.0, velocity=200.0)
    stage = apt_stage(sim)
    try:
        await stage.connect()
        assert stage.info is not None and stage.info["model"] == "BSC101"
        assert await stage.position_mm() == pytest.approx(11.0)
        await stage.move_mm(68.0)
        assert await stage.moving()
        await wait_until(lambda: stage.job is None, 3.0)
        assert await stage.position_mm() == pytest.approx(68.0)
        assert stage.fault is None
        assert apt.MOT_MOVE_COMPLETED in [e for _, e in stage.events]
        with pytest.raises(ValueError, match="outside the travel"):
            await stage.move_mm(151.0)
        assert set(sim.commands) <= apt.SENT
    finally:
        await stage.shutdown()


async def test_apt_status_bits_query_for_older_firmware():
    sim = APTSim(counts_per_mm=CPM, position=102.0, status_query="bits")
    stage = apt_stage(sim, status_query="bits")
    try:
        await stage.connect()
        assert await stage.position_mm() == pytest.approx(102.0)
        assert apt.MOT_REQ_STATUSUPDATE not in sim.commands
    finally:
        await stage.shutdown()


async def test_apt_refuses_moves_until_homed_and_homing_moves_to_zero_first():
    sim = APTSim(counts_per_mm=CPM, homed=False, home_time=0.3, velocity=500.0)
    stage = apt_stage(sim)
    try:
        await stage.connect()
        with pytest.raises(RuntimeError, match="not homed"):
            await stage.move_mm(11.0)
        await stage.home()
        await wait_until(lambda: sim.homed and stage.job is None, 3.0)
        await stage.move_mm(11.0)
        await wait_until(lambda: stage.job is None, 3.0)
        # Homed and away from 0: homing first moves to 0 (legacy), then homes.
        sim.commands.clear()
        await stage.home()
        assert await stage.moving()
        await wait_until(lambda: not stage._pending_home or stage._pending_home.done(), 5.0)
        await wait_until(lambda: stage.job is None, 5.0)
        moves = [c for c in sim.commands if c in (apt.MOT_MOVE_ABSOLUTE, apt.MOT_MOVE_HOME)]
        assert moves == [apt.MOT_MOVE_ABSOLUTE, apt.MOT_MOVE_HOME]
        assert sim.position_mm == 0.0 and stage.fault is None
    finally:
        await stage.shutdown()


async def test_apt_stuck_move_is_stopped_and_reported():
    sim = APTSim(counts_per_mm=CPM, velocity=10.0)
    sim.stuck = True
    stage = apt_stage(sim, move_timeout=0.3)
    try:
        await stage.connect()
        await stage.move_mm(100.0)
        await wait_until(lambda: stage.job is None, 3.0)
        assert "did not finish within 0.3 s" in (stage.fault or "")
        assert apt.MOT_MOVE_STOP in sim.commands
    finally:
        await stage.shutdown()


async def test_apt_dead_controller_fails_with_a_clear_error():
    sim = APTSim()
    sim.silent = True
    stage = apt_stage(sim)
    with pytest.raises(PortTimeout, match="not an APT controller"):
        await stage.connect()


# -- the backlight (Dynamixel + LED) ---------------------------------------------------------


def test_dynamixel_packets_match_legacy():
    # Legacy com.py's literal packets: goal position 1536 (in) and 1024 (out).
    goal = struct.pack("<H", dx.ADDR_GOAL_POSITION)
    assert dx.packet(1, dx.WRITE, goal + struct.pack("<i", 1536)).hex().upper() == (
        "FFFFFD00010900037400000600009909"
    )
    assert dx.packet(1, dx.WRITE, goal + struct.pack("<i", 1024)).hex().upper() == (
        "FFFFFD0001090003740000040000B289"
    )
    assert dx.degrees_to_counts(-45) == 1535 and dx.degrees_to_counts(-90) == 1023
    assert dx.unstuff(dx.stuff(b"\x01\xff\xff\xfd\x02")) == b"\x01\xff\xff\xfd\x02"


async def test_servo_refuses_writes_outside_its_table():
    servo = dx.XMServo(SimPort(XMServoSim()), 1)
    with pytest.raises(dx.ServoError, match="refusing"):
        await servo.write(7, b"\x02")  # the ID
    assert (await servo.ping())["model"] == XMServoSim.MODEL


async def test_silent_servo_names_what_to_check():
    sim = XMServoSim()
    sim.silent = True
    servo = dx.XMServo(SimPort(sim, name="backlight servo"), 1, timeout=0.2)
    with pytest.raises(dx.ServoTimeout, match=r"Dynamixel ID 1 did not answer PING.*57600"):
        await servo.ping()


async def test_led_switches_and_reads_nothing_back():
    sim = LEDSim()
    led = BacklightLED(SimPort(sim), timeout=0.05, quiet=0.02)
    await led.set(True)
    assert sim.on and led.on
    await led.set(False)
    assert sim.commands == ["n8", "f8"]


# -- vacuum and chiller: read only -------------------------------------------------------------


async def test_gauges_send_only_their_two_queries():
    sim = GaugeSim(pump=1.0e3, spec=2.3e-3)
    g = GaugeController(SimPort(sim))
    assert await g.pressure(2) == pytest.approx(2.3e-3)
    assert await g.pressure(1) == pytest.approx(1.0e3)
    assert sim.commands == ["#  RDCG2", "#  RDCG1"]
    with pytest.raises(SerialDeviceError, match="refusing"):
        await g.ask("#  SV")  # anything but a gauge read
    with pytest.raises(ValueError):
        await g.pressure(3)


async def test_gauge_error_answer_is_an_error():
    sim = GaugeSim(address="01")
    g = GaugeController(SimPort(sim))  # wrong address: the controller says '?'
    with pytest.raises(SerialDeviceError):
        await g.pressure(2)
    g = GaugeController(SimPort(sim), address="01")
    assert await g.pressure(2) == pytest.approx(2.3e-3)


async def test_chiller_reading_and_faults():
    sim = OasisSim(temp=15.4, setpoint=15.0, faults=0b0100)
    c = OasisChiller(SimPort(sim))
    r = await c.read()
    assert r["temp"] == 15.4 and r["setpoint"] == 15.0
    assert r["status"]["run"] and r["faults"]["pump_fail"]
    assert r["fault"] == "pump_fail"
    assert set(sim.commands) <= OasisChiller.allowed
    with pytest.raises(SerialDeviceError):
        await c.ask("SETTEMP 10")


# -- the Alpaca server ---------------------------------------------------------------------


def aux_config(**over) -> KiwiSpecAuxConfig:
    d = {
        "name": "test",
        "listen": "127.0.0.1:0",
        "request_timeout": 4.0,
        "heater": {"port": "sim"},
        "stage": {
            "port": {"url": "sim", "sim": {"counts_per_mm": CPM, "velocity": 300.0}},
            "positions": STAGE,
            "counts_per_mm": CPM,
            "poll_interval": 0.02,
            "status_max_age": 0.0,
        },
        "backlight": {"led_port": "sim", "servo_port": "sim", "max_on": 60.0},
        "telemetry": {"gauges": "sim", "chiller": "sim", "max_age": 0.0},
        **over,
    }
    return KiwiSpecAuxConfig.from_dict(d)


@pytest.fixture
def aux_server():
    server = KiwiSpecAuxServer(aux_config()).start()
    yield server
    server.stop()


async def connected(server: KiwiSpecAuxServer, kind: str, n: int = 0) -> AlpacaDevice:
    dev = AlpacaDevice(server.address, kind, n)
    await dev.connect(timeout=5)
    return dev


async def test_server_lists_its_devices(aux_server):
    assert aux_server.devices == ("switch", "filterwheel", "covercalibrator", "switch/1")
    async with httpx.AsyncClient() as c:
        r = await c.get(f"http://{aux_server.address}/management/v1/configureddevices")
    kinds = [(d["DeviceType"], d["DeviceNumber"]) for d in r.json()["Value"]]
    assert kinds == [("Switch", 0), ("FilterWheel", 0), ("CoverCalibrator", 0), ("Switch", 1)]


async def test_heater_switch(aux_server):
    sim = aux_server.sim("heater")
    sw = await connected(aux_server, "switch", 0)
    t = 5
    names = [await sw.get("GetSwitchName", timeout=t, Id=i) for i in range(4)]
    assert names == ["heater_enabled", "heater_setpoint", "cell_temperature", "heater_alarm"]
    assert await sw.get("GetSwitch", timeout=t, Id=0) is True
    assert await sw.get("GetSwitchValue", timeout=t, Id=2) == pytest.approx(54.6, abs=0.5)
    await sw.put("SetSwitchValue", timeout=t, Id=1, Value=54.0)
    assert sim.setpoint == 54.0
    with pytest.raises(InvalidValueException):
        await sw.put("SetSwitchValue", timeout=t, Id=1, Value=90.0)  # above setpoint_max
    await sw.put("SetSwitch", timeout=t, Id=0, State=False)
    assert not sim.enabled
    assert sim.commands.count("ens") == 1
    with pytest.raises(NotImplementedException):
        await sw.put("SetSwitchValue", timeout=t, Id=2, Value=50.0)
    assert await sw.get("CanWrite", timeout=t, Id=3) is False
    await sw.aclose()


async def test_stage_filterwheel(aux_server):
    sim = aux_server.sim("stage")
    fw = await connected(aux_server, "filterwheel")
    t = 5
    assert await fw.get("Names", timeout=t) == ["in", "flat", "out"]
    assert await fw.get("Position", timeout=t) == 0
    await fw.put("Position", timeout=t, Position=2)
    assert await fw.get("Position", timeout=t) == -1
    for _ in range(100):
        if await fw.get("Position", timeout=t) == 2:
            break
        await asyncio.sleep(0.05)
    assert sim.position_mm == pytest.approx(102.0)
    status = json.loads(await fw.put("Action", timeout=t, Action="Status", Parameters=""))
    assert status["name"] == "out" and status["homed"]
    await fw.put("Action", timeout=t, Action="MoveMM", Parameters="50")
    await asyncio.sleep(0.5)
    with pytest.raises(InvalidOperationException, match="not at a named position"):
        await fw.get("Position", timeout=t)
    with pytest.raises(InvalidValueException):
        await fw.put("Action", timeout=t, Action="MoveMM", Parameters="200")
    await fw.aclose()


async def test_stage_without_a_confirmed_scale_is_refused():
    cfg = aux_config(
        stage={"port": "sim", "positions": STAGE, "counts_per_mm": None},
        backlight=None,
        telemetry=None,
        heater=None,
    )
    server = KiwiSpecAuxServer(cfg).start()
    try:
        fw = await connected(server, "filterwheel")
        with pytest.raises(InvalidOperationException, match="counts_per_mm"):
            await fw.get("Position", timeout=5)
        with pytest.raises(InvalidOperationException, match="counts_per_mm"):
            await fw.put("Position", timeout=5, Position=0)
        status = json.loads(await fw.put("Action", timeout=5, Action="Status", Parameters=""))
        assert status["position_mm"] is None and isinstance(status["counts"], int)
        await fw.aclose()
    finally:
        server.stop()


async def test_backlight_covercalibrator(aux_server):
    led, servo = aux_server.sim("backlight LED"), aux_server.sim("backlight servo")
    cc = await connected(aux_server, "covercalibrator")
    t = 5
    assert led.commands == ["f8"]  # the LED off on connect
    assert await cc.get("CoverState", timeout=t) == 3  # open (arm out)
    await cc.put("CloseCover", timeout=t)
    for _ in range(100):
        if await cc.get("CoverState", timeout=t) == 1:
            break
        await asyncio.sleep(0.02)
    assert servo.position == dx.degrees_to_counts(-45) and servo.torque
    await cc.put("CalibratorOn", timeout=t, Brightness=1)
    assert led.on and await cc.get("CalibratorState", timeout=t) == 3
    with pytest.raises(InvalidValueException):
        await cc.put("CalibratorOn", timeout=t, Brightness=2)
    await cc.put("CalibratorOff", timeout=t)
    await cc.put("OpenCover", timeout=t)
    for _ in range(100):
        if await cc.get("CoverState", timeout=t) == 3:
            break
        await asyncio.sleep(0.02)
    assert servo.position == dx.degrees_to_counts(-90) and not servo.torque
    # Legacy's order: torque off, position mode, torque on, goal.
    writes = [p[:2] for inst, p in servo.commands if inst == dx.WRITE]
    addrs = [struct.unpack("<H", w)[0] for w in writes]
    assert addrs[:4] == [64, 11, 64, 116]
    with pytest.raises(NotImplementedException):
        await cc.put("HaltCover", timeout=t)
    await cc.aclose()


async def test_backlight_failsafe_turns_the_led_off_and_the_arm_out():
    server = KiwiSpecAuxServer(
        aux_config(backlight={"led_port": "sim", "servo_port": "sim", "max_on": 0.6})
    ).start()
    try:
        led = server.sim("backlight LED")
        cc = await connected(server, "covercalibrator")
        await cc.put("CloseCover", timeout=5)
        await asyncio.sleep(0.3)
        await cc.put("CalibratorOn", timeout=5, Brightness=1)
        assert led.on
        await wait_until(lambda: not led.on, 3.0)
        for _ in range(100):
            if await cc.get("CoverState", timeout=5) == 3:
                break
            await asyncio.sleep(0.05)
        assert await cc.get("CalibratorState", timeout=5) == 1
        await cc.aclose()
    finally:
        server.stop()


async def test_dead_servo_fails_cleanly_and_the_led_still_works():
    server = KiwiSpecAuxServer(
        aux_config(
            backlight={"led_port": "sim", "servo_port": "sim", "servo_timeout": 0.2},
            heater=None,
            stage=None,
            telemetry=None,
        )
    )
    server.sim("backlight servo").silent = True
    server.start()
    try:
        cc = await connected(server, "covercalibrator")
        assert await cc.get("CoverState", timeout=5) == 5  # Error
        with pytest.raises(Exception, match="Dynamixel ID 1 did not answer"):
            await cc.put("CloseCover", timeout=5)
        await cc.put("CalibratorOn", timeout=5, Brightness=1)
        assert server.sim("backlight LED").on
        status = json.loads(await cc.put("Action", timeout=5, Action="Status", Parameters=""))
        assert "did not answer" in status["cover_error"]
        await cc.aclose()
    finally:
        server.stop()


async def test_telemetry_is_read_only(aux_server):
    sw = await connected(aux_server, "switch", 1)
    t = 5
    n = await sw.get("MaxSwitch", timeout=t)
    names = [await sw.get("GetSwitchName", timeout=t, Id=i) for i in range(n)]
    assert names[:2] == ["spec_pressure", "pump_pressure"]
    assert "chiller_temperature" in names and "chiller_fault" in names
    assert await sw.get("GetSwitchValue", timeout=t, Id=0) == pytest.approx(2.3e-3)
    for i in range(n):
        assert await sw.get("CanWrite", timeout=t, Id=i) is False
        with pytest.raises(NotImplementedException):
            await sw.put("SetSwitch", timeout=t, Id=i, State=False)
    status = json.loads(await sw.put("Action", timeout=t, Action="Status", Parameters=""))
    assert status["chiller"]["temp"] == 15.0
    gauges = aux_server.sim("vacuum gauges")
    assert set(gauges.commands) <= {"#  RDCG1", "#  RDCG2"}
    await sw.aclose()


async def test_server_over_pyserial_urls():
    """The whole server with every port a ``socket://`` URL to a registered
    loopback simulator (the path a ``rfc2217://`` bridge takes)."""
    sims = serve(params={"stage": {"counts_per_mm": CPM, "velocity": 300.0}})
    try:
        u = sims.urls()
        cfg = aux_config(
            heater={"port": u["heater"]},
            stage={
                "port": {"url": u["stage"]},
                "positions": STAGE,
                "counts_per_mm": CPM,
                "poll_interval": 0.02,
            },
            backlight={"led_port": u["led"], "servo_port": u["servo"]},
            telemetry={"gauges": u["gauges"], "chiller": u["chiller"]},
        )
        server = KiwiSpecAuxServer(cfg).start()
        try:
            assert server.registration is None  # its ports are not all in process
            sw = await connected(server, "switch", 0)
            assert await sw.get("GetSwitchValue", timeout=5, Id=1) == 55.0
            fw = await connected(server, "filterwheel")
            assert await fw.get("Position", timeout=5) == 0
            cc = await connected(server, "covercalibrator")
            assert await cc.get("CoverState", timeout=5) == 3
            tel = await connected(server, "switch", 1)
            assert await tel.get("GetSwitchValue", timeout=5, Id=1) == pytest.approx(1.0e3)
            for d in (sw, fw, cc, tel):
                await d.aclose()
        finally:
            server.stop()
    finally:
        sims.close()


def test_port_config_and_the_simulator_flag():
    pc = PortConfig.from_dict("rfc2217://spectrograph-pc:7001", baud=115200, what="heater")
    assert pc.url.startswith("rfc2217://") and pc.baud == 115200 and not pc.is_sim
    with pytest.raises(ValueError, match="unknown keys"):
        PortConfig.from_dict({"url": "sim", "parity": "E"}, baud=9600, what="x")
    port = open_port(PortConfig(), TC200Sim, allow_hardware=False, what="heater")
    assert isinstance(port, SimPort)


def test_minerva_example_config_loads_and_needs_the_gate():
    path = ROOT / "sites" / "minerva" / "kiwispec_aux.example.json"
    cfg = KiwiSpecAuxConfig.from_dict(json.loads(path.read_text()))
    assert cfg.stage is not None and list(cfg.stage.positions) == ["in", "flat", "out"]
    assert all(pc.url.startswith("rfc2217://") for _, pc in cfg.ports())
    assert not cfg.allow_hardware
    with pytest.raises(HardwareNotAllowed):
        KiwiSpecAuxServer(cfg)


def test_no_valve_or_pump_control_anywhere():
    """The aux devices can read the vacuum, never control it (docs)."""
    import robofast_core.devices.kiwispec_serial as ks

    assert GaugeController(SimPort(GaugeSim())).allowed == {"#  RDCG1", "#  RDCG2"}
    source = (ROOT / "robofast_core" / "devices" / "kiwispec_aux_server.py").read_text().lower()
    for word in ("ventvalve", "pumpvalve", "vent_valve", "pump_valve"):
        assert word not in source
    assert not any(c.startswith(("SETTEMP ", "RUN", "STOP")) for c in ks.OasisChiller.allowed)


# -- the KiwiSpec service through the aux server ----------------------------------------------


def test_aux_config():
    cfg = AuxConfig.from_dict({"address": "127.0.0.1:11170", "heater": None})
    assert cfg.heater is None and cfg.telemetry == 1 and cfg.stage
    with pytest.raises(ValueError, match="address"):
        AuxConfig.from_dict({"heater": 0})
    with pytest.raises(ValueError, match="unknown keys"):
        AuxConfig.from_dict({"address": "x:1", "valves": True})


@pytest.fixture
async def legacy_sim():
    s = await KiwiSpecSim(SimConfig(**FAST)).start()
    yield s
    await s.stop()


async def kiwispec_with_aux(legacy_sim, aux_server, **aux):
    cfg = service_config(
        legacy_sim,
        flux_source="none",
        aux={"address": aux_server.address, "chiller": True, "timeout": 3.0, **aux},
    )
    return await KiwiSpecServer(cfg).start(status_loop=False)


AUX_ROUTED = {
    "cell_heater_temp",
    "cell_heater_get_set_temp",
    "cell_heater_set_temp",
    "cell_heater_on",
    "cell_heater_off",
    "i2stage_get_pos",
    "i2stage_move",
    "i2stage_movef",
    "get_spec_pressure",
    "get_pump_pressure",
    "backlight_on",
    "backlight_off",
}


async def test_service_uses_the_aux_devices_and_writes_their_header(legacy_sim, aux_server):
    service = await kiwispec_with_aux(legacy_sim, aux_server)
    heater = aux_server.sim("heater")
    stage = aux_server.sim("stage")
    stage.travel = (0.0, 150.0)
    try:
        async with httpx.AsyncClient(base_url=service.url, timeout=20) as http:
            r = await http.post("/configure", json={"iodine": "out"})
            assert r.status_code == 200, r.text
            assert stage.position_mm == pytest.approx(102.0)
            job = await run_job(
                http,
                "/expose",
                {"kind": "science", "exptime": 0.3, "object": "HD1", "telescopes": ["T1"]},
            )
            assert job["state"] == "done", job
            res = job["results"][0]
            assert res["iodine"]["name"] == "in" and stage.position_mm == pytest.approx(11.0)
            header = legacy_sim.files[res["file"]]["header"]
            assert header["I2TEMPA"][0] == pytest.approx(heater.temp, abs=0.5)
            assert header["I2TEMPS"][0] == 55.0
            assert header["I2POSAS"][0] == "in" and header["I2POSAF"][0] == pytest.approx(11.0)
            r = await http.post("/backlight", json={"on": True})
            assert r.status_code == 200, r.text
            assert aux_server.sim("backlight LED").on
            r = await http.post("/backlight", json={"on": False})
            assert r.status_code == 200, r.text
            assert not aux_server.sim("backlight LED").on
            await service.kiwispec.refresh()
            st = (await http.get("/status")).json()
            assert st["readings"]["spec_pressure"]["value"] == pytest.approx(2.3e-3)
            assert st["readings"]["chiller"]["value"]["temp"] == 15.0
            assert st["readings"]["heater_setpoint"]["value"] == 55.0
        # None of the aux devices' commands went to the legacy server.
        assert not set(legacy_sim.commands) & AUX_ROUTED
    finally:
        await service.stop()


async def test_failed_heater_read_writes_unknown_not_nothing(legacy_sim, aux_server):
    service = await kiwispec_with_aux(legacy_sim, aux_server, timeout=1.5)
    try:
        aux_server.sim("heater").silent = True
        async with httpx.AsyncClient(base_url=service.url, timeout=30) as http:
            job = await run_job(
                http, "/expose", {"kind": "bias", "exptime": 0.0, "object": "bias"}, wait=25
            )
            assert job["state"] == "done", job
            res = job["results"][0]
            header = legacy_sim.files[res["file"]]["header"]
            assert header["I2TEMPA"][0] == "UNKNOWN"
            assert header["I2TEMPS"][0] == "UNKNOWN"
            assert any("UNKNOWN" in w for w in res["warnings"])
    finally:
        await service.stop()


async def test_legacy_headers_carry_both_heater_keys(legacy_sim):
    """Without aux, every frame (not only science) gets I2TEMPA and I2TEMPS."""
    service = await KiwiSpecServer(service_config(legacy_sim, flux_source="none")).start(
        status_loop=False
    )
    try:
        async with httpx.AsyncClient(base_url=service.url, timeout=20) as http:
            job = await run_job(http, "/expose", {"kind": "dark", "exptime": 0.1, "object": "d"})
            header = legacy_sim.files[job["results"][0]["file"]]["header"]
            assert header["I2TEMPS"][0] == legacy_sim.heater_setpoint
            assert isinstance(header["I2TEMPA"][0], float)
    finally:
        await service.stop()
