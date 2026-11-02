# Sequences

A site's or instrument's routine that is only an ordering of operations
robofast already drives -- the calibration stage, a lamp on and off, a
wait, frames -- is a **sequence**: a named step list in the enclosure
config, run as an observation of kind `sequence`
(`robofast_core/sequences.py`; design.md 3.10 (c)). Adding one is
configuration: no code, no fork. What needs new code is a plug-in
(docs/plugins.md), which can also add step types.

## Defining one

In the enclosure config (the file the supervisor and the sequencer read):

```json
"sequences": {
  "tres_thar": {
    "description": "TRES ThAr arcs",
    "instrument": "camera_spectrograph",
    "steps": [
      {"calstage": "in"},
      {"lamp": "ThAr1", "on": true, "warmup": 60},
      {"expose": {"type": "ARC", "exptime": 30, "n": 3, "object": "ThAr"}},
      {"lamp": "ThAr1", "on": false},
      {"calstage": "out"}
    ],
    "cleanup": [{"calstage": "out"}]
  }
}
```

* `steps` -- run in order (a bare list is the steps, with no cleanup);
* `cleanup` -- run after the steps, always (below);
* `telescopes` -- the telescopes it may run on (default: every one);
* `instrument` -- the instrument type its frames are taken with
  (`robofast_core/instruments.py`; default `imager`): its frame types are
  checked against what that type takes;
* `description` -- for people; keys starting with `_` are comments.

`sites/flwo60/flwo60.json` has the TRES ThAr and flat sets
(`tres_thar`, `tres_flat`; every value a placeholder, unconfirmed), and
`sites/flwo60/calibration.example.json` runs them after the biases and
darks.

## The steps

A step is an object whose one step-type key names it; its other keys are
the step's options. Each step type is an entry of
`robofast_core.sequences.SEQUENCE_STEPS` (a registry, so a plug-in adds its
own: subclass `StepType`, register it).

