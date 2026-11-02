# PlaneWave PWI server (CDK700 mounts)

robofast's Alpaca server for a PlaneWave CDK700 driven by PlaneWave's PWI:
`robofast_core/devices/pwi_server.py` (Telescope + Focuser + Rotator),
over `pwi_client.py` (two backends), with simulators of both PWI HTTP APIs
(`pwi_sim.py`) and a small-move tool for the first hardware session
(`pwi_jog.py`, `robofast-pwi-jog`). ConformU runs against the server on both
simulators in CI (`scripts/conformu_pwi.py`).

| Backend | Program | HTTP | Reply |
|---|---|---|---|
| `pwi4` | PWI4 (`PWI4.exe`) | `GET /<device>/<command>?k=v`, port 8220 | `key=value` lines |
| `pwi2` | PWI2 (`PWI.exe`, MINERVA today) | `GET /?device=<d>&cmd=<c>&k=v`, port 44444 | `<system>` XML |

Sources: the legacy MINERVA driver, `MinervaCollaboration/minerva-control`
`minerva_library/cdk700.py` (PWI2; what MINERVA has used for years), and
its PWI4 port, `minerva_library/pwi4.py` and `docs/pwi4.md` (whose endpoint
list was checked against PlaneWave's reference client and the PWI4 4.1.4
binary). No PlaneWave code is copied here. Both APIs answer every command
with the whole status, so every command is also a status read.

## Devices

All three are device number 0 on one server (`/api/v1/telescope/0`,
`/api/v1/focuser/0`, `/api/v1/rotator/0`).

* **Telescope** (ITelescopeV4, alt-az, J2000): `SlewToCoordinatesAsync`
  (tracking on), `SlewToAltAzAsync` (tracking off), `AbortSlew`, `Park`
  (alt/az slew to `park_alt`/`park_az`, then tracking off), `Unpark`,
  `FindHome`, `Tracking`, `PulseGuide` (as a PWI RA/Dec offset of
  `guide_rate` arcsec/s, RA in coordinate arcsec). No sync, no MoveAxis.
* **Focuser** (absolute, microns): the focuser **on the current M3 port**.
  `Move` refused outside [`focus_min`, `focus_max`] or while the M3 moves.
* **Rotator**: the rotator on the current M3 port, mechanical degrees
  (`Sync`/`Reverse` only change `Position`, in memory). With PWI4 it reads
  only while the M3 is on `rotator_status_port` (below).

Actions on the telescope:

| Action | Parameters | Does |
|---|---|---|
| `SelectPort` | `"1"`, `"2"` or a `ports` name (`"imager"`, `"fau"`) | M3 to that port; waits until PWI reports it (`m3_timeout`; then `m3 stop` and an error). The sequencer's `port_action`. |
| `Offset` | `{"east": E, "north": N}` arcsec on the sky | RA/Dec offset (RA `E / cos Dec`); tracking; at most `max_offset` |
| `MountConnect` | | PWI connects to the mount controller (moves nothing) |
| `MotorsEnable` / `MotorsDisable` | | both axis motors |
| `Stop` | | mount stop; works without `Connected` |
| `Status` | | JSON: PWI's typed status and the server's state |

## Command mapping

| Operation | PWI4 request | PWI2 request | Confirmed? |
|---|---|---|---|
| status | `/status` | `/?cmd=getsystem` | both: legacy |
| mount connect | `/mount/connect` | `device=mount&cmd=connect` | both: legacy |
| enable motors | `/mount/enable?axis=0`, `axis=1` | `device=mount&cmd=enable` | both: legacy (`axis=-1` rejected by PWI4 4.1.4) |
| disable motors | `/mount/disable?axis=0`, `axis=1` | `device=mount&cmd=disable` | both: legacy |
| find home | `/mount/find_home` | `device=mount&cmd=findhome` | both: legacy |
| goto J2000 | `/mount/goto_ra_dec_j2000?ra_hours=&dec_degs=` (decimal) | `device=mount&cmd=move&ra2000=&dec2000=` (decimal hours/deg) | PWI4: legacy; PWI2: legacy (docstring says decimal or sexagesimal) |
| goto alt/az | `/mount/goto_alt_az?alt_degs=&az_degs=` | `device=mount&cmd=move&alt=&azm=` | both: legacy (park) |
| RA/Dec offset | `/mount/offset?ra_add_arcsec=&dec_add_arcsec=` | `device=mount&cmd=move&incrementra=&incrementdec=` | legacy; **sign conventions not confirmed on the sky** |
| stop | `/mount/stop` | `device=mount&cmd=stop` | both: legacy |
| tracking on/off | `/mount/tracking_on`, `/tracking_off` | `cmd=trackingon` / `trackingoff` | both: legacy |
| M3 port | `/m3/goto?port=1\|2`, `/m3/stop` | `device=m3&cmd=select&port=`, `cmd=stop` | both: legacy |
| focuser goto/stop | `/focuser/goto?target=`, `/focuser/stop` (GUI-selected focuser) | `device=focuserN&cmd=move&position=`, `cmd=stop` | legacy |
| rotator goto/stop | `/rotator/goto_mech?degs=`, `/rotator/stop` (GUI-selected rotator) | `device=rotatorN&cmd=move&position=`, `cmd=stop` | legacy |

