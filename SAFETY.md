# Safety

robofast commands real hardware: telescope mounts, focusers, rotators,
domes and roll-off roofs, power distribution units, cameras and
spectrographs. A bug, a wrong configuration, a network fault or a device that
does not do what it reports can move that hardware in ways that damage it,
the building around it, or a person nearby.

**robofast is not safety-certified software, and nothing in it is a safety
system.** It is provided "as is", without warranty of any kind (see
[LICENSE](LICENSE)). Whoever deploys it is the operator of the hardware and
is responsible for its safe operation.

## What the operator must provide

- **Independent hardware protection.** Mount limit switches and
  firmware limits, enclosure controllers that close on their own (a
  heartbeat or watchdog in the controller, a rain sensor wired to it),
  emergency stops, and power that can be cut by hand. robofast's own checks
  are a second layer; they must never be the only one.
- **A safe physical setup.** Cable wraps, collision zones between
  telescopes and with the enclosure, and the limits in each site
  configuration must be measured and verified on the hardware, not taken
  from an example file.
- **Site access control.** Keep people away from moving equipment, or make
  sure it cannot move while they are near it.
- **Testing before trusting.** Try every new configuration, driver or
  version with the enclosure closed and someone watching, starting from
  the simulators (`robofast_core/testing`) and small motions.
- **Supervision suited to the site.** Decide, for your site, when an
  operator must be on duty (`docs/modes.md`).

## How the documentation describes safety features

The documentation and code comments describe what the software is
**designed** to do: for example, that the safety supervisor opens an
enclosure only when the weather is safe and a sequencer holds a lease, or
that a server refuses a target below its altitude limit. These describe
intended behavior under the conditions the code anticipates. They are not
guarantees that the hardware will never be damaged or that the enclosure
will never be open in bad weather.

## Reporting a safety problem

If you find a bug that could move hardware unexpectedly or leave an
enclosure open, please open an issue (or, if it should not be public yet,
contact the maintainer privately) with the version, the configuration and
what happened.
