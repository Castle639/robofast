# Offset guiding

On a fiber spectrograph the guider normally guides on the target itself.
The fiber takes the core of the star, so the guide camera sees only the
light that *spills* round the fiber hole, as a ring. The **annulus**
centroider (`docs/guider.md`) finds the star's centre from that ring. For a
faint target the spill is too weak to centroid, which is common for TRES's
faint targets and also happens on MINERVA. **Offset guiding** guides on a
brighter star nearby instead. It holds that star at the pixel that puts
the target on the fiber.

Everything here runs in the simulators only. Nothing has guided a real
telescope by an offset star yet.

## How it works

```
setpoint = fiber + S^-1 @ (east, north)
```

- `fiber` is the fiber's pixel on the guide camera, located with the
  backlight or configured.
- `(east, north)` is the guide star's position relative to the target, in
  arcsec on the sky, at the time of the observation.
- `S` is the guide camera's sky matrix: its plate scale, PA and parity, in
  the PA calibration's FITS convention (`docs/rotcal.md`,
  `calibration.fits_sky_matrix`).

The guide loop holds the guide star at `setpoint`. Whatever moves both
stars together (seeing tilt, drift, the mount) is corrected, so the target
stays on the fiber. The controller is the same, and so are the tip/tilt,
the offloading and the loss and re-acquisition logic. Because the guide
star is clear of the fiber hole, the centroider is a plain one: the
windowed centre of mass replaces `annulus`.

Acquisition looks for the **guide star**, not the target. The catalog
identifier or the acquisition cascade (`docs/acquisition.md`) is given the
guide star's coordinates, and its predicted pixel is the setpoint.

`robofast_core/guider/offset.py` handles the geometry. It also covers the
effects that change the offset:

