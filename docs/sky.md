# The sky: catalog, simulator, plate solving

robofast knows where the stars are (`robofast/catalog.py`), can render
what a camera on a mis-pointing telescope sees (`robofast/testing/skysim.py`),
and can work out from an image where the telescope really points
(`robofast/astrometry.py`). Every light frame can be solved as it is
written, with its WCS and photometric zero point in the header
(`robofast/framesolve.py`). The pieces are pure numpy plus astropy
(already a dependency); nothing needs the network once the catalog cache
and the data directory are filled (section 1, "Pre-filling a host").

## 1. The catalog

`Catalog.cone(ra, dec, radius, epoch=..., mag_limit=...)` returns a `Stars`
table: RA/Dec (ICRS, deg) moved to `epoch` by proper motion, `pmra`/`pmdec`
(mas/yr, `pmra` with cos dec), Gaia `g`, `bp_rp`, an estimated Johnson `v`
(Riello et al. 2021), `source_id` and the `source` of each row, brightest
first. Sources, in order:

| Source | What | Where | Network |
|---|---|---|---|
| Gaia DR3 tiles (`TileCache`) | every star with G < 16 | the cache directory | fetched on demand, then never |
| bright stars (`bright_stars()`) | every Gaia DR3 star with G < 6.5 (12 119) | `<cache_dir>/bright_stars.npy` if present, else the data directory (485 kB) | downloaded once from Zenodo |
| Hipparcos supplement | the Hipparcos stars Gaia lacks (the very brightest, G < ~3) | the cache directory | `build_catalog.py hipparcos` |
| WDS (`DoubleStars`) | the Washington Double Star catalog (158 000 pairs) | the cache directory | `build_catalog.py wds` |
| VizieR (`VizierSource`) | Gaia DR3 for a field nothing cached covers | -- | every query; needs astroquery (optional) |

The tiles come from one **catalog source**, `catalog.source` (below,
"Catalog sources"): Gaia DR3 by default; UCAC4 or 2MASS for a user whose
use Gaia's non-commercial licence does not cover.

Bright-catalog stars are added to a Gaia answer only where Gaia has no star
within 3 arcsec. A cone is widened by 3 arcsec/yr times the epoch difference
before it is cut, so a fast-moving star that has moved into the field is
found (`Catalog.max_pm`).

Offline: a query whose tiles are not cached and cannot be fetched raises
`CatalogUnavailable` (never a silent empty field); `cone(...,
bright_only=True)` uses only the bright stars and answers with no tile and
no network once `bright_stars.npy` is in place. If the bright-star file is
missing and cannot be downloaded, the catalog logs one warning and works
from its tiles alone (only `bright_only` queries, which then raise
`CatalogUnavailable`, and the few very bright stars Gaia's tiles lack are
lost).

### Cache layout

```
<cache_dir>/gaia-dr3-g16-hpx7/manifest.json          what, licence, epoch, columns
<cache_dir>/gaia-dr3-g16-hpx7/<pix // 1024:03d>/<pix:06d>.npy
<cache_dir>/bright_stars.npy                         optional (else the data directory's)
<cache_dir>/hipparcos-not-in-gaia.npy                optional
<cache_dir>/wds.npy                                  optional
```

Tiles are HEALPix pixels, nested scheme, level 7 (nside 128: 196 608 tiles
of 0.21 deg2, about 0.46 deg across). Gaia's `source_id >> 35` is the
star's level-12 nested HEALPix index, so a tile is one `source_id` range
query on the Gaia archive (clustered on it). Each file is a numpy structured
array (`source_id` i8, `ra`, `dec` f8, `pmra`, `pmdec`, `g`, `bp_rp` f4; 36
bytes a star), written atomically. Sizes (G < 16): a high-latitude tile
holds ~60-200 stars (2-7 kB), one near the Galactic plane ~1000-3000 (36-100
kB); the whole sky would be ~2-3 GB, but only the tiles of fields actually
observed are ever fetched. The Gaia archive takes 1-2 minutes per query
(fixed overhead), so `build_catalog.py` fetches several tiles at once;
filling a night's targets ahead of time is the intended use.

