# MEarth South (CTIO)

Eight telescopes (`tel11`-`tel18`) under one roll-off roof on Cerro Tololo.
Values are adapted from the MEarth observing system (J. Irwin, used with
permission). **Nothing here is ready for hardware**: every address is a
`127.0.0.1` placeholder, the roof server refuses its real port, and the
weather bridge has no real station source yet.

| File | What | Run |
|---|---|---|
| `roof.json` | the roof's Alpaca Dome server (`docs/mearth_roof.md`); `allow_hardware: false` | `python -m robofast.devices.mearth_roof_server --config sites/mearth_south/roof.json --simulate` |
| `mearth_south.json` | the enclosure: supervisor and sequencer | `python -m robofast.supervisor_service --config sites/mearth_south/mearth_south.json` |
| `weather.yaml` | the weather bridge, legacy South limits; a placeholder station source (unsafe until replaced) | `python -m robofast.weather_bridge --config sites/mearth_south/weather.yaml --shadow` |

## Policy

- **Attended by default; robotic by the site's choice.** The roof has no
  hardware watchdog, so nothing closes it if the supervisor dies. The site
  opts in to running unattended anyway
  (`safety.allow_unattended_without_watchdog`; JDE 2026-11-02: MEarth ran
  robotically for years with this roof): it starts in `attended` (no lease,
  so no opening, unless an operator is on duty; `docs/console.md`) and may
  be switched to `robotic`, with a loud warning everywhere it shows
  (`docs/modes.md`, "Unattended without a watchdog").
- **Park before close.** The roof rolls over the telescopes.
- **Faults block opening.** The roof server's fault channel (`Faults`)
  reports a move that failed, an unknown position or both limits made;
  only an operator clears it (no automatic clearing, as legacy).
- **Reopen hold 20 minutes**, as legacy latched its weather alerts.

## Unconfirmed

Everything under "Confirm on site" in `docs/mearth_roof.md`, and:

- the telescopes' Alpaca endpoints (legacy drove the mounts and cameras
  through Windows software on one PC per telescope);
- a reader for the site's weather station and cloud sensor (legacy read
  both over serial ports), to replace the placeholder `station` source;
- the weather limits that the bridge cannot express yet (dew margin, the
  humidity correction of the cloud limit, separate east and west Sun
  limits, the cloud sensor's rain flag).
