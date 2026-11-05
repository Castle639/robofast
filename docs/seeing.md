# Seeing: where it comes from

The seeing (FWHM, arcsec) is used in two places:

* **the guiding recommendation**: for `guide: "auto"` it decides annulus
  or offset guiding (docs/offset_guiding.md). Bad seeing spills more of the
  target's light round the fiber, so annulus guiding works on fainter
  targets;
* **the `max_seeing` constraint**: a request configuration's
  `constraints.max_seeing` (OCS, arcsec) makes it observable only while the
  seeing is at most that. JDE (2026-11-02): "schedulers may use seeing as a
  selection criterion, too".

Both read the same **seeing source**. Which source is configuration: the
dispatch plan's `seeing` entry. The code is `robofast/seeing.py`.

## The default: the guiders' live FWHM

No site has a seeing monitor robofast reads (MINERVA has none on its
weather pages). Every guider measures the seeing anyway, so by default the
seeing is the guiders' FWHM:

1. While guiding, each guide loop reports `seeing_arcsec`: the FWHM of the
   star it guides on, times `plate_scale`. On a fiber (the annulus
   centroider) the star's core is in the hole, and a fit to the spill
   round it is not the seeing. There the loop reports the width the
   centroider learned from a star clear of the hole, during acquisition.
   It reports nothing when that width is configured (`fwhm`), since a
   fixed number is not a measurement.
2. The guider publishes it as `<telescope>.guide_seeing` (FITS `GUIDSEE`,
   arcsec) in the sequencer's value registry, through `GuiderRemote`,
   every `poll_interval`. The sequencer hands its registry to the plan,
   and the plan's seeing source listens there. A guider publishes even
   without a frame pipeline (an `images` entry) when the source asks for
   it.
3. The source keeps at most one sample per telescope every
   `sample_interval` (30 s), stamped with the scheduler's clock. The
   seeing is the **median** of the samples in the last `window` (30 min),
   from all telescopes together (the site's seeing), times `factor`. The
   median ignores a frame or two of a bad star.
4. It is **stale** (no value) with fewer than `min_samples` (3), or when
   the newest sample is older than `max_age` (15 min): after a long slew,
   a calibration block, or a closed dome.

With no value, the **fallback** answers:

* for `guide: "auto"`, the instrument's guiding model's `seeing`
  (`instruments.<name>.guiding.seeing`);
* for `max_seeing`, the entry's `fallback` (none by default). With no
  fallback either, `unknown` decides: `pass` (the default: the constraint
  does not hold a request back when nothing is known) or `fail`.

```json
"seeing": {
  "source": "guider",
  "window": 1800,
  "max_age": 900,
  "min_samples": 3,
  "sample_interval": 30,
  "factor": 1.0,
  "telescopes": ["t1", "t2"],
  "fallback": null,
  "unknown": "pass"
}
```

Every key is optional; no entry means these defaults. `telescopes`
limits the guiders listened to (default: all). `factor` scales the
guiders' FWHM if a site finds its guide optics broaden it (measure that
against the science camera before setting it).

### What it is not

* The guider's FWHM includes its own optics and focus. It is a good
  measure of *changes* in the seeing, and close to the seeing on a
  well-focused guide camera. It is not a DIMM.
* At the start of the night, and after the guiders have been idle for
  `max_age`, there is no live value. A `max_seeing` request is then let
  through (`unknown: pass`) unless the site sets a `fallback` or
  `unknown: fail`.
* A visit is checked again before each of its observations, like every other
  hard constraint. A visit whose remainder needs better seeing than the
  median now is abandoned.

## Other sources

`source` is a key of the registry `robofast.seeing.SEEING_SOURCES`
(docs/plugins.md). Core's entries:

| Source | Options | What |
|---|---|---|
| `guider` | above | the guiders' live FWHM (the default) |
| `fixed` | `value` (arcsec) | a constant: a site with no guider, or a test |

A plug-in adds another source, for example a DIMM or a weather feed's
seeing:

```python
from robofast.seeing import SEEING_SOURCES, Seeing

class Dimm:
    name = "mysite.dimm"

    def __init__(self, options, clock):
        self.url = options["url"]
        self.clock = clock

    def current(self, now):
        value, age = read_my_dimm(self.url)   # arcsec, seconds
        return None if age > 600 else Seeing(value, self.name, samples=1, age=age)

def register():
    SEEING_SOURCES.register("mysite.dimm", Dimm)
```

`current(now)` returns a `Seeing` or None (stale: the fallback answers).
An exception is logged and treated as None. A source that wants the
sequencer's value registry has `bind(registry)` and returns True. Then the
guiders publish into that registry too.

## Where it is recorded

* the decision record (docs/design.md 3.7): every `decision` and `idle`
  entry has `seeing`: `{"value", "source", "samples", "age"}`, or null
  when nothing is known. With the fallback, `source` is `fallback`;
* a `guide: "auto"` decision's `guide.seeing`: the seeing the
  recommendation used. The recommendation's `conditions.seeing` holds the
  same value;
* a rejected candidate's reason: `seeing 2.50" (guider) > max_seeing 2"`;
* the FITS headers: `GUIDSEE`, the seeing the frame's own guider measured.

## Tests

`tests/core/test_seeing.py` covers:

* the median, the window, staleness and the per-telescope sampling;
* collection from the value registry;
* the guide loop's report: a star's FWHM; on a fiber, only a learned
  width;
* the config and its errors, and a plug-in source that fails into the
  fallback;
* `max_seeing` rejecting and then accepting as the seeing improves, and
  the decision record;
* `unknown: pass` and `fail`;
* `guide: "auto"` flipping from offset to annulus guiding when the
  guiders measure bad seeing;
* the sequencer service binding the guiders to the plan.