| Effect | Size (60" offset) | Handling |
|---|---|---|
| Proper motion | up to arcsec over years | Both stars are moved to the date of observation (`catalog.propagate`). An explicit offset moves with the target. |
| Differential refraction | ~0.1" at 30 deg altitude, ~0.02" at 60 deg | Computed from ERFA's `refco` constants: along the vertical `(A sec^2 z + 3B tan^2 z sec^2 z) x` the vertical offset, across it `A x` the offset. It is logged and applied, unless the guider config's `offset_guide.refraction` is false. It needs `site`, which the dispatch scheduler sends. |
| Field rotation | depends on the port | The setpoint is recomputed every `offset_guide.update` seconds (default 10). `field_rotation: "none"` is for an equatorial mount (the 60-inch) or a derotated port (MINERVA's FAU rotator runs in derotate mode). `"altaz"` with `sense` is for an alt-az port without a derotator: the PA then follows the change in parallactic angle since the start. |
| Plate scale, PA errors | 0.1 % gives 0.06", 0.1 deg gives 0.1" | These are systematic, so prefer close guide stars. |

Each update moves the setpoint without resetting the controller, as
`POST /target` does. A target step therefore gives no derivative kick.

## Asking for it

A request configuration does it through `guiding_config.extra_params`:

```json
"guiding_config": {"mode": "ON",
                   "extra_params": {"guide": "offset",
                                    "offset_star": {"east": 25.0, "north": -10.0}}}
```

- **`guide`** is `"annulus"`, the default (guide on the target itself), or
  `"offset"`.
- **`offset_star`** is one of:
  - `"auto"`, the default: the guider picks the star from its catalog, as
    described below;
  - `{"east", "north"}`: the guide star's position relative to the target,
    in arcsec on the sky with east as a true angle. It is fixed relative to
    the target;
  - `{"ra", "dec", "pmra"?, "pmdec"?, "epoch"?, "g"?}`: deg ICRS, mas/yr
    and Julian year (default 2000.0), with Gaia G optional.

The dispatch scheduler copies these settings into the observation's
`guide_offset`, together with the target's astrometry and the site. Its
decision record carries them (`"guide": {"on": "offset", ...}`). The
sequencer passes them to the guider's `POST /start` as `offset_guide`. A
static plan can set `guide_offset` on an observation directly.

### MINERVA's sheet

`acquisition_offset_north` and `acquisition_offset_east` map to offset
guiding (`docs/minerva_sheet.md`). Legacy's offset is **the target minus
the guide star**, in arcsec on the sky (no cos dec), so `offset_star` is
`{"east": -east, "north": -north}`. Either column non-zero turns it on.
Legacy needed both to be non-zero, so a pure north or east offset was
silently ignored.

## Choosing the guide star (`"auto"`)

`choose_guide_star` uses the guider's `catalog` (Gaia DR3 and the bright
stars, `docs/sky.md`). It searches the guide camera's field round the
target, at the date of observation. It keeps stars that:

- are brighter than `g_faint` and fainter than `g_bright`, because
  brighter stars saturate the guide exposure. The camera and exposure set
  this, so set it per guider;
- are at least `min_separation` arcsec from the target, which keeps them
  clear of the fiber hole and the target's spill;
- put the setpoint on the camera, with `edge_margin` pixels to spare;
- have no catalog star brighter than their own G + `isolation_dmag`
  within `isolation` arcsec.

Of these it takes the one with the smallest `separation /
separation_scale + max(0, G - g_good)`. Close keeps the systematic errors
small, and bright keeps the centroid noise small. The rejected candidates
are logged with the reason. If no star qualifies, the start is refused
(409) and the guider says why.

## The guider service

The guider's config takes these settings (all optional):

```json
"offset_guide": {"update": 10, "refraction": true, "field_rotation": "none",
                 "sense": 1, "wavelength": 0.65,
                 "auto": {"g_bright": 7.0, "g_faint": 14.5, "g_good": 11.0,
                          "min_separation": 8, "edge_margin": 40, "isolation": 6,
                          "isolation_dmag": 3, "separation_scale": 60}}
```

It also needs:

- the plate scale (`guide.plate_scale` or the PA calibration's);
- the port's PA, from `pa_calibration` or `offset_guide.pa` at start;
- the fiber pixel, configured or located.

The start is refused if any of them is missing.

`GET /status` reports:

- `guide_mode` (`"offset"` or `"target"`);
- `fiber`, the fiber pixel even while the setpoint is elsewhere;
- `offset_guide`: the star, the scale and PA, and `now`, the latest
  solution: the offset east/north, the altitude and parallactic angle, the
  refraction shift, the PA, and the pixel offset and setpoint.

A plain `POST /start` afterwards guides on the target at the fiber again.

## In the FITS headers

`GuiderRemote` copies these values into the sequencer's registry, so they
land in the headers of the frames:

| Keyword | Value |
|---|---|
| `GUIDMODE` | `offset` or `target` |
| `GSSOURCE` | `offset`, `coordinates` or `catalog` |
| `GSRA`, `GSDEC` | the guide star, deg ICRS, at its epoch |
| `GSMAG` | its Gaia G, when known |
| `GSID` | its Gaia DR3 source_id (`catalog`) |
| `GSOFFE`, `GSOFFN` | the guide star minus the target, arcsec, now |
| `GSPIXX`, `GSPIXY` | the setpoint (guide-camera pixels) |
| `GSDAR` | the differential refraction across the offset, arcsec |

`XSCIFIB`/`YSCIFIB` stay the fiber's pixel. The sequencer's `guiding`
event also records `guide_mode`, `guide_star` and `guide_offset`.

## Legacy

- **MINERVA (`minerva-control`).** `control.py` turned the sheet's offsets
  into pixels with the rotator PA, but used them only to *choose* which
  star `zwo.py` reported as the guide star, as "the star at brightest +
  offset". A scalar `np.linalg.norm` over all the stars made that choice
  always star 0, so the selection most likely never worked. The setpoint
  stayed on the fiber, so the chosen star, not the target, would have been
  guided onto the fiber. There was no proper motion, refraction or cos dec
  handling.
- **tres-guider.** It moved the setpoint by a pixel offset from the fiber,
  but searched for the star at the opposite sign and assumed `PA = 0`.

## Tests

`tests/core/test_offset_guiding.py` covers:

- **Geometry:** the pixel offset inverts the sky matrix, offsets are true
  angles, proper motion, refraction (size, direction, and the shrink
  across the vertical), and field rotation.
- **Choosing a star:** saturation, crowding, off-camera stars, and the
  trade between bright and close.
- **The guide loop, on a simulated TRES-like faint target with a fiber
  hole.** Guided by a star 25" east and 15" north, the target stays within
  0.25 px rms of the fiber while the image drifts ~5 px in 20 s. Guiding on
  the target itself fails to acquire. With field rotation of 0.05 deg/s
  the setpoint updates keep the target on the fiber, and a stale setpoint
  drags it off.
- **The HTTP service and its FITS values.**