`cache_dir` comes from the `catalog` config entry, else
`$ROBOFAST_CATALOG_DIR`, else `~/.cache/robofast/catalog`:

```json
"catalog": {"source": "gaia", "allow_noncommercial": true,
            "cache_dir": "/var/lib/robofast/catalog", "level": 7, "mag_limit": 16,
            "fetch": true, "live": false}
```

`source` and `allow_noncommercial` are described next; `archive_url`
overrides the source's own service (the Gaia archive, IRSA).

### Catalog sources

`robofast.catalog.CATALOG_SOURCES` is a registry (docs/plugins.md):
core registers three, and a plug-in may add its own (a `CatalogSource`:
its licence, epoch, tile directory and fetcher).

| `source` | Catalog | Positions | Proper motions | Magnitudes (`g`, `bp_rp` in the tiles) | Fetched from |
|---|---|---|---|---|---|
| `gaia` (default) | Gaia DR3 | J2016.0 | yes | Gaia G, BP-RP | the ESA Gaia archive (TAP) |
| `ucac4` | UCAC4 (USNO; Zacharias et al. 2013), to R ~ 16 | J2000.0 | yes (1-6 mas/yr) | G estimated from APASS B, V; else 2MASS J, Ks; else UCAC's model magnitude | IRSA TAP, `ucac4_sources` |
| `2mass` | 2MASS All-Sky Point Source Catalog (Skrutskie et al. 2006) | as observed, 1997-2001 (taken as J2000.0) | **none** | G and BP-RP estimated from J, Ks | IRSA TAP, `fp_psc` |

