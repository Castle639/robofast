"""The hardware rehearsal (robofast.testing.rehearsal; docs/demo.md) on
fake mounts and cameras served over Alpaca on loopback *as if* they were the
real ones: separate servers the rehearsal only knows by address, with a
faster clock (test only). It covers the external-endpoint path, the mount
guard, the end action and the checks without hardware."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast.testing.alpaca_server import FakeAlpacaServer
from robofast.testing.fakes import FakeCamera, FakeDevice, FakeFocuser, FakeTelescope
from robofast.testing.mount_guard import Endpoint
from robofast.testing.rehearsal import (
    EndAction,
    RealTelescope,
    RehearsalSettings,
    check,
    rehearse,
    summary,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(900)]


class Mount(FakeTelescope):
    """A fake mount that can also slew to alt/az (the end action) and counts
    every Park and Unpark it is sent (the rehearsal must send none)."""

    CanSlewAltAzAsync = True

    def __init__(self) -> None:
        super().__init__(slew_time=0.5)
        self.parks = 0
        self.unparks_sent = 0

    def Park(self) -> None:
        self.parks += 1
        super().Park()

    def Unpark(self) -> None:
        self.unparks_sent += 1
        super().Unpark()

    def SlewToAltAzAsync(self, az: float, alt: float) -> None:
        if self._tracking:
            raise RuntimeError("SlewToAltAz while tracking")
        self.Altitude, self.Azimuth = alt, az
        self._slew_until = self._now() + self._slew_time


class Rotator(FakeDevice):
    def __init__(self) -> None:
        super().__init__()
        self.Position = 12.0
        self.MechanicalPosition = 30.0


def serve_real(name: str) -> tuple[Mount, FakeCamera, list[FakeAlpacaServer]]:
    mount = Mount()
    mount._parked = False  # as the operator leaves it: unparked
    mount_srv = FakeAlpacaServer(
        {"telescope": mount, "focuser": FakeFocuser(), "rotator": Rotator()}
    ).start()
    cam = FakeCamera(256, 256)
    cam_srv = FakeAlpacaServer({"camera": cam}).start()
    return mount, cam, [mount_srv, cam_srv]


async def test_a_rehearsal_on_fake_real_hardware(tmp_path):
    out = Path(os.environ.get("ROBOFAST_E2E_OUT") or tmp_path) / "rehearsal"
    servers = []
    real, mounts, cams = {}, {}, {}
    for name in ("t1", "t2"):
        mount, cam, srvs = serve_real(name)
        servers += srvs
        mounts[name], cams[name] = mount, cam
        ep = Endpoint.parse(f"{srvs[0].address}/0")
        real[name] = RealTelescope(ep, Endpoint.parse(f"{srvs[1].address}/0"), ep, ep)
    settings = RehearsalSettings(
        real=real,
        end_action=EndAction.parse("altaz:60,180"),
        duration=3600.0,
        exptime=2.0,
        images=1,
        image_exptime=0.5,
        spectra=3,
        console_port=None,
        idle_poll=1.0,
        # Test only: a dark night, ten times faster than real time.
        start=datetime(2026, 11, 10, 4, 0, tzinfo=UTC),
        scale=10.0,
    )
    try:
        outcome = await rehearse(out, settings)
    finally:
        for s in servers:
            s.stop()
    print(summary(outcome))
    problems = check(outcome)
    assert not problems, "\n".join(problems)
    for name, mount in mounts.items():
        # robofast's parks became "tracking off" at the guard; nothing else.
        assert mount.parks == 0 and mount.unparks_sent == 0, name
        assert not mount.Tracking
        assert (mount.Altitude, mount.Azimuth) == (60.0, 180.0)
        assert cams[name].exposures_started >= 2, name
    assert all(c == "COMPLETED" for c in outcome.statuses.values()), outcome.statuses
    assert len(outcome.slews) >= 4 and all(r["error_arcsec"] < 1 for r in outcome.slews)
    assert outcome.why_end == "every request done"
