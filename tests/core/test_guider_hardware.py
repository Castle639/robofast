"""The guider's hardware backends are registries (robofast/guider/hardware.py)."""

from __future__ import annotations

import pytest

from robofast.guider import hardware as hw
from robofast.guider.base import RealClock
from robofast.guider.service import GuiderServiceConfig, build_parts


def parts(**cfg):
    return build_parts(GuiderServiceConfig.from_dict(cfg), allowed=False, clock=RealClock())


def test_core_registers_its_backends():
    assert {"sim", "alpaca", "zwo", "framegrabber"} <= set(hw.CAMERAS.names())
    assert {"sim", "sxao", "sxao-sim", "pi", "pi-sim"} <= set(hw.TIPTILTS.names())
    assert {"sim", "alpaca"} <= set(hw.MOUNTS.names())
    assert "sim" in hw.BACKLIGHTS
    # A spectrograph's backlight is registered by the plug-in that drives it.
    assert hw.BACKLIGHTS.origin("kiwispec") == "robofast.devices"


def test_an_unknown_kind_lists_the_registered_ones():
    with pytest.raises(ValueError, match=r"camera\.kind: guide camera kind 'nope' is not one of"):
        parts(camera={"kind": "nope"})
    with pytest.raises(ValueError, match=r"tiptilt.*'kind' is required"):
        parts(tiptilt={"port": "x"})


def test_a_plug_in_adds_a_camera_and_a_backlight():
    made = []

    def camera(spec, ctx):
        made.append(spec)
        return hw.CAMERAS["sim"]({}, ctx)

    hw.CAMERAS.register("test.cam", camera)
    hw.BACKLIGHTS.register("test.light", lambda spec, ctx: ("light", spec))
    try:
        p = parts(camera={"kind": "test.cam", "gain": 3}, backlight={"kind": "test.light", "x": 1})
        assert made == [{"gain": 3}] and p.world is not None
        assert p.backlight == ("light", {"x": 1})
        with pytest.raises(ValueError, match="registered twice"):
            hw.CAMERAS.register("test.cam", camera)
    finally:
        hw.CAMERAS.unregister("test.cam")
        hw.BACKLIGHTS.unregister("test.light")


def test_the_earlier_backlight_form_still_works():
    assert hw.spec_kind({"kiwispec": "http://h:1"}, "backlight") == (
        "kiwispec",
        {"url": "http://h:1"},
    )
    assert hw.spec_kind({"sim": True}, "backlight") == ("sim", {})
    assert parts(backlight={"sim": True}).backlight is not None
    assert parts(backlight={"kind": "sim"}).backlight is not None
    kiwi = parts(backlight={"kiwispec": "http://127.0.0.1:1"}).backlight
    assert kiwi is not None
    with pytest.raises(ValueError, match="url"):
        parts(backlight={"kind": "kiwispec"})
