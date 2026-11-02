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
(`tres_thar`, `tres_flat`, made from the generic templates below; every
value a placeholder, unconfirmed), and
`sites/flwo60/calibration.example.json` runs them after the biases and
darks.

## Templates: generic sequences

Lamp calibrations, biases and darks are much the same everywhere, so
robofast ships a few generic sequences as **templates**: a step list with
parameters. A site instantiates one with its own values instead of writing
the steps out:

```json
"sequences": {
  "tres_thar": {
    "template": "arc_set", "instrument": "camera_spectrograph",
    "lamp": "ThAr1", "warmup": 60, "exptime": 30, "count": 3, "object": "ThAr",
    "calstage": "in", "calstage_after": "out"
  },
  "biases": {"template": "bias_set", "count": 10}
}
```

| Template | Parameters (required in bold) | Steps |
|---|---|---|
| `arc_set` | **`lamp`**, **`exptime`**, **`count`**, `warmup`, `calstage`, `calstage_after`, `filter`, `object`, `instrument` | the stage to `calstage`, the filter, the lamp on (its `warmup`), `count` `ARC` frames, the lamp off, the stage to `calstage_after`; cleanup: the stage to `calstage_after` |
| `lamp_flat_set` | the same | the same with `LAMP_FLAT` frames |
| `bias_set` | **`count`**, `filter`, `object`, `instrument` | the filter, `count` `BIAS` frames |
| `dark_set` | **`exptime`**, **`count`**, `filter`, `object`, `instrument` | the filter, `count` `DARK` frames |

An optional parameter left out is null, and a null leaves out what it
fills: a step whose step type's value is null (no `calstage`: the stage is
not moved, and the cleanup is empty), or an option (no `warmup`: the lamps
entry's). `instrument` defaults to `imager`. Besides the parameters, an
instance may give `telescopes` and `description` (default: the
template's). The sequence it makes is then loaded and checked exactly as
one written out (below): a template adds no way around a check.

**A site's own templates, or its version of a built-in.** The enclosure
config's `sequence_templates` adds templates, and one named like a built-in
replaces it for that enclosure:

```json
"sequence_templates": {
  "arc_set": {
    "params": {"lamp": {}, "exptime": {}, "count": {"default": 1}},
    "steps": [
      {"lamp": "$lamp", "on": true},
      {"expose": {"type": "ARC", "exptime": "$exptime", "n": "$count"}},
      {"lamp": "$lamp", "on": false}
    ],
    "cleanup": []
  }
}
```

`params`: each parameter `{}` (required) or `{"default": value}`. A string
`"$name"` anywhere in `steps`, `cleanup`, `instrument` or `description`
stands for parameter `name`'s value (the whole string only, so a number
stays a number); a `$name` that is not a parameter is refused at load.
Parameters may not be named `template` or `telescopes`. A plug-in registers
a template in `robofast_core.sequences.SEQUENCE_TEMPLATES` (a
`SequenceTemplate`; docs/plugins.md).

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
* **With other values:** an observation's `sequence_params` (`{"count":
  5}`) makes a sequence from a template again with those parameters,
  checked as at load (a static plan's when the sequencer starts; else
  before the first step: a failure moves and switches nothing).
* **Requests** (below).

## Requests naming a site sequence

An OCS `ARC` or `LAMP_FLAT` configuration names its lamp
(`optical_elements.lamp`; the `arc` and `lamp_flat` kinds,
docs/pulizzi.md) -- or, instead, one of the site's sequences in
`extra_params.sequence`:

```json
{"type": "ARC", "instrument_configs": [
  {"exposure_time": 30, "exposure_count": 3, "extra_params": {"sequence": "tres_thar"}}]}
```

The sequence runs with the request's `exposure_time` and the frames still
to take as its template's `exptime` and `count` (an observation of kind
`sequence` with `sequence_params`); everything else (the lamp, the
warm-up, the stage, the cleanup) is the site's. So the observers' own
procedure runs for an outside request, with the same safety. The rules:

* the dispatch limits list the sequences requests may name per telescope
  (`telescopes.<name>.sequences`; `sites/flwo60/dispatch.example.json`
  lists `tres_thar` and `tres_flat`), and only those telescopes take the
  configuration;
* each listed sequence must be in the enclosure config, run on that
  telescope, be dark, be made from a template that takes `exptime` and
  `count`, and its lamp frames must all be of one type: checked when the
  sequencer and the request API start (each refuses to start otherwise);
* at submission the request API refuses a sequence no telescope lists, one
  whose lamp frames are not the configuration's type (an `ARC` naming a
  flat set), and values that break its checks (`exposure_count` 40 that
  would keep the lamp on past its `max_on`), each with the reason; an
  enclosure with neither lamps nor sequences gets the no-lamps reason and
  its `help.no_lamps`;
* a configuration names the lamp or the sequence, not both.

A fixed sequence (written out, not from a template) cannot be named by a
request: it has no exposure time or count to substitute.

MINERVA defines no sequences (and its dispatch limits list none): its calibrations (the calibration plan's
biases and darks, and KiwiSpec's own frames) run as before.

## Events

`sequence_start`, `sequence_step` (index, step type), the steps' own
(`calstage_selected`, `lamp_on`, `lamp_warmup`, `lamp_off`,
`exposure_started`, ...), `sequence_done`, `sequence_cleanup_done` (`ok`),
and `sequence_cleanup_failed` (critical) -- each with the telescope, the
generation and the sequence's name.