| Step | Does | Needs on the telescope | In a cleanup? |
|---|---|---|---|
| `{"calstage": POS}` | the calibration stage to a named position (docs/tres_frontend.md) | `calstage` | yes |
| `{"filter": NAME}` | the filter wheel to a named filter | `filterwheel` | yes |
| `{"port": NAME}` | the M3 port | `port_action` | no |
| `{"adc": "track"}`, `{"adc": "off"}` | ADC tracking on (and settled, as for light frames), or off with the prisms neutral (docs/adc.md) | `adc` | `off` only |
| `{"lamp": NAME, "on": true, "warmup": s}` | a lamp on, then its warm-up (default: the `lamps` entry's) | `lamps`, with that lamp | no |
| `{"lamp": NAME, "on": false}` | a lamp off | `lamps` | yes |
| `{"focus": POS}`, `{"focus": {"delta": STEPS}}` | the focuser, absolute or relative | `focuser` and `autofocus` (the sequencer moves a focuser only then) | no |
| `{"wait": s}` | a wait | | yes |
| `{"expose": {"type": T, "exptime": s, "n": N, "object": NAME}}` | `N` frames with the telescope's camera, saved like any other (`IMAGETYP` `ARC`, `FLAT`, `BIAS`, `DARK`, `LIGHT`) | a camera | no |
| `{"spectrograph": [{"kind": K, "exptime": s, "n": N}, ...]}` | the enclosure's spectrograph service's own calibration frames, as one job (docs/kiwispec.md) | the enclosure's `spectrograph` | no |

`expose` types: `BIAS` (no exposure time), `DARK`, `ARC` and `LAMP_FLAT`
(the shutter opens on a lamp, which must be on: no sky light), and
`EXPOSE` (sky light). A sequence with an `EXPOSE` step is a **light**
observation: the sequencer runs it like any light observation -- the
enclosure open under a lease, the telescope unparked, slewed to the
observation's target (and guiding if it asks) -- then its steps. Any other
sequence is **dark**: run in the closed phase, with the enclosure shut and
the telescope parked. The sequencer sets an observation's `light` from its
definition, whatever the plan said.

Lamps: the switch's own `max_on` still switches a lamp off; before each
`ARC` or `LAMP_FLAT` frame (and after the last) every lamp the sequence has
switched on is checked to be on still, or the observation fails
(`lamp went off`). Unlike the `arc` and `lamp_flat` kinds, a sequence does
not switch other lamps off for you: say so with a step.

## The cleanup, always

A sequence's `cleanup` steps run after its steps whenever they end:
finished, failed (a device error, a lamp gone off), or cancelled -- the
lease revoked (unsafe weather, an operator's close, a mode switch), a
preemption, the sequencer stopping. Only steps that cannot let light in or
move the telescope may be cleanup steps (the table above), and they run with
grants of their own, since a cancelled generation's grants are fenced.

Every lamp the sequence switched on is switched off first, whatever the
cleanup lists, through a grant of its own (switching a lamp off is never
harmful). On a cancellation the lamps go off before the observation's task
ends; the rest of the cleanup (the stage moving out can take a minute)
finishes in the background, and the generation's own cleanup waits for it
(bounded by `sequencer.sequence_cleanup_timeout`, 120 s) before it parks,
then switches every lamp off again (`LampsOff`). A cleanup step that fails
is a critical event and an error in the cleanup report; after a sequence
that otherwise finished, it fails the observation.

## The checks

**When the config is loaded** (the sequencer service refuses to start;
every message names the step's path, e.g. `sequences.tres_thar.steps[2]
(on tillinghast): a ARC frame needs a lamp on (a lamp step before it)`),
each sequence is checked on every telescope it may run on:

* every step is a known step type, with known options and good values;
* the telescope has the device each step needs, and the lamp each lamp
  step names;
* each `expose` type is one the `instrument` takes; `ARC` and `LAMP_FLAT`
  frames come with a lamp on;
* no lamp is left on at the end, or switched on twice;
* a lamp is not kept on longer than its `max_on` (when the telescope's
  `lamps` entry repeats the switch's: `lamps.lamps.<name>.max_on`), counting
  warm-ups, waits and frames with the lamps entry's `frame_overhead`;
* the cleanup holds only cleanup steps.

A static or calibration plan's sequence observations are checked too: the
name exists, the telescope is one it runs on, and a calibration plan's are
dark (it never opens).

**Before the first step**, against the devices themselves (reads only):
the stage's and the wheel's position names (`Names`), the lamp switch's
lamps, and each lamp's time on against the switch's own `max_on`. A
sequence that fails here has moved and switched nothing.

## Running one

* **A static plan:** a target `{"name": "thar", "kind": "sequence",
  "sequence": "tres_thar"}` (its other fields as for any observation: a
  target for a light sequence, `allow_partial` is not used).
* **A calibration plan:** `"sequences": ["tres_thar", "tres_flat"]` (each on
  every telescope), or `{"sequence": name, "telescopes": [...]}`; taken
  after the biases and darks, with the enclosure closed.
* **Requests: not in this step.** An OCS request is parametric: an `ARC`
  or `LAMP_FLAT` configuration says which lamp, how long and how many, and
  the `arc` and `lamp_flat` kinds (docs/pulizzi.md) already run exactly that
  procedure with the same safety (the stage before the lamp, the warm-up,
  `max_on` checked first, the lamp off in a `finally` and in every
  cleanup). A sequence is the site's fixed procedure, so it belongs in the
  site's plans. Mapping a request type to a site's sequence (with the
  request's exposure time and count substituted) is a question for JDE
  (docs/design.md 3.10).

MINERVA defines no sequences: its calibrations (the calibration plan's
biases and darks, and KiwiSpec's own frames) run as before.

## Events

`sequence_start`, `sequence_step` (index, step type), the steps' own
(`calstage_selected`, `lamp_on`, `lamp_warmup`, `lamp_off`,
`exposure_started`, ...), `sequence_done`, `sequence_cleanup_done` (`ok`),
and `sequence_cleanup_failed` (critical) -- each with the telescope, the
generation and the sequence's name.