Every source fills the same tile records, so the rest of robofast does not
know which it has: limits are in (estimated) G, the plate solver ranks by
it, the guider chooses guide stars by it, the simulator puts it through
each port's zero point, vetting compares it. The estimates use the Gaia
EDR3 documentation's photometric relations (section 5.5.1, tables 5.7 and
5.8; Riello et al. 2021): `G - V` from `B - V`, `G - Ks` and `BP - RP`
from `J - Ks` (`robofast/catalog_sources.py` has the coefficients).
Measured against Gaia DR3 on one high-latitude tile (2026-11-02, the 81
Gaia stars with G < 16 around (213.0, +62.3)): UCAC4 matched 74, estimated
G off by -0.01 mag median, 0.12 rms, BP-RP 0.12 rms, positions 0.05"
median; 2MASS matched 77, G -0.02 median, 0.15 rms, positions 0.10" median
(their stars' motion since 2000 included). Good enough for guide-star
limits, plate solving and simulated fields; not photometry.

**2MASS has no proper motions**: a star moving 50 mas/yr is 1.3" from its
2MASS position in 2026. Plate solving tolerates that (the solver's match
radius is larger); for offset guiding on a fast-moving star, and the
acquisition-offset check of MINERVA's sheet (`docs/minerva_sheet.md`),
prefer UCAC4. Neither carries the Gaia-lacking bright supplement or a
bright-star list unless you build one: `build_catalog.py bright --source
ucac4 --cache DIR` writes `ucac4_bright_stars.npy` (UCAC4 includes the
Hipparcos and Tycho-2 bright stars), which `bright_only` queries (pointing
models) then use.

```sh
python scripts/build_catalog.py tiles --source ucac4 --cache DIR --field 212.72 62.52 0.4
python scripts/build_catalog.py bright --source ucac4 --cache DIR
```

Each source has its own tile directory under the cache
(`ucac4-g16-hpx7/`, `2mass-psc-g16-hpx7/`), and its manifest records the
source, licence and whether it is non-commercial.

### Licences

| Data | Licence | Commercial use | Source of the terms |
|---|---|---|---|
| Gaia DR3 (tiles, `bright_stars.npy`, the Zenodo record, the live VizieR service) | CC BY-NC 3.0 IGO | only with ESA's prior authorisation (request to data.licences@esa.int) | <https://www.cosmos.esa.int/web/gaia-users/license>; <https://www.cosmos.esa.int/web/esdc/terms-and-conditions> |
| Hipparcos stars Gaia lacks | fetched from the ESA Gaia archive, whose terms (CC BY-NC 3.0 IGO) apply | as Gaia | <https://www.cosmos.esa.int/web/esdc/terms-and-conditions> |
| UCAC4 | a US Government work (US Naval Observatory); "free of charge"; cite Zacharias et al. 2013, AJ 145, 44 and IRSA's DOI 10.26131/IRSA17 | no restriction stated | the UCAC4 readme (`readme_u4.txt`, distributed with the catalog); <https://irsa.ipac.caltech.edu/data/USNO/UCAC4/overview.html> |
| 2MASS Point Source Catalog | public: IRSA's public data have "no usage restrictions"; acknowledgement and citation (Skrutskie et al. 2006, AJ 131, 1163) requested | no restriction stated | <https://irsa.ipac.caltech.edu/data_use_terms.html>; <https://ipac.caltech.edu/2mass/releases/allsky/faq.html> (the acknowledgement) |
| WDS | a US Government work (US Naval Observatory) | yes | (above) |

UCAC4 and 2MASS are US Government-funded catalogs without a licence of
their own; neither states a restriction on commercial use, and both ask
to be acknowledged. robofast does not give legal advice: a commercial user
should read these terms (and, for 2MASS, can confirm with IRSA's helpdesk)
before relying on them.

**Refusing non-commercial data.** `"allow_noncommercial": false` in a
`catalog` entry, or `ROBOFAST_ALLOW_NONCOMMERCIAL=0` in the environment
(a floor for the whole host: no config turns it back on), makes robofast
refuse every non-commercial file: a Gaia source is an error when the
config is loaded (`NonCommercialRefused`, saying why and what to do), as
is the live VizieR service; the Gaia bright-star list and the Hipparcos
supplement are never read; the environment variable also stops the
Zenodo download of the Gaia extracts (`python -m robofast.zenodo
fetch` skips that record). The test suite's own fixtures are exempt only
because the tests do not set it. Configure the `catalog` entry of every
service that has one (guider, frame solver, vetting, pointing model,
rotator calibration) or, simpler, set the environment variable in the
systemd units.

The acknowledgements, for anything published with these data: Gaia's
(below); for UCAC4, cite Zacharias et al. 2013; for 2MASS: "This
publication makes use of data products from the Two Micron All Sky
Survey, which is a joint project of the University of Massachusetts and
the Infrared Processing and Analysis Center/California Institute of
Technology, funded by the National Aeronautics and Space Administration
and the National Science Foundation."

```sh
python scripts/build_catalog.py tiles --cache DIR --targets sites/minerva/targets.example.json
python scripts/build_catalog.py tiles --cache DIR --field 212.72 62.52 0.4
python scripts/build_catalog.py wds --cache DIR
python scripts/build_catalog.py hipparcos --cache DIR
python scripts/build_catalog.py bright --cache DIR
```

### What is downloaded, from where

robofast's repository is BSD 3-Clause and ships no catalog data. Gaia data
are distributed by ESA under **CC BY-NC 3.0 IGO**
(<https://www.cosmos.esa.int/web/gaia-users/license>), which allows
redistribution only for non-commercial use, with attribution: not
compatible with BSD, so nothing Gaia-derived is in the repository (JDE
decision 2026-10-31). Everything is fetched on first use and cached:

| What | Licence | Fetched from | Cached in |
|---|---|---|---|
| Gaia DR3 tiles (G < 16) | CC BY-NC 3.0 IGO | the ESA Gaia archive (TAP), per tile, as needed | `<cache_dir>` (catalog config) |
| UCAC4 or 2MASS tiles (`source: ucac4` / `2mass`) | US Government catalogs (above, "Licences") | IRSA (TAP), per tile, as needed | `<cache_dir>` |
| `bright_stars.npy` (Gaia DR3, G < 6.5) | CC BY-NC 3.0 IGO | Zenodo record `gaia_extracts` (pinned by size and md5 in `robofast/zenodo_assets.py`) | the data directory |
| `test_fixture_tiles.npz` (Gaia DR3 tiles of the five test fields) | CC BY-NC 3.0 IGO | the same Zenodo record; read by the test suite only | the data directory |
| Hipparcos stars Gaia lacks | ESA; terms not stated plainly, so never redistributed | the Gaia archive (`build_catalog.py hipparcos`) | `<cache_dir>` |
| WDS (Washington Double Star catalog) | US Naval Observatory, a US Government work (public domain) | its home at Georgia State (`build_catalog.py wds`) | `<cache_dir>`; 15 of its lines are in the test data |

The Zenodo record holds a single copy of the files robofast would
otherwise have to bundle, so the Gaia archive is not queried for them
(once, by `build_catalog.py bundle`, when a new version is made). Until
the record is published and pinned (docs/zenodo.md), nothing is
downloaded: `bright_stars()` uses a local copy if one is in place
(`build_catalog.py bright --cache DIR`) and otherwise the catalog runs
without it.

**The data directory** is `$ROBOFAST_DATA_DIR`, else
`$XDG_CACHE_HOME/robofast/data`, else `~/.cache/robofast/data`, with one
subdirectory per Zenodo record (`gaia_extracts/bright_stars.npy`). A
file of the pinned size is used as it is; a download lands on a temporary
name, is checked against the pinned size and md5 and only then renamed
into place (`robofast/zenodo.py`). `ROBOFAST_OFFLINE=1` forbids
downloads. On a host where home directories are small or shared, point
`ROBOFAST_DATA_DIR` (and the catalog's `cache_dir`) at local disk, e.g.
`/var/lib/robofast/data` in the systemd units' environment.

**Pre-filling a host** that will run offline (a control host at a site):

```sh
# on the host, while it has network (or on any machine, then copy the tree):
export ROBOFAST_DATA_DIR=/var/lib/robofast/data
python -m robofast.zenodo fetch       # every pinned Zenodo file
python -m robofast.zenodo status      # what is there, what is missing
python scripts/build_catalog.py tiles --cache /var/lib/robofast/catalog \
    --targets sites/minerva/targets.example.json
python scripts/build_catalog.py wds --cache /var/lib/robofast/catalog
```

Copying `bright_stars.npy` into the catalog's `cache_dir` works too
(`catalog_at` looks there first).

**Attribution.** Anything that uses these data must carry ESA's
acknowledgement:

> This work has made use of data from the European Space Agency (ESA)
> mission Gaia (<https://www.cosmos.esa.int/gaia>), processed by the Gaia Data
> Processing and Analysis Consortium (DPAC,
> <https://www.cosmos.esa.int/web/gaia/dpac/consortium>). Funding for the DPAC
> has been provided by national institutions, in particular the institutions
> participating in the Gaia Multilateral Agreement.

and cite Gaia Collaboration, Prusti et al. 2016, A&A 595, A1 and Gaia
Collaboration, Vallenari et al. 2023, A&A 674, A1. A commercial user should
check ESA's terms before using the Gaia data at all, or choose another
source ("Catalog sources", "Licences" above).

**Publishing a new version** of the Zenodo record (a rebuilt bright-star
list, new test fields): `python scripts/build_catalog.py bundle --out DIR`
rebuilds every file from the Gaia archive, byte for byte reproducibly, and
`scripts/zenodo_publish.py` uploads it as a draft; docs/zenodo.md has the
whole workflow.

Earlier commits of the repository did contain `robofast/data/bright_stars.npy`
and real Gaia fixture tiles under `tests/core/data/catalog/` (under CC BY-NC
3.0 IGO, as their README said); they were removed from the tip, not from
the history.

### Tests

The catalog tests run on one test sky (`tests/core/skydata.py`), five
fields -- a sparse high-latitude field (212.72, +62.52), HD 185144 (sigma
Dra), a dense field near the Galactic plane (285.79, +25.29), nu Draconis
(an equal pair 62 arcsec apart) and tau Ceti -- over 30 HEALPix tiles:

* **real Gaia DR3** (`test_fixture_tiles.npz` and `bright_stars.npy` from
  the Zenodo record) once the record is pinned: fetched once into the data
  directory (CI caches it, keyed on `zenodo_assets.py`) and unpacked there;
* **synthetic** until then, or with `ROBOFAST_TEST_SKY=synthetic`:
  `tests/core/data/catalog/`, a seeded random catalog with Gaia-like star
  counts, magnitudes and proper motions per field, plus hand-placed stars
  where the tests need them (nu1/nu2 Dra in their WDS geometry, sigma Dra,
  tau Ceti, a bright pair in the sparse field). No Gaia data;
  `python scripts/build_catalog.py synthetic` regenerates it
  (`robofast/testing/sky_fixture.py`).

Both carry the real WDS lines (`wds.npy`). The two tests that only mean
something on real data (the HEALPix check against Gaia's own `source_id`s,
and the bright-star list's size) skip on the synthetic sky; every other
catalog, vetting, simulator and plate-solving test runs on either. CI never
queries the Gaia archive.

## 2. Pointing-error models (`robofast/pointing.py`)

`PointingModel(terms, mount, latitude)` holds TPOINT terms (arcsec):
equatorial IH, ID, CH, NP, MA, ME, TF, FO, DAF, HCES/HCEC, DCES/DCEC; alt-az
IA, IE, CA, NPAE, AN, AW, TF, TX, ACES/ACEC, ECES/ECEC (formulae in the
module doc). Convention: the model gives **mount minus true**.
`mount_from_true` is what to command to land on a star; `true_from_mount`
(fixed-point inverse, < 1 mas) is where a commanded telescope really points.
Every term is linear in its coefficient: `fit(names, true..., mount...)`
fits them by least squares (residuals weighted by cos dec) with formal
errors and the rms before and after -- what a pointing-model run does with
plate-solved images.

## 3. The simulator (`robofast/testing/skysim.py`)

`SkyScene(catalog, camera, photometry, site, pointing, seeing, ...)`:

1. the mount is commanded to (RA, Dec); the scene's *true* pointing model
   (hour angle from the local sidereal time; azimuth/elevation for alt-az)
   says where it really points;
2. catalog stars around the true pointing, moved to the date;
3. projected (TAN) with the camera's plate scale, rotator position angle,
   parity, optical axis (`crpix`) and optional radial distortion `k1`;
4. fluxes from G through the port's zero point and extinction at the
   airmass; the sky from the Sun, Moon and dark sky (Krisciunas & Schaefer
   1991; a twilight ramp); seeing, defocus, Poisson and read noise, bias,
   gain, saturation, subframes and binning by the shared renderer
   (`robofast/testing/starfield.py`).

`frame.truth` carries the commanded and true pointing, the PA, and every
catalog star with its exact pixel. Commanded coordinates are taken as ICRS
(no precession, nutation, aberration or refraction: the mount software's
business).

Opt-in users (the default random star fields are unchanged):
`SkyFocusScene(sky, mount, clock)` is a `FocusScene` for `StarfieldCamera`
whose stars follow a mount's `RightAscension`/`Declination` (the simulated
night's imagers); `sky_world_stars(sky, ra, dec, when)` gives the guider
simulator's `SimWorld` its target and field stars.

## 4. Photometry (`robofast/photometry.py`)

`PortPhotometry` per telescope and port: `zp` is the catalog magnitude
giving 1 e-/s through that port, measured (`zero_point`, with its error and
date) or estimated from an efficiency chain (`efficiency`: named factors
such as `mirrors`, `qe`, `filter`, `beamsplitter`) times the collecting area
and the band's photon rate. The FAU sits behind a 2 % beamsplitter, so its
zero point is the imager-like optics' minus 4.25 mag; the fiber gets the
other 98 % (`with_fraction("beamsplitter", 0.98)`, for exposure-meter and
spectrum S/N estimates). Also: the CCD-equation `snr`, and
`sky_brightness`/`sky_conditions` for the sky.

## 5. Plate solving (`robofast/astrometry.py`)

**Near-blind** (`solve_near`, the default for acquisition, pointing models
and every frame): given the approximate pointing (`search_radius`, default
0.25 deg) and the plate scale (`scale_tolerance`, default 3 %):

1. detect stars (`detect_stars`: block-median background, 3x3 smoothing,
   8-connected pieces of at least 4 pixels, flux-weighted centroids);
2. take the catalog around the guess, to about the depth of the detections,
   onto the tangent plane in pixel units;
3. form triangles of each star with pairs of its nearest neighbours, in
   both lists; since the scale is known, the three side lengths themselves
   are the invariants (rotation and parity are free unless `pa`/`flip` are
   given);
4. each matched triangle gives a candidate similarity transform; keep the
   one most stars agree with;
5. match every detection through it, fit a TAN WCS (SIP with `sip_order`
   2-3 for a distorted field) by linear least squares, re-match tighter,
   refit;
6. accept only if at least `min_matches` stars (default 4) and a quarter
   of the stars that should match did (else `SolveError`: "a chance
   alignment").

Few stars: a pair is accepted only with `min_matches=2` and a known parity
(the brighter detection is the brighter catalog star); a single star only
with `min_matches=1` and a known PA (an offset); both are flagged
`degenerate`.

Accuracy and speed, measured by the tests (render with a pointing error of
1-3 arcmin, solve from the commanded position): MINERVA imager (0.61"/px,
2048x2048) centre within 0.1-0.3 arcsec, PA within 0.01 deg, scale within
0.05 %, ~100-200 stars matched, rms ~0.2-0.3 arcsec; FAU (0.265"/px,
1936x1216) dense to sparse fields within 0.1-0.3 arcsec with 3-60 stars.
About 1 s per 2048x2048 frame (detection dominates) on a loaded server.

**Blind** (`solve_field`): astrometry.net's `solve-field` if installed,
with its index files in a local directory (`index_dir`). For MINERVA's
fields: the imager (21 arcmin) needs the 4100/5200-series indexes with
quads of 2-30 arcmin (4107-4110 or 5204-5206), the FAU (8.5 x 5.4 arcmin)
5203-5205 (`http://data.astrometry.net/`). Not installed in CI; the test
skips.

`Solution`: the astropy WCS (1-based in FITS, as FITS requires), `ra`/`dec`
of the image centre, `pa` (+y east of north), `scale`, `flip`, `rms`,
`n_matched`, the matched stars, `method`, `seconds`, `degenerate`.

## 6. Solved frames and zero points (`robofast/framesolve.py`)

With `images.solve` in the enclosure config,

```json
"solve": {"catalog": {"cache_dir": "/var/lib/robofast/catalog"},
          "telescopes": {"t1": {"search_radius": 0.2}}}
```

the frame writer solves every light frame near its mount WCS (in the
writer thread, bounded by `timeout`). On success the approximate WCS is
replaced: `WCSSRC = 'robofast'`, `CTYPE` `RA---TAN` (or `-SIP`), the CD/PC
cards, `WCSRMS`, `WCSNSTAR`, `WCSPA`, `WCSSCALE`, `WCSFLIP`, `WCSMETH`. The
frame's photometric zero point from the matched stars (3-arcsec-radius
apertures, isolated and unsaturated stars) goes in as `ZPMAG` (G giving 1
e-/s, or 1 ADU/s without `EGAIN`), `ZPERR`, `ZPSCAT`, `ZPNSTAR`, `ZPUNIT`
(next to `AIRMASS`); the nightly index gets a `solve` entry (with the
mount's pointing offset), and the registry `{tel}.image.zp`,
`{tel}.image.wcs_rms`, `wcs_offset_arcsec`, `wcs_pa`. A failure keeps the
mount WCS (`WCSSRC = 'mount'`), adds `WCSFAIL`, and never costs the frame.
The index's `solve` entry also carries the frame's conditions
(`context`: temperatures, Moon, cloud sensor, the raw sky level) for the
throughput monitor, which follows each telescope's zero points over months
(mirror cleanliness, dew, a cover left on): docs/throughput.md.

## 7. Target vetting (`robofast/vetting.py`)

Before a target is ever observed, `vet(catalog, name, ra, dec, port)` looks
at its field through one port (the FAU with the target on the fiber, or the
imager) and flags: `twin`/`companion`/`blend` (catalog stars within
`companion_radius` and `companion_dmag`), `wds_companion` (a WDS pair at the
target: Gaia misses the closest and brightest pairs), `not_brightest` (a
star within `brightest_margin` of the target's brightness in the
acquisition search box), `saturates`, `too_faint` (S/N at the port's
exposure, from its photometry), `few_stars` (too few to plate solve),
`high_proper_motion`, `fiber_contamination` (another star inside the
guider's capture radius), `not_in_catalog`, `catalog_unavailable`.
`VettingResult.unambiguous` says whether "take the brightest star" would
take the target.

Wiring: the dispatch plan's `vetting` entry (inline, or a file:
`sites/minerva/sky.example.json`) makes the scheduler vet every group at
intake (`submit`, ToOs, the drop box): flags are logged and written to the
plan record (`"vetting"`), and a flag listed in `reject` refuses the group
(the drop box files the reason under `rejected/`). The request API's
`POST /requestgroups/validate` returns `vetting` and `warnings`, and a
rejecting flag makes the group invalid. A spectrum's observation carries
`spectrum["acquisition"] = {"unambiguous", "flags"}` to the guider.

## 8. Acquisition that a twin cannot fool

The guider's `select: "catalog"` (`robofast/guider/identify.py`)
plate-solves each acquisition frame near the target and takes the detected
star within `identify_tolerance` pixels of the target's catalog position;
the sequencer sends the target (`target_radec`) and vetting's
`unambiguous` with `POST /start`, and the guider service needs `catalog`
and `solver` in its config. Without a solution (clouds, too few stars) it
falls back to the brightest star only if vetting found the field
unambiguous; otherwise the acquisition fails with "target not identified".
`tests/core/test_vetting.py` runs it on nu Draconis (G 4.79 and 4.84, 62
arcsec apart, both in the FAU): the brightest-star acquisition puts nu1 on
the fiber when nu2 was asked for, the catalog acquisition puts nu2 there.

Acquisition in normal FAU operation does not rely on plate solving (only
the target, perhaps a companion, shows in a short frame through the 2 %
beamsplitter): the guider's acquisition cascade (`docs/acquisition.md`)
uses the star count, the port's PA calibration and the catalog pattern.

## 9. Measured on MINERVA

From the legacy T2 FAU autofocus frames of n20260423 (read-only; the frames
are not in the repository), solved with robofast's solver (the header
RA/Dec are JNow, precessed to ICRS first): plate scale 0.2655 arcsec/px,
the image **mirrored** (east right with north up), PA 99-106 deg on that
night, solution rms 0.17-0.32 arcsec with 5-13 Gaia stars; zero point **G =
19.70 +- 0.10 for 1 ADU/s** (stored 16-bit ADU; 5 frames). Details and the
efficiency-chain comparison: `sites/minerva/README.md` (photometry).
Detection rejects the ASI174's many hot pixels by their sharpness.

## 10. What is left

- Measured plate scales, PAs and parities for each MINERVA port (T2's FAU:
  section 9), saved with `python -m robofast.rotcal solve`
  (`docs/rotcal.md`); the imagers' and T1's FAU zero points; the ASI174's
  e-/ADU.
- ~~A pointing-model builder~~: `robofast/pointmodel.py`,
  `docs/pointing_model.md`.
- astrometry.net index files on the control hosts, if a blind fallback is
  wanted.
- ~~A throughput monitor~~: `robofast/throughput.py`,
  `docs/throughput.md` (its extinction coefficients and baselines still
  need real data).
- The Gaia cache filled for the target list on each host; `select:
  "catalog"` turned on in the guider configs once the FAU parity and the
  solver are confirmed on the sky.