Nothing else can be sent (`PWI4_PATHS`, `PWI2_COMMANDS` in
`pwi_client.py`): no sync, no pointing-model load/save/add, no jog, no
disconnect, no PWI process control, no focuser/rotator homing, no
derotation (`goto_field` / `derotatestart`; a later step).

Status fields read (`PWIStatus`): PWI4 `mount.is_connected`,
`mount.axis0/1.is_enabled` (az/alt), `mount.is_slewing`, `mount.is_tracking`,
`mount.altitude_degs`, `mount.azimuth_degs`, `mount.ra/dec_apparent_*`,
`mount.ra/dec_j2000_*`, `site.lmst_hours`, `m3.port` (0 = moving),
`focuser.*`, `rotator.mech_position_degs`; PWI2 `mount.connected`,
`azm_enabled`, `alt_enabled`, `moving`, `tracking`, `alt_radian`,
`azm_radian`, `ra_radian`, `dec_radian`, `ra_2000`, `dec_2000`,
`is_finding_home`, `encoders_have_been_set`, `*_motor_error_code`,
`status.lst`, `m3.port`, `m3.moving_rotate`, `focuserN.*`, `rotatorN.*`.

Quirks handled:

* **PWI2 `mount.moving` is `True` whenever tracking is off** (legacy
  `cdk700.isMoving`), so it means "slewing" only while tracking. For an
  untracked move the server also counts the mount as slewing while its
  position changes faster than `moving_speed` (deg/s) between reads.
