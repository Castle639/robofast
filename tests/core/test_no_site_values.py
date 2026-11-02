"""Core names no site, telescope or instrument (docs/design.md 3.10).

Every site value lives under ``sites/<site>/``; core code reads it from
config and has no site's name or number in it. This test parses
``robofast_core/**/*.py`` (the AST, so docstrings and comments are free to
mention sites) and fails on a string literal or an identifier that names a
site or a site-specific instrument, or on a site's coordinates or elevation
as a number.

Three directories are exempt:

* ``robofast_core/sites/`` (site code: adapters, legacy importers, each
  site's plug-in) and ``robofast_core/testing/`` (simulations and fixtures);
* ``robofast_core/devices/``: drivers for specific hardware (the Aqawan
  PAC, the KiwiSpec PC, the TCSGalil, the TRES front end, ...). A driver
  may name its own hardware; the core modules that use it (sequencer, plan,
  dispatch, request_*, console, safety, imaging, photometry, vetting, sun,
  weather_*, guider, ...) may not.

A module **named** after a site or a site-specific instrument belongs in
one of the first and last of those: one anywhere else fails
(:func:`test_no_site_named_module_in_core`), so an importer named for its
site cannot sit in core again.

:data:`ALLOWLIST` lists what core may still name, each entry tagged with
the plan step that removes it (docs/design.md 3.10); after step 2 it is
empty. An entry that no longer matches anything fails the test too, so the
list only shrinks.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "robofast_core"

#: Site and site-specific instrument names (lowercase). Matched at the start
#: of a word (so "metres" does not match "tres", "KiwiSpecBusy" matches).
NAMES = ("minerva", "kiwispec", "flwo", "tillinghast", "tres", "mearth", "tierras", "aqawan")
_NAME_RE = re.compile(r"(?<![a-z])(" + "|".join(NAMES) + ")")

#: Site numbers: (value, tolerance). Latitudes and longitudes (deg) and
#: elevations (m) of the sites in sites/.
SITE_NUMBERS = (
    (31.68, 0.01),  # Mt. Hopkins latitude
    (-110.88, 0.01),  # Mt. Hopkins longitude
    (-30.17, 0.01),  # CTIO latitude
    (-70.80, 0.01),  # CTIO longitude
    (2316.0, 0.5),  # MINERVA elevation
    (2344.0, 0.5),  # FLWO 60-inch elevation
    (2124.0, 0.5),  # MEarth South elevation
)

#: Instrument couplings without a site name in them, watched the same way:
#: name -> (pattern, modules it applies to or None for all). The KiwiSpec
#: iodine cell in the request schema and sequencer; the literal instrument
#: name "spectrograph" that request intake and dispatch key behaviour on;
#: the ADC server's switch numbers read and written by core.
COUPLINGS: dict[str, tuple[re.Pattern[str], tuple[str, ...] | None]] = {
    "iodine": (re.compile(r"iodine"), None),
    '"spectrograph"': (
        re.compile(r"^spectrograph$"),
        ("request_model.py", "request_api.py"),
    ),
    "ADC switch id": (re.compile(r"^ADC_(SWITCHES|TRACKING|SETTLED)$"), None),
}

#: Directories exempt as a whole (relative to robofast_core/).
EXEMPT_DIRS = ("sites/", "testing/", "devices/")

#: What core still names: (module, name) -> (count, why). Every entry is
#: removed by a later step of the site-configuration plan (docs/design.md
#: 3.10) and is tagged with it; delete the entry with the code it covers.
ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {}

#: Where a module may be named after a site (relative to robofast_core/).
SITE_NAMED_DIRS = ("sites/", "devices/")


def _modules() -> list[Path]:
    out = []
    for p in sorted(CORE.rglob("*.py")):
        rel = p.relative_to(CORE).as_posix()
        if rel.startswith(EXEMPT_DIRS):
            continue
        out.append(p)
    return out


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    # A bare string statement anywhere (an attribute docstring) is a comment.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            ids.add(id(node.value))
    return ids


def _identifiers(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.keyword) and node.arg:
        return [node.arg]
    if isinstance(node, ast.alias):
        return [node.name, node.asname or ""]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def _number(node: ast.AST) -> float | None:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _number(node.operand)
        return None if v is None else -v
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    return None


def scan(path: Path) -> list[tuple[str, int, str]]:
    """``(what, line, text)`` for each site name or number in ``path``."""
    tree = ast.parse(path.read_text(), str(path))
    skip = _docstrings(tree)
    hits = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        texts = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            texts.append(node.value)
        texts += _identifiers(node)
        for text in texts:
            for m in _NAME_RE.finditer(text.lower()):
                hits.append((m.group(1), line, text))
            for name, (pattern, only) in COUPLINGS.items():
                if (only is None or path.name in only) and pattern.search(text):
                    if name == '"spectrograph"' and not isinstance(node, ast.Constant):
                        continue  # the literal, not identifiers
                    hits.append((name, line, text))
        # A negative number is seen twice (the UnaryOp, then its positive
        # operand); only the signed value can match a negative site number.
        v = _number(node)
        if v is not None:
            for value, tol in SITE_NUMBERS:
                if abs(v - value) <= tol:
                    hits.append((f"{value:g}", line, repr(v)))
    return hits


def test_core_names_no_site():
    found: Counter[tuple[str, str]] = Counter()
    where: dict[tuple[str, str], list[str]] = {}
    for path in _modules():
        rel = path.relative_to(CORE).as_posix()
        for what, line, text in scan(path):
            key = (rel, what)
            found[key] += 1
            where.setdefault(key, []).append(f"robofast_core/{rel}:{line}: {text!r}")
    errors = []
    for key, n in sorted(found.items()):
        allowed = ALLOWLIST.get(key, (0, ""))[0]
        if n > allowed:
            errors.append(
                f"{key[0]} names {key[1]!r} {n} times (allowlist: {allowed}); move the value "
                f"into sites/<site>/ config (docs/design.md 3.10):\n    "
                + "\n    ".join(where[key])
            )
    for key, (allowed, why) in sorted(ALLOWLIST.items()):
        if found.get(key, 0) < allowed:
            errors.append(
                f"allowlist entry {key} ({why}) allows {allowed} but only "
                f"{found.get(key, 0)} remain: lower or delete it"
            )
    assert not errors, "\n".join(errors)


def test_no_site_named_module_in_core():
    """A module, or a package, named after a site lives in sites/ or devices/."""
    named = []
    for p in sorted(CORE.rglob("*.py")):
        rel = p.relative_to(CORE)
        if rel.as_posix().startswith(SITE_NAMED_DIRS):
            continue
        parts = [*rel.parts[:-1], p.stem]
        if any(_NAME_RE.search(part.lower()) for part in parts):
            named.append(f"robofast_core/{rel.as_posix()}")
    assert not named, (
        f"{named}: named after a site or a site's instrument; site code goes under "
        "robofast_core/sites/<site>/ (a plug-in), a hardware driver under "
        "robofast_core/devices/ (docs/design.md 3.10)"
    )


def test_the_scanner_sees_names_and_numbers_but_not_docstrings(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        '"""Docstring: MINERVA, the FLWO 60-inch, 31.68."""\n'
        "# comment: tres\n"
        "import robofast_core.devices.kiwispec_client as kc\n"
        "from robofast_core.devices.tres_frontend_server import TRESFrontEndConfig\n"
        "def f(metres, stress):\n"
        '    """f: Tillinghast."""\n'
        "    x = 'sites/minerva/aqawan1.json'\n"
        "    lat, lon, elev = 31.6809, -110.8781, 2344\n"
        "    return KiwiSpecBusy, x.mearth_south, f'{x} on MEarth'\n"
    )
    names = sorted({w for w, _, _ in scan(p)})
    assert names == sorted(
        {"kiwispec", "tres", "minerva", "aqawan", "mearth", "31.68", "-110.88", "2344"}
    ), names
