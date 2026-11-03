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
  `FindHome` (only with `allow_find_home`; see "Homing"), `Tracking`, `PulseGuide` (as a PWI RA/Dec offset of
  `guide_rate` arcsec/s, RA in coordinate arcsec). No sync, no MoveAxis.
* **Focuser** (absolute, microns): the focuser **on the current M3 port**.
  `Move` refused outside [`focus_min`, `focus_max`] or while the M3 moves.
* **Rotator**: the rotator on the current M3 port, mechanical degrees
  (`Sync`/`Reverse` only change `Position`, in memory). With PWI4 it reads
  only while the M3 is on `rotator_status_port` (below).

Actions on the telescope:

| Action | Parameters | Does |
|---|---|---|
| `SelectPort` | `"1"`, `"2"` or a `ports` name (`"imager"`, `"fau"`) | M3 to that port; waits until PWI reports it (`m3_timeout` in all; then `m3 stop` and an error). A goto PWI ignored (the reported port unchanged after `m3_resend_after` s) is sent again, at most `m3_retries` times. The sequencer's `port_action`. |
| `Offset` | `{"east": E, "north": N}` arcsec on the sky | RA/Dec offset (RA `E / cos Dec`); tracking; at most `max_offset` |
| `MountConnect` | | PWI connects to the mount controller (moves nothing) |
| `MotorsEnable` / `MotorsDisable` | | both axis motors |
| `FindHome` | `"both"`, `"0"` (azimuth) or `"1"` (altitude) | home; only with `allow_find_home`; one axis needs PWI4 4.1.7+ |
| `Stop` | | mount stop; works without `Connected` |
| `Status` | | JSON: PWI's typed status and the server's state |

## Command mapping