* **PWI4 reports only the GUI-selected focuser and rotator** ("Select from
  M3" must be ticked in PWI4; nothing in the API can check it), and
  `rotator.mech_position_degs` is always Rotator index 0's.

## Safety

* **Hardware gate** (`robofast_core/hwgate.py`, design.md 3.1): without
  `"allow_hardware": true` in the config **and** `--hardware`, the server
  and the jog tool talk only to a verified simulator on loopback. A tunnel
  (`ssh -L 7101:...`) on 127.0.0.1 is refused before a byte is sent.
* **Nothing implicit**: starting, Alpaca `Connected` and every read only
  read PWI. Connecting PWI to the mount, enabling the motors and homing
  are explicit (`MountConnect`, `MotorsEnable`, `FindHome`).
* **Altitude limits** (`min_alt`, `max_alt`) are checked here before any
  goto (RA/Dec targets via PWI's sidereal time and the configured
  latitude, J2000 precessed to date). Motion also needs the mount connected,
  both motors enabled and no PWI2 drive error.
* **Limit watch**: every `watch_interval` s, if the mount is outside the
  limits by more than `limit_margin` and still moving further out (a
  tracked target setting, motion commanded by something else), the server
  sends `stop`. It never commands motion.
* **Stop** is always reachable: Alpaca `AbortSlew`, Action `Stop` (no
  connection needed), `robofast-pwi-jog stop`, and PWI's own GUI.
* Every PWI request has a timeout (`pwi.timeout`); nothing is retried.

## Config

`sites/minerva/remote/t1_pwi2.json` (and `t2_pwi2`, `t1_pwi4`, `t2_pwi4`):
`name`, `backend`, `latitude`, `longitude`, `elevation`, `min_alt`,
`max_alt`, `park_alt`, `park_az` are required (no defaults, design.md
3.10); with a focuser also `focus_min`, `focus_max`. Optional: `pwi`
(`host`, `port`, `timeout`, `connect_timeout`), `allow_hardware`, `listen`,
`focuser`, `rotator`, `rotator_status_port`, `ports`, `m3_timeout`,
`max_offset`, `guide_rate`, `max_pulse_ms`, `limit_margin`,
`watch_interval`, `moving_speed`, `slew_report_min`, `park_tolerance`,
`status_max_age`, `request_timeout`, `aperture_diameter`, `focal_length`,
`max_jog_deg`, `jog_timeout`, `discovery_port`.

    robofast-pwi-server --config sites/minerva/remote/t1_pwi2.json --hardware

## Simulators

    python -m robofast_core.devices.pwi_sim --api pwi4|pwi2 --port 0 [--ready]

One CDK700 model behind either API. Motion needs connect + enable (start
with `--ready` for connected, enabled and homed); slews at `--slew-rate`;
the azimuth never crosses 0/360, so a goto from 359 to 1 deg goes the long
way (as a mount's cable wrap might). Fault modes (`/robofast-sim/mode?mode=`):
`hang`, `slow`, `garble`, `http500`. Loopback only, registered with
`hwgate` while serving.

## The jog tool

`robofast-pwi-jog --config CONFIG [--hardware] [--yes] COMMAND`

| Command | Does |
|---|---|
| `status [--watch S]` | read only (the default) |
| `stop` | mount stop, at once |
| `connect` | PWI connects to the mount controller |
| `enable` / `disable` | both axis motors (`enable` asks first) |
| `tracking-off` | tracking off |
| `altaz --dalt D --daz D` | relative alt/az goto, each at most `max_jog_deg`; target inside the limits; tracking off; no azimuth move across 0/360 |
| `radec --dra A --ddec A` | relative RA/Dec offset in arcsec while tracking, each at most `max_jog_deg` x 3600 |

A move prints its plan and waits for `yes` (or `--yes`), then prints the
position every `--poll` s. It sends **stop** on Ctrl-C, after
`jog_timeout`, if the mount moves more than planned + `--slack` deg from
where it started, or if the altitude leaves the limits. Everything is
logged to `$ROBOFAST_DATA_DIR/pwi-jog/<name>.log` (or `--log`).

## First hardware session

Checklist (before any command that moves):

1. **Enclosure closed** (the mount moves; no sky needed). Nobody in it.
2. **Legacy users stopped**: the legacy control code (`mainNew` and
   anything else that talks to PWI2 on that telescope) is not running, so
   robofast is PWI's only client. Only one of PWI2 and PWI4 is running.
3. **Webcam on the telescope**, watched by someone who can see the mount.
4. **The PDU outlet** for the mount (and how to switch it off) is known.
5. **Abort at hand**: a second terminal with
   `robofast-pwi-jog --config ... --hardware stop` typed and ready; PWI's
   own Stop button as a second path.
6. The tunnel is up (`docs/remote.md`); `status` reads sane values
   (altitude, azimuth, motors as expected).

Sequence (T1, PWI2, through the 7101 tunnel):

    C=sites/minerva/remote/t1_pwi2.json
    robofast-pwi-jog --config $C --hardware status                # read only
    robofast-pwi-jog --config $C --hardware status --watch 2      # leave running in a window
    robofast-pwi-jog --config $C --hardware connect               # only if status says conn 0
    robofast-pwi-jog --config $C --hardware enable                # only if motors 00; asks yes
    robofast-pwi-jog --config $C --hardware tracking-off          # only if track 1
    robofast-pwi-jog --config $C --hardware altaz --dalt 0.2      # first move: 0.2 deg up; asks yes
    robofast-pwi-jog --config $C --hardware altaz --dalt -0.2     # back
    robofast-pwi-jog --config $C --hardware altaz --daz 0.2       # azimuth, away from 0/360
    robofast-pwi-jog --config $C --hardware altaz --daz -0.2
    robofast-pwi-jog --config $C --hardware stop                  # any time

Watch on the webcam that each move goes the right way and by about the
right amount. Only then the server (`robofast-pwi-server --config $C
--hardware`) and Alpaca clients.

## To check on hardware

* PWI2 accepts decimal `ra2000`/`dec2000`/`alt`/`azm` (legacy docstrings
  say so; legacy acquisitions sent decimals).
* What PWI2 does with an alt/az goto while tracking (legacy turned tracking
  on before an alt/az park, then off): robofast turns tracking off first.
* PWI2 `moving` after an untracked alt/az goto (we rely on position change).
* Offset sign conventions (`incrementra` / `ra_add_arcsec` positive = RA
  increases) and the PulseGuide directions, with a star on a camera.
* Whether PWI4's `/mount/find_home` reports `is_slewing` while homing.
* The azimuth cable wrap: which way PWI takes for a goto near the wrap
  limits (the jog refuses azimuth moves across 0/360 meanwhile).
* Focuser positions (microns) and rotator mechanical angles match legacy.
