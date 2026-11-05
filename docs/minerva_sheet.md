# MINERVA's target lists: the Google Sheet

MINERVA's RV targets and its B stars live in a Google Sheet, the "MINERVA
target list": tab `targets` (the RV stars) and tab `B stars` (rapidly
rotating B stars observed through the iodine cell, docs/kiwispec.md
section 4). Legacy also had a red-side list in a sheet of its own; it is
retired (see "Not done"). Legacy
`minerva-control` (`minerva_library/targetlist.py`) exported each tab to
CSV with a Google service account (`downloadList`) and read the CSV
(`rdlist`, `mkdict`) for its scheduler (`scheduler.py`) and its nightly
schedule (`rv_control.mkschedule`).

`robofast/sites/minerva/target_sheet.py` reads the same tabs with the
same service account and a **read-only** scope, keeps a timestamped
snapshot of every fetch, and turns each active row into one request group
in the request language (`docs/request_language.md`), ready for the
dispatch scheduler. It follows the TRES importer's stance
(`docs/catalogs.md`): what legacy reads is read the way legacy meant it; a
row that legacy would misread, or that robofast cannot honour, is
**refused** with its sheet row number and the reason; nothing is guessed.

```
python -m robofast.sites.minerva.target_sheet fetch  --config target_sheet.json   # live -> snapshot
python -m robofast.sites.minerva.target_sheet check  --config target_sheet.json   # validate, list refusals
python -m robofast.sites.minerva.target_sheet export --config target_sheet.json -o minerva-requests.json
python -m robofast.sites.minerva.target_sheet check  --instrument spectrograph --csv targets=targets.csv --csv bstars=bstars.csv
python -m robofast.sites.minerva.target_sheet export --config target_sheet.json --snapshot /var/lib/robofast/data/minerva_sheet/20261101T120000Z
```

`check` and `export` take one source: `--config` (fetch live; if the Sheet
cannot be read, fall back to the newest snapshot; `--offline` skips the
fetch), `--snapshot DIR` (that snapshot), or `--csv LIST=PATH` (a CSV
export of a tab, "File > Download > CSV" in the Sheet, or legacy's
`targets.csv` / `bstar.csv`); without `--config`, `--instrument` names
the spectrograph's `instrument_type`. `check` exits 2 if any row is refused.
`export` writes the accepted rows (`{"groups": [...]}`, validated by the
request model) and lists the refused ones on stderr; `--strict` makes any
refusal an error. `fetch` only fetches; it never falls back. Errors exit 2.

## Configuration

`sites/minerva/target_sheet.example.json`, copied to the control host and
filled in there:

| Key | Meaning |
|---|---|
| `key_file` | the service-account key (JSON), e.g. `/etc/robofast/secrets/minerva_sheets.json` (see "The key file" below). `$ROBOFAST_MINERVA_SHEETS_KEY` overrides it; with neither, `$GOOGLE_APPLICATION_CREDENTIALS` is used |
| `lists.<name>.sheet` | the spreadsheet key (the id in the Sheet's URL). **Site config only**, never in a repository; the example has placeholders, and `fetch` refuses them |
| `lists.<name>.tab` | the worksheet: `targets`, `B stars` |
| `lists.<name>.kind` | `targets` (RV stars) or `bstars` |
| `lists.<name>.instrument` | the spectrograph's `instrument_type` in the site's dispatch config (`spectrograph` for MINERVA). Required: a site value, no default (design.md 3.10) |
| `lists.<name>.proposal`, `id_prefix` | the requests' `proposal` and id prefix (`RV-`, `BSTAR-`) |
| `lists.<name>.pool`, `per_night` | B stars: the cadence pool and how many of it a night (default the list's name, 1) |
| `lists.<name>.limit` | the cadence's nightly limit: `hard` (default: refused once reached) or `soft` (still taken, but only when nothing else is observable). MINERVA's B stars: `soft` |
| `lists.<name>.enabled` | `false` skips the list |
| `offset_check` | how acquisition offsets are checked against the star catalog (below, "Acquisition offsets"): `tolerance` (arcsec, 3), `g_bright`, `g_faint` (catalog magnitude, 7 and 14.5), `search` (arcsec, 60), `action` (`refuse`, the default, or `flag`), `catalog` (a catalog entry, docs/sky.md; default the default catalog). On by default; `false` turns it off |
| `snapshot_dir`, `keep` | where snapshots go (default `$ROBOFAST_DATA_DIR/minerva_sheet`, see `robofast.zenodo.data_dir`) and how many are kept (30) |

The live fetch needs `gspread` and `google-auth`: `pip install --group
sheets` (from the repository) or `pip install ".[sheets]"`. They are optional imports; nothing else in robofast needs them.
Cells are read unformatted (the stored numbers, not the Sheet's display
rounding of them), so coordinates keep their full precision.

### The key file

The service-account key is a credential: anyone holding it reads the
collaboration's Sheet. Keep it out of every repository, in
`/etc/robofast/secrets/`, mode `0600`, owned by the user that runs the
reader:

```
sudo install -m 600 -o <service user> minerva_sheets.json /etc/robofast/secrets/
```

Under systemd, the deployment's way (`docs/deploy.md`, "Secrets") is
better: the file stays `root:root 0600` in `/etc/robofast/secrets/`, the
unit has `LoadCredential=minerva_sheets.json:/etc/robofast/secrets/minerva_sheets.json`,
and the config says `"key_file": "$CREDENTIALS_DIRECTORY/minerva_sheets.json"`
(environment variables in the path are expanded).

Before a live fetch the reader checks the file's metadata (never its
contents) and **refuses** a key that group or others can read or write, or
that lies inside a git work tree (a parent directory holds `.git`), with
an error naming the fix. A refused key is a configuration error, not a
reason to fall back to a snapshot. A missing key is: the lists then come
from the newest snapshot. `.gitignore` also ignores `*service-account*.json`
and `secrets/` as a backstop.

## Snapshots and the fallback

Every live fetch writes `<snapshot_dir>/<UTC time>/` (for example
`20261101T120000Z/`) holding `targets.csv`, `bstars.csv` (one CSV per list,
as fetched) and `meta.json` (fetch time; each list's tab, kind, row count
and SHA-256). It is written to a temporary directory and renamed into
place, so a snapshot is never half written; a CSV that no longer matches
its digest is refused. The newest `keep` are kept. Snapshots hold the
collaboration's targets: the directory is created mode 700.

When the Sheet cannot be read (network, Google, a missing or revoked key,
gspread not installed), the lists come from the newest snapshot, with a
warning naming the reason and the snapshot's age. Either way one line
`MINERVA target lists from <snapshot path>` is logged, so every load
names exactly where its targets came from. With neither a Sheet nor a
snapshot it is an error. `ROBOFAST_OFFLINE=1` skips the fetch.

## The columns

Headers are matched as legacy matched them: the text before any `(`,
stripped (`ra (J2000 hrs)` is `ra`; `seplimit` and `seplimit (sec)` are
the same). A tab without one of the columns legacy's `mkdict` reads is
refused as a whole (legacy stopped with a `KeyError`), as is a column read
here that appears twice. A column not listed below is ignored with a
warning.

Only rows with `active` 1 are converted; the others are counted as
inactive and not checked (legacy skipped them). Blank rows are skipped.

| Column (tab) | Legacy reading | robofast |
|---|---|---|
| `name` | the target's name | the target's `name`; the group id is `<id_prefix>-<name>` (characters outside `A-Za-z0-9._+-` become `_`), stable across fetches so the dispatch state's visit history carries over. Blank, or a name or id already on an earlier active row: **refused** (legacy's `mkdict(name=)` took the first) |
| `ra (J2000 hrs)` | RA in hours, J2000, at epoch 2000.0 (`cdk700.starmotion`) | `target.ra` = 15 x ra, degrees. Required, 0 <= ra < 24 |
| `radeg (J2000 deg)` | not read | if given, must agree with 15 x `ra` within 1 arcsec, or the row is **refused** (the sheet disagrees with itself; legacy used `ra`) |
| `dec (J2000 deg)` | Dec in degrees, J2000 | `target.dec`. Required, within +-90 |
| `pmra (mas/yr)`, `pmdec (mas/yr)` | mu_alpha cos(dec) and mu_delta, applied from J2000.0 to now (`starmotion`) | `proper_motion_ra`/`_dec` with `epoch` 2000.0. Blank: **refused** (legacy read NaN and pointed at NaN); write 0 when negligible |
| `parallax (mas)` | FITS header only (`starmotion` looks for a `px` key that `mkdict` never sets, so parallax was never applied) | `target.parallax` (barycentric correction); blank: absent |
| `rv (km/s)` | FITS header; in `starmotion` only together with the (missing) parallax | `target.extra_params.radial_velocity`; blank: absent |
| `vmag (Johnson V)` | FITS header | in the request's `observation_note` |
| `priority` | multiplies the scheduler's weight (1-10 in practice) | the group's `ipp_value`. Must be > 0: a priority of 0 or less on an active row is **refused** (set `active` 0 instead) |
| `exptime (sec)` | the spectrum's exposure time, one exposure | `exposure_time`, `exposure_count` 1. Must be > 0 |
| `maxobs` (`targets`) | visits a night; blank or unreadable: 2. (The scheduler's test `observed > maxobs` let one more through) | `extra_params.cadence.max_per_night`, as written; blank: 2. Not a whole number >= 1: **refused** (legacy silently used 2) |
| `seplimit (sec)` | minimum time between visits; blank: 3 h | `cadence.min_interval` (start to start); blank: 10800 s |
| `fauexptime (sec)` | the acquisition camera's exposure time | `acquisition_config.exposure_time` (accepted by the request model; the guider sets its own exposure today). Blank: absent |
| `acquisition_offset_north`, `_east (arcsec)` | offset guiding: the target is this far from the star the guider locks on (target minus guide star; applied only when **both** are non-zero, and its star selection was broken: docs/offset_guiding.md, "Legacy") | offset guiding (docs/offset_guiding.md): `guiding_config.extra_params` `guide: "offset"`, `offset_star: {"east": -east, "north": -north}`, when either is non-zero, **after a catalog star is found there** ("Acquisition offsets" above; else refused). Blank or 0: none |
| `iodine` (`targets`; the red list) | **not read**: `mkdict` sets `i2` true for every target | 1 or blank: iodine in (as legacy). 0: **refused**, since legacy observed such a row through the cell anyway and robofast will not guess which was meant (set 1, or ask for a template, a `SPECTRUM` with iodine false, separately) |
| `active` | 1: observe; anything else: skip | 1: convert; 0 or blank: inactive; anything else (`yes`, `TRUE`, 2): **refused** |
| `comment` | FITS header | in `observation_note` |
| `maxobs` (`B stars`) | as for targets, but the scheduler's "one B star a night" flag dominated | not used: the B stars share a pool (below) |
| `distance`, `Teff`, `sptype`, `bad_priority`, `vsini`, `pmodeperiod`, `Amp P-mode`, `dtactive`, `tc`, `period`, `t14`, `Howard (2010)`, `finder`, `mstar`, `rstar`, `rpearth`, `mpearth`, `k` | not read | ignored (kept in the snapshot) |
| red list: `Common Name`, `Sloan I band`, `MEarth Image`, `MEarth Mags`, `Companion`, `Type and Seperation`, `Planets`, `Mass`, `Period`, `Paper` | not read | ignored |

A row that passes these checks is also validated by the request model
(`RequestGroup.from_dict`): whatever that refuses refuses the row, with
its reason.

### Acquisition offsets

Legacy trusted `acquisition_offset_north`/`_east` as typed. robofast
checks each one against the star catalog (`docs/sky.md`) before the row
becomes a request (`verify_offsets`; the check itself is
`robofast.guider.offset.check_offset_star`, usable for any explicit
offset). The guide star the offset implies is the target, moved along its
proper motion from J2000 to the date of the import, plus the offset; the
catalog's stars are moved to the same date. The row passes when a catalog
star lies within `offset_check.tolerance` (3") of that position and is a
sensible guide star: fainter than `g_bright` (7, which saturates the FAU)
and brighter than `g_faint` (14.5), the limits `offset_star: "auto"`
uses. A verified offset gets the star's catalog magnitude
(`offset_star.g`), which the guiding recommendation (`guide: "auto"`)
then uses instead of its assumed one.

A row that fails is **refused** (`action: "refuse"`), with what was found:

```
refused: targets row 8 (SYN-ZETA): acquisition offset: no catalog star within 3" of
target + offset; the nearest (mag 11.0) is 12.0" away, at east +12.0" north -5.0" from the target
```

(the nearest star within `search`, 60", and its own offset from the
target, which is perhaps what the sheet meant). With `action: "flag"`
the row is kept and the failure is a warning. When the catalog cannot
answer (its tiles are not cached and it may not fetch them: `--offline`,
`$ROBOFAST_OFFLINE`, or `fetch: false`), the row is kept with a "not
checked" warning: robofast refuses only on evidence. Each list's summary
counts the offsets verified, failed and not checked:

```
targets: 41 requests, 3 inactive, 2 refused; acquisition offsets: 6 verified, 1 failed, 0 not checked
```

On the command line, `check` and `export` run the same check (the
config's `offset_check`, or the defaults with `--csv`/`--snapshot`);
`--catalog DIR` names the catalog's cache directory, `--no-offset-check`
skips it.

## The requests

Every accepted row becomes one group with one standing request
(`extra_params.cadence`) and one `SPECTRUM` configuration: guided, iodine
in (`extra_params.iodine: true`), fed by every free telescope with the
configured instrument (no `location.telescope`, as legacy's `doSpectra` used the
telescopes it had), no constraints of its own beyond the telescope's and
site's limits (legacy had none).

- **RV targets** (`kind: targets`): cadence `min_interval` = `seplimit`,
  `max_per_night` = `maxobs`.
- **B stars** (`kind: bstars`): cadence `min_interval` = `seplimit`,
  `max_per_night` = `per_night` (1), `pool: "bstars"` and, in MINERVA's
  config, `limit: "soft"`. A pool shares the nightly count among its
  requests (`docs/request_language.md`): the first B star of the night
  competes on its `ipp_value` like any request; once one is taken, the
  others stay observable but are *demoted* below every other request, so
  a second B star is taken only when nothing else can be observed (better
  than an idle telescope). This is legacy's rule: after the night's B star
  `scheduler.calculate_weights` divided the B stars' weights by 10^6
  ("unless we have nothing better to do"). `limit: "hard"` refuses further
  B stars outright; raise `per_night` for more. Legacy also raised the B
  stars' weight as the night ran out (x 0.784 exp(8726 s / time left)) and
  `mkschedule` put one at the start of the night; robofast does not.

The output loads in the dispatch plan as a `requests` file
(`sites/minerva/dispatch.example.json`); run `export` from a timer and
point the plan at its output. The request API reloads a request file when
it changes.

## Not done

- The red-side list is **retired** (JDE, 2026-11): no longer maintained,
  so it is not in the example config. It might return if the Starlight
  Xpress AO drastically improves throughput; to revive it, add a list
  (`kind: targets`, its own `sheet`, an `instrument` for the red side) to
  the site config. The reader is generic and still knows its columns. Its
  `iodine` column is probably the red spectrograph's own setting, not
  KiwiSpec's cell: rows with `iodine` 0 would be refused until that is
  settled.
- Legacy's B-star boost through the night, and its weighting of three
  visits a night by hour angle (`calc_weight_multi`): the dispatch score
  has its own terms (design.md 3.7).
- Writing to the Sheet: never; the scope is read only.