| Operation | PWI4 request | PWI2 request | Confirmed? |
|---|---|---|---|
| status | `/status` | `/?cmd=getsystem` | both: legacy |
| mount connect | `/mount/connect` | `device=mount&cmd=connect` | both: legacy |
| enable motors | `/mount/enable?axis=0`, `axis=1` | `device=mount&cmd=enable` | both: legacy (`axis=-1` rejected by PWI4 4.1.4) |
| disable motors | `/mount/disable?axis=0`, `axis=1` | `device=mount&cmd=disable` | both: legacy |
| find home (both axes) | `/mount/find_home` | `device=mount&cmd=findhome` | both: legacy |
| find home (one axis) | `/mount/findhome?axis=0\|1` | (none: PWI2 homes both) | PWI4 changelog, 4.1.7 beta 3 ("Add axis parameter to /mount/findhome"); **path spelling and values unconfirmed** |
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
PWI4 also: `pwi4.version`, and `mount.axisN.is_homed` where present (PWI4
4.1.4 had none, and the changelog documents no homed status key; PWI4
4.1.2+ "remembers if [a CDK700 has] been homed since the last power
cycle", but 4.1.6 reports nothing of it over HTTP: see "The home record").

Quirks handled:

* **PWI2 `mount.moving` is `True` whenever tracking is off** (legacy
  `cdk700.isMoving`), so it means "slewing" only while tracking. For an
  untracked move the server also counts the mount as slewing while its
  position changes faster than `moving_speed` (deg/s) between reads.
* **PWI4 reports only the GUI-selected focuser and rotator** ("Select from
  M3" must be ticked in PWI4; nothing in the API can check it), and
  `rotator.mech_position_degs` is always Rotator index 0's.
* **PWI4 accepts a goto beyond its own axis limits** (HTTP 200, no error),
  decelerates to exactly the limit, and then reports `mount.is_slewing`
  (and `is_tracking`) true forever. The server watches every slew it sent:
  slewing while the mount moves less than `stall_move` deg in `stall_time`
  s is a failed slew. It sends stop, and `Slewing` raises the reason (the
  target is probably beyond PWI's own limit) until the next slew or
  `AbortSlew`. The jog tool stops such a move too.
* **PWI4 can ignore an M3 goto**: `/m3/goto?port=1` sent about 3 s after the
  M3 had arrived at port 2 answered 200, and the M3 never moved (still port 2
  after 120 s); sent again later it took 6 s. `SelectPort` re-sends a goto
  the M3 has not started on (see Actions). `m3.port` reads -1 while the M3
  moves, and may read -1 or stale just after PWI4 starts.
* **PWI4 configuration keys are dotted** (`Axis0.HomePositionDegs`,
  `Axis1.NegativeSoftwareLimitDegs`); an undotted name is silently ignored.

## Homing

`/mount/find_home` under PWI4 4.1.6 homed both CDK700s from every start
position tried (17 of 17 runs on one, 11 of 11 on the other), in 33-58 s:

* The altitude home sensor is a single edge; its state tells the controller
  which way to go.
* Azimuth first drives to the NoWrap sensor (66 deg above home on one
  mount, 93 deg below on the other), then back to home, so expect up to
  about 100 deg of azimuth travel.
* PWI4 aborts an axis after 30 s of its own fine search if the sensor's
  hysteresis is wide (seen once): the axis stops away from home.
* Until the first home after a controller power cycle the encoder positions
  mean nothing (both axes read about 0 deg).

What robofast does:

* **Nothing homes implicitly**: not starting, not connecting, not enabling,
  not before a slew. The server's `FindHome` (Alpaca, and the `FindHome`
  Action) is refused (NotImplemented, `CanFindHome` false) unless the config
  says `"allow_find_home": true`; the MINERVA configs say false.
* The engineering path is `robofast-pwi-jog home --axis 0|1|both`: it asks
  first, prints the position while it runs, and sends stop on Ctrl-C, after
  `home_timeout` s, or (for one axis) if the other axis moves more than
  0.5 deg -- what a PWI4 that ignored the axis parameter would do.
* One axis at a time needs PWI4 4.1.7 or later (by `pwi4.version`; the
  axis parameter arrived in 4.1.7 beta 3, and a 4.1.7 beta 1 or 2 that
  ignored it would be caught by the other-axis watch). On PWI4 4.1.6 home
  both axes together. PWI2 homes both together.
* **Moves are refused while the mount is not homed**: slews, park,
  tracking on, offsets, pulses, and the jog's `altaz`/`radec`. Homed means
  PWI2 `encoders_have_been_set`, PWI4 `axisN.is_homed` where reported, or
  robofast's **home record** (below). The engineering override is
  `"allow_unhomed_moves": true` in the config **and** `--allow-unhomed` on
  the command line (server or jog); every such move is logged as a warning.

### The home record (PWI4)

PWI4 4.1.6's `/status` has no homed flag, so robofast keeps its own
evidence (`robofast_core/devices/pwi_homed.py`): a JSON file per telescope,
`$ROBOFAST_DATA_DIR/pwi-homed/<name>.json` (or `home_record` in the
config), shared by the server and the jog tool.

* **Written** only when a home robofast commanded (`robofast-pwi-jog home`,
  or the server's `FindHome`) ends with the mount still and each homed axis
  within `home_tolerance` (deg) of the config's `home_alt` / `home_az`:
  where PWI4 reports the mount after a good home. A home that ends
  elsewhere (an aborted fine search) is not recorded, and the jog prints
  where it ended. Without `home_alt`/`home_az` nothing is recorded (the
  server counts its own home until it stops, as before).
* **Dropped** (the file removed) as soon as the server or the jog sees PWI
  not connected to the mount (a PWI4 restart or a controller power cycle),
  both axes reading within 1 deg of 0 (the encoders after a power cycle), or
  a PWI version other than the one that homed. Reconnecting does not bring
  it back: home again.
* `robofast-pwi-jog forget-home` drops it by hand. What it cannot see: a
  controller power cycle while nothing of robofast's reads PWI, followed by
  a move away from 0 by another client. After any power cycle, home again
  or run `forget-home`.

So the operator's path after a power cycle is: `connect`, `enable`,
`home --axis both` (watching the webcam), and the record lets the server
and jogs move without any override. To set `home_alt`/`home_az` for a
mount, home it once, check it on the webcam, and copy the position the jog
prints.

The PWI version (PWI4 `pwi4.version`; PWI2 reports none) is logged by the
server at start and on any change, by the jog at its first read, and is in
the `Status` Action's JSON. It is not yet in FITS headers: the header path
reads standard Alpaca properties, and a PWI version needs a new value
source there (a later step).

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
* **A stop is not instant**: from full slew speed (15 deg/s, with
  15 deg/s^2 deceleration configured in PWI4) a CDK700 coasted 6-8 deg after
  `/mount/stop` (measured 5.9 and 7.4 deg in azimuth and altitude on one
  mount, 7.2 and 8.1 on the other, PWI4 4.1.6). Anything that stops the
  mount at a limit must act that far before it. The config's
  `stop_distance` (default 8 deg) at `slew_speed` (default 15 deg/s) is that
  coast; at speed v robofast assumes `stop_distance * (v / slew_speed)^2`
  (constant deceleration) plus one `watch_interval` of travel.
* **Limit watch**: every `watch_interval` s the server sends `stop` if the
  mount is outside the limits by more than `limit_margin` and still moving
  further out (a tracked target setting), or if it is heading out fast
  enough that it would coast past `limit_margin` after a stop (motion
  commanded by something else). A slew the server itself sent to a target
  inside the limits is left to PWI's own deceleration unless the mount
  passes that target by more than `limit_margin`. It never commands motion.
  The jog tool applies the same coast to its own limit check.
* **Stalled slews**: see "Quirks handled" (PWI4 at its own limit).
* **Stop** is always reachable: Alpaca `AbortSlew`, Action `Stop` (no
  connection needed), `robofast-pwi-jog stop`, and PWI's own GUI.
* Every PWI request has a timeout (`pwi.timeout`); nothing is retried,
  except an M3 goto PWI visibly did not start on (bounded, `m3_retries`).

## Config

`sites/minerva/remote/t1_pwi2.json` (and `t2_pwi2`, `t1_pwi4`, `t2_pwi4`):
`name`, `backend`, `latitude`, `longitude`, `elevation`, `min_alt`,
`max_alt`, `park_alt`, `park_az` are required (no defaults, design.md
3.10); with a focuser also `focus_min`, `focus_max`. Optional: `pwi`
(`host`, `port`, `timeout`, `connect_timeout`), `allow_hardware`, `listen`,
`focuser`, `rotator`, `rotator_status_port`, `ports`, `m3_timeout`,
`m3_resend_after`, `m3_retries`, `max_offset`, `guide_rate`,
`max_pulse_ms`, `limit_margin`, `watch_interval`, `stop_distance`,
`slew_speed`, `stall_time`, `stall_move`, `moving_speed`,
`slew_report_min`, `park_tolerance`, `status_max_age`, `request_timeout`,
`aperture_diameter`, `focal_length`, `max_jog_deg` (at most 10),
`jog_timeout`, `home_timeout`, `home_alt`, `home_az`, `home_tolerance`,
`home_record`, `allow_find_home`, `allow_unhomed_moves`,
`discovery_port`.

    robofast-pwi-server --config sites/minerva/remote/t1_pwi4.json --hardware [--allow-unhomed]

## Simulators

    python -m robofast_core.devices.pwi_sim --api pwi4|pwi2 --port 0 [--ready]

One CDK700 model behind either API. Motion needs connect + enable (start
with `--ready` for connected, enabled and homed); slews at `--slew-rate`;
the azimuth never crosses 0/360, so a goto from 359 to 1 deg goes the long
way (as a mount's cable wrap might). Fault modes (`/robofast-sim/mode?mode=`):
`hang`, `slow`, `garble`, `http500`. The PWI4 behaviour seen on hardware is
modelled too: `--pwi-min-alt` (a goto below PWI's own limit stops there and
reports slewing forever), `m3.port` -1 while the M3 moves, dropped M3
gotos (`CDK700Model.m3_drops`), and a controller power cycle
(`CDK700Model.power_cycle`: disconnected, not homed, both axes at 0).
Loopback only, registered with `hwgate` while serving.

## The jog tool

`robofast-pwi-jog --config CONFIG [--hardware] [--yes] COMMAND`

| Command | Does |
|---|---|
| `status [--watch S]` | read only (the default) |
| `stop` | mount stop, at once |
| `connect` | PWI connects to the mount controller |
| `enable` / `disable` | both axis motors (`enable` asks first) |
| `tracking-off` | tracking off |
| `home --axis 0\|1\|both` | find home (asks first; see "Homing"); records a home that ends at `home_alt`/`home_az` |
| `forget-home` | drop robofast's home record |
| `altaz --dalt D --daz D` | relative alt/az goto, each at most `max_jog_deg`; target inside the limits; tracking off; no azimuth move across 0/360; homed (or `--allow-unhomed`) |
| `radec --dra A --ddec A` | relative RA/Dec offset in arcsec while tracking, each at most `max_jog_deg` x 3600 |

A move prints its plan and waits for `yes` (or `--yes`), then prints the
position every `--poll` s. It sends **stop** on Ctrl-C, after
`jog_timeout`, if the mount moves more than planned + `--slack` deg from
where it started, if the altitude leaves the limits or heads out of them
fast enough to coast past them, or if PWI reports slewing while the mount
does not move (`stall_time`). Everything is
logged to `$ROBOFAST_DATA_DIR/pwi-jog/<name>.log` (or `--log`).

## PWI4 on the network

PWI4's HTTP API (port 8220) listens on the telescope PC's own 127.0.0.1
only (as on T1, PWI4 4.1.6). robofast reaches it without opening it to the
LAN: a second SSH hop through the telescope PC's own sshd (gateway, then
the telescope PC on port 22, forwarding to its 127.0.0.1:8220), with
`ProxyJump`. The `ssh` lines are in
[docs/remote.md](remote.md#pwi4-through-the-telescope-pcs-sshd).

The alternative: in PWI4's HTTP server settings allow connections from
other machines, and add a Windows Defender Firewall inbound rule on the
telescope PC for TCP 8220 allowing **only** the gateway's address (Scope:
remote IP address).

Either way, check through the tunnel from the control machine with
`robofast-pwi-jog --config ... --hardware status` (it prints PWI4's
version).

## First hardware session

Checklist (before any command that moves):

1. **Enclosure closed** (the mount moves; no sky needed). Nobody in it.
2. **Legacy users stopped**: the legacy control code (`mainNew` and
   anything else that talks to PWI on that telescope) is not running, so
   robofast is PWI's only client. Only one of PWI2 and PWI4 is running.
3. **Webcam on the telescope**, watched by someone who can see the mount.
4. **The PDU outlet** for the mount (and how to switch it off) is known.
5. **Abort at hand**: a second terminal with
   `robofast-pwi-jog --config ... --hardware stop` typed and ready; PWI's
   own Stop button as a second path.
6. The tunnel is up (`docs/remote.md`); PWI4's API reachable ("PWI4 on the
   network").
7. **Before any home**: compare PWI2's two mount settings files on the
   telescope PC (`settingsMount.xml` vs
   `settingsMount.20161219.badhome.xml`) and set PWI4's mount homing
   direction and limits to match the live PWI2 file. For T1 the live
   `settingsMount.xml` (read 2026-11-02) says:

   | PWI2 setting | Live value | 2016 "badhome" file |
   |---|---|---|
   | `homingMoveAzmCwWhenTrue` | True (azimuth homes clockwise) | False |
   | `homingMoveAltUpWhenTrue` | False (altitude homes down) | True |
   | azimuth home (encoder) | 4.90527 rad (281.05 deg) | |
   | altitude home (encoder) | 0.81423 rad (46.65 deg) | |
   | altitude limits: hard / goto / track | 20-90 / 20-86 / 20-87 deg | |
   | azimuth CW limit: hard / goto / track | 510 / 450 / 480 deg | |
   | azimuth CCW limit: hard / goto / track | -150 / -90 / -130 deg | |

   PWI4 was set to match (its keys are dotted: `Axis0.HomePositionDegs`,
   `Axis1.NegativeSoftwareLimitDegs`; an undotted key is silently
   ignored), and homing then worked from every start position ("Homing").
   With PWI4's home positions equal to PWI2's (the same encoder frame) PWI4
   loads PWI2's `.PXP` pointing-model files directly
   (`/mount/model/load?filename=`, from PWI4's own GUI or by hand: robofast
   does not send it). robofast's own limits stay tighter: `min_alt` 21, `max_alt` 83 (legacy
   scheduler); the jog refuses any azimuth move across 0/360 rather than
   modelling the -90..450 deg wrap.

Sequence (T1 on PWI4, through the 7141 tunnel):

    C=sites/minerva/remote/t1_pwi4.json
    J="robofast-pwi-jog --config $C --hardware"
    # 1. Read only.
    $J status                          # version, alt/az, conn, motors, homed
    $J status --watch 2                # leave running in a window
    # 2. Connect and enable (no motion).
    $J connect                         # only if conn 0
    $J enable                          # only if motors 00; asks yes
    $J tracking-off                    # only if track 1
    # 3. Tiny jogs BEFORE homing, to see which way each axis goes.
    $J --allow-unhomed altaz --dalt 0.2     # up 0.2 deg; asks yes; watch the webcam
    $J --allow-unhomed altaz --dalt -0.2    # back
    $J --allow-unhomed altaz --daz 0.2      # azimuth (away from 0/360)
    $J --allow-unhomed altaz --daz -0.2
    # 4. Settings checked (checklist 7)? Home (33-58 s; azimuth may first
    #    travel ~100 deg to its NoWrap sensor). Both axes on PWI4 4.1.6;
    #    one at a time (--axis 1, then --axis 0) needs PWI4 4.1.7+.
    $J home --axis both                # asks yes; stop at hand
    $J status                          # "home record: axes [0, 1]"
    # 5. Homed (recorded): jogs without the override.
    $J altaz --dalt 0.5
    $J stop                            # any time

Watch on the webcam that each move goes the right way and by about the
right amount, and that each home goes toward its sensor. Only then the
server (`robofast-pwi-server --config $C --hardware`) and Alpaca clients.
On PWI2 (T1 config `t1_pwi2.json`, tunnel 7101) the steps are the same
except that `home --axis both` is the only home.

## Measured on hardware

Two CDK700s under PWI4 4.1.6 (2026-11):

| What | Measured |
|---|---|
| Slew of 10-40 deg | 6-10 s; final error at most 2 arcsec |
| Repeatability (returning to a position) | 0.1-0.3 arcsec |
| Tracking servo error | 0.06-0.12 arcsec rms |
| Stop from full slew speed (15 deg/s, 15 deg/s^2) | coasts 6-8 deg ("Safety") |
| Home (`/mount/find_home`) | 33-58 s, from any start ("Homing") |
| M3 port 1 to 2 / 2 to 1 | 5.7-10.7 s / 6-41 s |
| Rotator (`/rotator/goto_mech?degs=`; the parameter is `degs`) | about 2.3 deg/s; ends 2-6 arcsec short of the target |
| `/status` latency | about 2 ms on the telescope PC (an SSH tunnel adds its round trip) |

## To check on hardware

* PWI2 accepts decimal `ra2000`/`dec2000`/`alt`/`azm` (legacy docstrings
  say so; legacy acquisitions sent decimals).
* What PWI2 does with an alt/az goto while tracking (legacy turned tracking
  on before an alt/az park, then off): robofast turns tracking off first.
* PWI2 `moving` after an untracked alt/az goto (we rely on position change).
* Offset sign conventions (`incrementra` / `ra_add_arcsec` positive = RA
  increases) and the PulseGuide directions, with a star on a camera.
* Whether PWI4's `/mount/find_home` reports `is_slewing` while homing.
* The per-axis home request: `/mount/findhome?axis=0|1` (changelog
  spelling; the older both-axes command is `/mount/find_home`), axis 0 =
  azimuth, 1 = altitude (PWI4 4.1.7+). PWI4 4.1.6 reports no homed state
  over HTTP (hence the home record); whether a later PWI4 does.
* The azimuth cable wrap: which way PWI takes for a goto near the wrap
  limits (the jog refuses azimuth moves across 0/360 meanwhile).
* Focuser positions (microns) and rotator mechanical angles match legacy.
