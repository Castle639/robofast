# Plug-ins: site and instrument code without editing core

A site that needs new code -- a weather page in its own format, a plan
type, an observation kind, an instrument -- adds it as a **plug-in**, not as
a patch to `robofast_core` (docs/design.md 3.10). A plug-in is a Python
module with a `register()` function that adds entries to core's
**registries**; a config then names those entries by their keys, exactly as
it names core's own.

## The registries

| Registry | Key in a config | Core's entries |
|---|---|---|
| `robofast_core.plan.PLAN_TYPES` | a plan's `type` | `static`, `calibration`, `dispatch` |
| `robofast_core.kinds.OBSERVATION_KINDS` | an observation's `kind`; each kind's `request_types` are OCS configuration `type`s | `expose` (EXPOSE, DARK, BIAS), `autofocus` (AUTO_FOCUS), `spectrum` (SPECTRUM), `spectrograph_calibration`, `manual`, `pointing_model` (POINTING_MODEL), `rotator_calibration` (ROTATOR_CALIBRATION), `sky_flat` (SKY_FLAT), `arc` (ARC), `lamp_flat` (LAMP_FLAT) |
| `robofast_core.instruments.INSTRUMENT_TYPES` | a dispatch config's `instruments.<name>.type`; an enclosure config's `spectrograph.type` | `imager`, `camera_spectrograph`, `kiwispec` |
| `robofast_core.guider.hardware.CAMERAS`, `TIPTILTS`, `MOUNTS`, `BACKLIGHTS` | a guider config's `camera`, `tiptilt`, `mount`, `backlight` `kind` | cameras `sim`, `alpaca`, `zwo`, `framegrabber`; tip/tilts `sim`, `sxao`, `sxao-sim`, `pi`, `pi-sim`; mounts `sim`, `alpaca`; backlights `sim`, `kiwispec` (by `robofast_core.devices`) |
| `robofast_core.notifier.sinks.SINK_TYPES` | a notifier sink's `type` | `email`, `webhook`, `command` |
| `robofast_core.weather_sources.SOURCE_TYPES` | a weather source's `type` | `sun`, `redis`; MINERVA's `minerva.ridge`, `minerva.sky_temps`, `minerva.mearth` (a plug-in) |
| `robofast_core.request_model.REQUEST_IMPORTERS` | a dispatch plan's `imports`: `[{"type", "path", ...options}]` | none; MINERVA's `minerva.legacy_csv` (a plug-in) |

Every registry is a `robofast_core.plugins.Registry`: `register(name,
entry)` adds an entry, and a name that is already taken is refused (the
error names both origins), so a plug-in can never silently replace core's
entry or another plug-in's. A config that names something nobody
registered is an error listing what is registered.

Core registers its own entries with the same call, so the table above is
just what is there before any plug-in runs. The hardware drivers'
handlers (KiwiSpec) are a built-in plug-in, `robofast_core.devices`.

## Loading a plug-in

Either way, each plug-in's `register()` runs once per process.

**An installed package** declares an entry point in the group
`robofast.plugins` in its own `pyproject.toml`; robofast runs every such
entry the first time any registry is used:

```toml
[project.entry-points."robofast.plugins"]
mysite = "mysite.robofast:register"
```

**Code that is not installed** (a module on the Python path, or a site
adapter shipped under `robofast_core/sites/`) is listed in the config that
needs it, under `plugins` (module names, or `module:function`):

```yaml
# sites/minerva/weather.yaml
plugins:
  - robofast_core.sites.minerva
```

The services load the `plugins` of the configs they read: the weather
bridge's config, an enclosure config (sequencer, request API), a plan entry
(`plan_from_config`, the dispatch plan), the request API's config and the
notifier's config.

## A minimal example

A site whose weather station serves `temperature=12.3` lines, as a new
weather source type `mysite.station`:

```python
# mysite/robofast.py
from datetime import datetime

from robofast_core.weather_sources import SOURCE_TYPES, HttpSource, Reading


class StationSource(HttpSource):
    """GETs the configured url; parse() turns the page into a Reading."""

    def parse(self, text: str, now: datetime) -> Reading:
        pairs = (line.split("=", 1) for line in text.splitlines() if "=" in line)
        return Reading(self.name, now, {k.strip(): float(v) for k, v in pairs})


def register() -> None:
    SOURCE_TYPES.register("mysite.station", lambda cfg, site, **kw: StationSource(cfg, **kw))
```

