"""Registries and plug-ins (robofast.plugins, docs/plugins.md)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from robofast import plugins
from robofast.instruments import INSTRUMENT_TYPES
from robofast.kinds import OBSERVATION_KINDS, ObservationKind
from robofast.plan import PLAN_TYPES, Observation, StaticPlan, plan_from_config
from robofast.plugins import Registry, load_config_plugins, load_plugins
from robofast.weather_sources import SOURCE_TYPES

PLUGIN = """
from robofast.kinds import OBSERVATION_KINDS, ObservationKind
from robofast.plan import PLAN_TYPES, StaticPlan
from robofast.weather_sources import SOURCE_TYPES, HttpSource, Reading

CALLS = []


class StationSource(HttpSource):
    def parse(self, text, now):
        pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line)
        return Reading(self.name, now, {k.strip(): float(v) for k, v in pairs})


class Twice(ObservationKind):
    name = "{prefix}.twice"
    light = True

    async def run(self, r):
        await r.expose_frames()
        await r.expose_frames()


def register():
    CALLS.append(1)
    SOURCE_TYPES.register("{prefix}.station", lambda cfg, site, **kw: StationSource(cfg, **kw))
    PLAN_TYPES.register(
        "{prefix}.reversed",
        lambda config, telescopes, **kw: StaticPlan(config["targets"][::-1], telescopes),
    )
    OBSERVATION_KINDS.register(Twice.name, Twice())
"""


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """A plug-in module (``plug_<n>``) on the path; its entries are removed after."""
    name = f"plug_{tmp_path.name.replace('-', '_')}"
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(PLUGIN.replace("{prefix}", name)))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    SOURCE_TYPES.unregister(f"{name}.station")
    PLAN_TYPES.unregister(f"{name}.reversed")
    OBSERVATION_KINDS.unregister(f"{name}.twice")
    plugins._loaded.discard(name)
    sys.modules.pop(name, None)


def test_a_name_is_registered_once():
    reg: Registry[int] = Registry("thing")
    reg.register("a", 1)
    with pytest.raises(ValueError, match="thing 'a' is registered twice: by builtins and by"):
        reg.register("a", 2)
    assert reg["a"] == 1 and list(reg) == ["a"]
    with pytest.raises(ValueError, match=r"x\.type: thing 'b' is not one of \['a'\]"):
        reg.lookup("b", "x.type")
    with pytest.raises(ValueError, match="registered twice"):
        PLAN_TYPES.register("dispatch", lambda *a, **k: None)


def test_core_registers_its_own_entries_and_the_drivers_theirs():
    assert PLAN_TYPES.names() == ["static", "calibration", "dispatch"]
    assert {"expose", "spectrum", "arc", "lamp_flat"} <= set(OBSERVATION_KINDS.names())
    assert {"imager", "camera_spectrograph", "kiwispec"} <= set(INSTRUMENT_TYPES)
    # KiwiSpec is the devices package's built-in plug-in, not core's.
    assert INSTRUMENT_TYPES.origin("kiwispec") == "robofast.devices"
    assert INSTRUMENT_TYPES.origin("imager") == "robofast.instruments"


def test_a_config_lists_a_plugin_and_its_entries_work(plugin):
    with pytest.raises(ValueError, match=f"plan type '{plugin}.reversed' is not one of"):
        plan_from_config({"type": f"{plugin}.reversed", "targets": []}, ["t1"])
    targets = [{"name": "a"}, {"name": "b"}]
    plan = plan_from_config(
        {"type": f"{plugin}.reversed", "plugins": [plugin], "targets": targets}, ["t1"]
    )
    assert [o.name for o in plan.queues["t1"]] == ["b", "a"]
    assert sys.modules[plugin].CALLS == [1]
    assert load_config_plugins({"plugins": [plugin]}) == []  # once per process
    assert sys.modules[plugin].CALLS == [1]
    assert f"{plugin}.station" in SOURCE_TYPES
    obs = Observation("x", kind=f"{plugin}.twice")
    with pytest.raises(ValueError, match=f"a {plugin}.twice needs light"):
        Observation("x", kind=f"{plugin}.twice", light=False)
    assert isinstance(OBSERVATION_KINDS[obs.kind], ObservationKind)
    # Observations of the plug-in's kind go to a plan like any other.
    assert StaticPlan([{"name": "y", "kind": f"{plugin}.twice"}], ["t1"])


def test_bad_plugins_are_refused_with_the_reason(plugin):
    with pytest.raises(ValueError, match="plugins: no_such_module_xyz: No module named"):
        load_plugins(["no_such_module_xyz"])
    with pytest.raises(ValueError, match="a list of module names"):
        load_config_plugins({"plugins": "robofast.sites.minerva"})
    with pytest.raises(ValueError, match="module has no register"):
        load_plugins(["robofast.kinds"])
    plugins._loaded.discard("robofast.kinds")


def test_entry_points_are_run_once(plugin, monkeypatch):
    calls = []

    def register():
        calls.append(1)
        PLAN_TYPES.register("ep.plan", lambda *a, **k: None)

    ep = SimpleNamespace(name="mysite", value="mysite.robofast:register", load=lambda: register)
    monkeypatch.setattr(
        plugins, "entry_points", lambda group: [ep] if group == plugins.ENTRY_POINT_GROUP else []
    )
    monkeypatch.setattr(plugins, "_started", False)
    try:
        assert "ep.plan" in PLAN_TYPES  # the first use loads them
        assert PLAN_TYPES.origin("ep.plan") == "mysite (mysite.robofast:register)"
        load_plugins()
        assert calls == [1]
    finally:
        PLAN_TYPES.unregister("ep.plan")
        plugins._loaded.discard("entry-point:mysite")


def test_the_weather_bridge_loads_the_sites_adapters_from_its_config():
    from robofast.weather_bridge import load_config, parse_config

    raw = load_config(Path(__file__).resolve().parents[2] / "sites/minerva/weather.yaml")
    assert raw["plugins"] == ["robofast.sites.minerva"]
    cfg = parse_config(raw)
    assert "minerva.ridge" in SOURCE_TYPES
    assert SOURCE_TYPES.origin("minerva.ridge") == "robofast.sites.minerva"
    assert {s.type for s, _ in cfg.sources} >= {"redis", "minerva.ridge"}
