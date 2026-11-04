# KiwiSpec's auxiliary devices, driven directly

KiwiSpec's iodine cell heater, iodine stage, fiber backlight, vacuum gauges
and chiller are serial devices on the spectrograph PC (Windows 7, Python
2.7). Legacy MINERVA reached them only through `spectrograph_server.py` on
that PC (docs/kiwispec.md, section 1.1): single-threaded, no exception
handling around its dispatch, a 10 s client timeout for a stage that takes
longer than that to home. robofast needs Python 3.12 and cannot run there.
So robofast drives the devices itself, from Linux, through their serial
ports **by URL**, and serves them as standard ASCOM Alpaca devices:

    robofast (Linux)                                      spectrograph PC
    ---------------------------------------------------   ------------------------------
    KiwiSpec service --Alpaca--> kiwispec_aux_server --rfc2217://pc:700N--> bridge --> COMn --> device
                                  (drivers, simulators)

| Module | What it is |
|---|---|
| `robofast_core/devices/serial_url.py` | serial ports by URL (pyserial `serial_for_url`: `rfc2217://`, `socket://`, a local device, `loop://`) or `sim`; the hardware gate; bounded async reads |
| `robofast_core/devices/tc200.py` | Thorlabs TC200 (the iodine cell heater) |
| `robofast_core/devices/thorlabs_apt.py` | the Thorlabs APT binary protocol (the iodine stage), with the motion supervised |
| `robofast_core/devices/dynamixel2.py` | Robotis Dynamixel protocol 2.0 (the backlight arm's XM servo) |
| `robofast_core/devices/kiwispec_serial.py` | the backlight LED controller, the vacuum gauge controller (read only), the Oasis chiller (read only) |
| `robofast_core/devices/kiwispec_aux_sim.py` | a byte-level simulator of each device, in process or on a registered loopback TCP port |
| `robofast_core/devices/kiwispec_aux_server.py` | the Alpaca server (`robofast-kiwispec-aux-server`) |
| `robofast_core/devices/kiwispec_aux.py` | `AuxRouted`: the KiwiSpec service's calls for these devices, sent to the Alpaca server |
| `tests/core/test_kiwispec_aux.py` | fast-tier tests, all against the simulators |
| `scripts/conformu_kiwispec_aux.py` | ConformU against all four Alpaca devices (CI job `conformu`) |
| `sites/minerva/kiwispec_aux.example.json` | MINERVA's config (bridge host and ports are placeholders) |

Everything about the hardware below comes from legacy MINERVA's code on the
spectrograph PC (`spectrograph_server.py`, `com.py`, `com.ini`,
`dynamixel.py`, `PyAPT.py`, `chiller.py`) and the vendors' protocol
manuals. **None of it has been tried on the hardware yet**; what must be
confirmed first is listed in "To confirm on the hardware".

## 1. The Alpaca devices

One server, four devices; each is optional (a section absent from the
config is not served).

| Alpaca device | Hardware | Why this interface |
|---|---|---|
| `switch/0` (ISwitchV2) | iodine cell heater | an on/off output, a set point, a temperature and an alarm bit are exactly a Switch's boolean and analog switches, writable or not |
| `filterwheel/0` (IFilterWheelV3) | iodine stage | the stage is used only at **named** positions (`in`, `flat`, `out`), which is a filter wheel; `Position` = -1 while moving gives arrival for free. A Focuser would lose the names, needs integer steps and gives no way to say "not homed"; the rare millimetre move (legacy `i2stage_movef`) is an action, `MoveMM` |
| `covercalibrator/0` (ICoverCalibratorV2) | fiber backlight | the arm that swings the LED disk over the fiber is a cover (closed = over the fiber), the LED is a calibrator (brightness 0 or 1): a flat-panel cover/calibrator is the same mechanism |
| `switch/1` (ISwitchV2) | vacuum gauges, chiller | read-only telemetry. ObservingConditions has fixed weather members (its `Pressure` is the atmosphere's, in hPa) and no place for a vacuum or a chiller; read-only switches carry any named value |

### `switch/0`: the iodine cell heater

| Id | Name | Writable | Value |
|---|---|---|---|
| 0 | `heater_enabled` | yes | 0/1 |
| 1 | `heater_setpoint` | yes | C, `setpoint_min`..`setpoint_max` (default 40..65), step 0.1 |
| 2 | `cell_temperature` | no | C |
| 3 | `heater_alarm` | no | 0/1, the controller's sensor alarm |

The TC200's only output command, `ens`, is a **toggle**. Writing
`heater_enabled` reads the status first, toggles only if it differs, and
reads it again; if the toggle did not take it says so and does **not**
toggle again. A set point is written (`tset=`) and read back.

### `filterwheel/0`: the iodine stage

`Names` are the configured positions. `Position = n` starts the move and
returns; reading `Position` gives -1 while the stage moves, else the index
of the named position it rests at (within `tolerance`, 0.05 mm), else an
`InvalidOperation` error saying why (not homed; between positions; the
last move's failure). Actions:

* `Status`: JSON (mm, counts, status bits, homed, limits, the move in
  progress, the controller's identification, the last fault).
* `Home`: **moves the stage.** Needed once after the controller powers up
  (until then every move is refused). As legacy did, a stage that is
  already homed first moves to 0, then homes. Bounded by `home_timeout`
  (180 s; homing alone takes about 13 s).
* `MoveMM` (`Parameters`: mm): any position in `travel` (0..150 mm, legacy's
  bound).
* `Stop`: a profiled stop.

A move is bounded by `move_timeout` (120 s); one that overruns is stopped
and reported. The server never homes or moves the stage on its own (legacy
power-cycled, homed and moved it on every connect).

### `covercalibrator/0`: the fiber backlight

* `CloseCover`: the arm **in** over the fiber (-45 deg); `OpenCover`: out
  (-90 deg), then the servo's torque off. Each move is legacy's sequence:
  torque off, operating mode 3 (position), torque on, goal position, wait
  until it stops (bounded by `move_timeout`, 5 s). `HaltCover` is not
  implemented (the move takes a fraction of a second).
* `CalibratorOn(1)` / `CalibratorOff`: the LED (`n8` / `f8`). Its state
  cannot be read back; the server reports what it last commanded, and
  turns the LED off on the first connect.
* **Failsafe:** the LED heats the spectrograph, so `max_on` (60 s, as
  legacy) after the last `CalibratorOn` the server turns it off and swings
  the arm out.
* **A servo that does not answer** (as the backlight's servo does today: it
  does not answer PINGs) leaves `CoverState` = Error; `OpenCover` and
  `CloseCover` try a PING and fail at once with "Dynamixel ID 1 did not
  answer PING within 0.5 s on backlight servo: check the servo's power and
  cable, its ID (1) and baud rate (57600), and the serial bridge". The LED
  still works.

### `switch/1`: telemetry (read only)

`spec_pressure` (gauge CG2, the spectrograph) and `pump_pressure` (CG1, the
pump line), mbar; `chiller_temperature`, `chiller_setpoint`,
`chiller_pump_temperature` (C), `chiller_running` and `chiller_fault`
(0/1). `Action("Status")` returns them all as JSON with the chiller's
status and fault bits. Readings are reused for `max_age` (2 s).

## 2. Vacuum: telemetry only

**robofast does not control the vacuum.** The gauge driver can send
exactly two strings, `#  RDCG1` and `#  RDCG2`; nothing in robofast opens
or closes the pump valve or the vent valve, or switches the vacuum pump,
or any of their PDU outlets, and the aux server has no member that could.
The chiller driver likewise sends only its five queries (never its set
point, run or stop commands). Tests check both.

Note what this leaves behind: the legacy server's pressure-logging thread
**closes the pump valve by itself** on a suspected pump failure
(`log_pressures`). If the legacy server stops polling the gauge controller
(it must, for the bridge to own the port: below), that watchdog stops too.
robofast does not take it over; deciding what replaces it comes before
the gauge port moves to the bridge.

## 3. The serial bridge on the spectrograph PC

Not deployed yet (it needs JDE's approval). What it must provide:

* one TCP port per serial port, on the spectrograph PC, reachable from the
  robofast host only;
* **RFC 2217** (preferred): robofast then sets the baud rate, framing and
  RTS/CTS itself. pyserial's own `examples/rfc2217_server.py` runs on the
  PC's Python 2.7 (pyserial 3.5 supports it), one instance per port;
  `ser2net` (telnet/RFC 2217 mode) or a vendor bridge would do too. A raw
  TCP bridge (`socket://`) also works if the bridge itself is set to the
  settings below;
* exclusive use: a Windows COM port has one owner, so the legacy server
  must not open these ports while the bridge has them (it opens the heater,
  gauge, LED and servo ports per command, and holds the stage through
  APT.dll all the time; its pressure logger polls the gauges every 0.5 s).

| Device | Legacy name (`com.ini` / code) | Serial settings | Placeholder URL |
|---|---|---|---|
| iodine cell heater (TC200) | `[I2Heater]` | 115200 8N1, no flow control | `rfc2217://spectrograph-pc:7001` |
| iodine stage (APT, USB FTDI 0403:FAF0 as a COM port) | APT.dll, serial number in `spectrograph_server.ini` `I2SERIAL` | 115200 8N1, **RTS/CTS** | `rfc2217://spectrograph-pc:7002` |
| backlight LED controller | `[backlight]` | 9600 8N1 | `rfc2217://spectrograph-pc:7003` |
| backlight servo (Dynamixel, USB2Dynamixel / U2D2) | `backlight_move_motor` (the COM port in the code, not `[backlightmotor]`) | 57600 8N1 | `rfc2217://spectrograph-pc:7004` |
| vacuum gauge controller | `[gaugeController]` | 19200 8N1 | `rfc2217://spectrograph-pc:7005` |
| chiller (Oasis, CP210x) | `chiller.ini` | 9600 8N1 | `rfc2217://spectrograph-pc:7006` |

The COM port numbers are in the live `com.ini` and code on the PC (they
differ from the copies on GitHub); confirm each in Device Manager.

## 4. Protocols

* **TC200** (`tc200.py`): ASCII, CR-terminated. `stat?` (hex status: bit 0
  enabled, 4/5 unit, 6 sensor alarm, 7 cycle paused), `tact?`, `tset?`,
  `tset=<T>`, `ens` (toggle), `*idn?`. The unit echoes the command and ends
  with a `>` prompt; an answer starting `CMD_` is an error.
* **Thorlabs APT** (`thorlabs_apt.py`): 6-byte little-endian headers, short
  and long messages, host 0x01, controller 0x50. Sent: `HW_NO_FLASH_PROGRAMMING`,
  `HW_REQ_INFO`, `MOD_SET_CHANENABLESTATE`, `MOT_MOVE_HOME`,
  `MOT_MOVE_ABSOLUTE`, `MOT_MOVE_STOP`, `MOT_REQ_STATUSUPDATE` (or, with
  `status_query: "bits"`, `MOT_REQ_POSCOUNTER` + `MOT_REQ_STATUSBITS`).
  Completion messages and the controller's error reports are read as they
  come. Positions are counts / `counts_per_mm`.
* **Dynamixel 2.0** (`dynamixel2.py`): `FF FF FD 00` packets, CRC-16
  (0x8005), byte stuffing; PING, READ, WRITE; writes only to addresses 11
  (operating mode), 64 (torque) and 116 (goal position). Angles to counts
  as legacy: `2047 + round(radians(deg) / (2 pi / 4095))` (-45 deg 1535,
  -90 deg 1023). The packets match legacy `com.py`'s literal ones byte for
  byte (tested).
* **LED controller**: `n8` / `f8` + CR; the answer is ignored, as legacy
  did.
* **Gauge controller**: `#` + address (two spaces) + `RDCG1`/`RDCG2` + CR;
  answer `*` + address + value, or `?...` for an error. mbar, as legacy.
  The command set is InstruTech / Granville-Phillips style (a controller
  with two convection gauges); the model is not named in legacy's code.
* **Oasis chiller**: `TEMP?`, `SETTEMP?`, `PUMPTEMP?`, `STAT1A?`, `FLTS1A?`
  + CR LF, each answered by a bare number; the status and fault bits are
  legacy's (`chiller_status_dict`).

## 5. To confirm on the hardware

In the order they matter:

1. **Stage scale, `counts_per_mm`.** No legacy file has it (APT.dll kept the
   stage's settings). `null` in MINERVA's config, and until it is set every
   stage position and move is refused (`Status` still reads counts). With
   the stage homed and moved to a known position by the legacy software,
   read `Status`'s counts and divide by the millimetres.
2. **The stage controller's model and message set:** legacy's `HWTYPE` 12
   is a BSC101 benchtop stepper driver, but the USB ID 0403:FAF0 is shared by
   APT controllers. Check `Status` `controller` (from `HW_GET_INFO`), that
   the controller is `dest` 0x50, that it answers `MOT_REQ_STATUSUPDATE`
   (else `status_query: "bits"`), and the status bits robofast reads as
   moving (0x10/0x20/0x40/0x80/0x200), homed (0x400) and enabled
   (0x80000000).
3. **Homing direction and time**: about 13 s from near 0; legacy moved to 0
   before homing ("the home position must be away from the motor").
4. **TC200 framing:** the echo and the `>` prompt, and `stat?`'s hex format
   (legacy's own parser would have misread a status with a hex letter).
5. **The servo:** that it answers at all (it does not answer PINGs today),
   its ID (1) and baud (57600), and the arm angles (-45 / -90 deg).
6. **The LED controller's** answer to `n8`/`f8`, if any.
7. **The gauge controller's** model, address (two spaces) and units (mbar
   or Torr).
8. **The chiller's** answers (bare numbers) and its port.
9. Bridge behaviour: RFC 2217 option negotiation (baud, RTS/CTS) through
   whatever bridge is chosen.

## 6. Using it from the KiwiSpec service

Add `"aux"` to the KiwiSpec service's config:

    "aux": {"address": "127.0.0.1:11170", "chiller": true}

Then the heater, the iodine stage, the backlight and the pressures (and,
with `chiller`, the chiller) go to the aux server; everything else (the
camera, file names, headers) still goes to the legacy server. Any device
can stay on the legacy server: `"heater": null`, `"telemetry": null`,
`"stage": false`, `"backlight": false`. The service moves the stage by
name through the aux server's `Names`, and polls `Status` for arrival as it
did with the legacy server.

**FITS headers.** Every frame's header gets `I2TEMPA` (the cell's
temperature) and `I2TEMPS` (its set point), read from whichever device the
service is configured to use, and `I2POSAF` / `I2POSAS` / `I2POSSS` (the
stage). A read that fails is written as `UNKNOWN`, with a warning in the
frame's result; a key is never dropped (legacy's `control.py` wrapped
these reads in `except: pass`).

## 7. Running

    python -m robofast_core.devices.kiwispec_aux_server --config kiwispec_aux.json [--hardware] [--simulate]

`--simulate` puts every port on its simulator. A real port needs
`"allow_hardware": true` in the config **and** `--hardware`; without both,
only `sim`, `loop://` and a verified robofast simulator on loopback are
opened (`python -m robofast_core.devices.kiwispec_aux_sim` serves the
simulators on loopback TCP ports, for the `socket://` path). Starting the
server moves nothing: connecting reads, and turns the backlight LED off.