and in the site's weather config:

```yaml
plugins: [mysite.robofast]
sources:
  station: {type: mysite.station, url: "http://station.example/now", ...}
```

The other registries work the same way:

* **A plan type**: `PLAN_TYPES.register("lookahead", factory)` with
  `factory(config, telescopes, *, base, clock) -> PlanProvider`
  (`robofast_core/plan.py`).
* **An instrument type**: `INSTRUMENT_TYPES.register(name,
  InstrumentType(...))` (`robofast_core/instruments.py`): the OCS
  configuration types it takes and advertises, its own `extra_params`
  (`Param`: how each is parsed, for which types, and how it goes to the
  instrument with each frame), and, for a spectrograph service the
  sequencer drives, its calibration frame kinds and a `client_factory`
  building a `SpectrographHook` from the enclosure config's `spectrograph`
  entry. A dispatch config then declares which of its instrument names is
  of that type: `"instruments": {"echelle": {"type": "mysite.echelle"}}`
  (a name it does not declare is its own type, so `imager` needs none).
* **A guider backend**: `CAMERAS.register("mysite.cam", factory)` (or
  `TIPTILTS`, `MOUNTS`, `BACKLIGHTS`) with `factory(spec, ctx)`: `spec` the
  part's config less its `kind`, `ctx` a `BuildContext` (`allowed`: the
  hardware gate, which a real device's driver must honor; `clock`; `world`,
  the simulated sky) (`robofast_core/guider/hardware.py`).
* **A request importer**: `REQUEST_IMPORTERS.register("mysite.list",
  fn)` with `fn(path, **options) -> list[RequestGroup]`; a dispatch plan
  then has `"imports": [{"type": "mysite.list", "path": "targets.txt"}]`.
* **An observation kind**: subclass `robofast_core.kinds.ObservationKind`
  and register an instance under its `name`. The kind owns its validation
  (`validate`, `fields`), its request mapping (`request_types`,
  `check_instrument_config`, `check_configuration`, `observation`) and its
  execution (`run`, called at its `stage` of the sequencer's set-up). Core's
  kinds in `robofast_core/kinds.py` are the worked examples.

A plug-in that fails to import, or registers a name that is taken, stops
the service at start-up with the reason: a config that names it cannot be
served without it.

## Site code shipped with robofast

`robofast_core/sites/<site>/` holds each site's code: its plug-in (a
`register()` in the package, listed in that site's configs) and its legacy
importers with their own command lines:

| Module | What |
|---|---|
| `robofast_core.sites.minerva` | the plug-in: weather pages (`minerva.*` sources), the legacy target CSV (`minerva.legacy_csv`) |
| `robofast_core.sites.minerva.target_sheet` | the target lists from their Google Sheet (`python -m`; docs/minerva_sheet.md) |
| `robofast_core.sites.minerva.focus_model` | the legacy `guessFocus` model as a `FocusModel` (docs/autofocus.md) |
| `robofast_core.sites.minerva.guider_calibration` | the legacy SX AO calibration as a matrix (docs/guider.md) |
| `robofast_core.sites.flwo60.catalogs` | the telshell catalogs, `.mct`/`.off`/`.sec` (`python -m`; docs/catalogs.md) |
| `robofast_core.sites.flwo60.guider_calibration` | tres-guider's tip/tilt calibration as a matrix |

Core never imports them; `tests/core/test_no_site_values.py` refuses a
site's name, number or site-named module anywhere else in core but the
hardware drivers (`robofast_core/devices/`).

## Not code: device action names

Where core sends an Alpaca `Action` to a device, the action's name is
configuration, its default the name robofast's own server uses (that is
robofast's protocol): the safety config's `fault_action`,
`clear_faults_action` (`ClearFaults`) and `heartbeat_action`
(`Heartbeat`); a recovery policy's `powercycle.action` (`PowerCycle`); a
telescope's `lamps.status_action` (`Status`) and `lamps.off_action`
(`LampsOff`); `port_action`. A device server of another make needs a
config line, not a plug-in. Likewise the ADC's switches are found by name
(`adc.switches`, docs/adc.md).
