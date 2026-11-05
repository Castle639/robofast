"""Registries and plug-ins: site and instrument code without editing core.

What a site needs that is new code -- a plan type, an observation kind, an
instrument handler, a weather source -- is an entry in a :class:`Registry`
keyed by name (docs/design.md 3.10, docs/plugins.md). Core registers its
own entries the same way a plug-in does, with ``REGISTRY.register(name,
entry)``; a config names an entry by its key (``"type": "dispatch"``,
``"type": "kiwispec"``, ...), and a name nobody registered is an error that
lists the names that are.

A plug-in is a module with a ``register()`` function (or any zero-argument
callable) that makes those calls. robofast runs it once per process, from
either of two places:

* the Python entry-point group ``robofast.plugins`` (:data:`ENTRY_POINT_GROUP`):
  an installed package declares, in its own ``pyproject.toml``::

      [project.entry-points."robofast.plugins"]
      mysite = "mysite.robofast:register"

  and every such entry is loaded the first time any registry is used;
* a config's ``plugins`` list of module names (or ``module:function``),
  for code that is not installed as a package -- e.g. a site adapter
  shipped under ``robofast/sites/`` (``plugins:
  ["robofast.sites.minerva"]`` in ``sites/minerva/weather.yaml``).
  The services load it when they read the config (:func:`load_config_plugins`).

Two entries with the same name in one registry are refused (the error names
both origins), so a plug-in can never silently replace core's or another
plug-in's entry.

:data:`BUILTIN_PLUGINS` are core's own plug-ins, loaded before the entry
points: the hardware drivers' handlers (``robofast.devices``).
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from importlib.metadata import entry_points
from typing import Any

__all__ = [
    "BUILTIN_PLUGINS",
    "ENTRY_POINT_GROUP",
    "Registry",
    "load_config_plugins",
    "load_plugins",
]

log = logging.getLogger(__name__)

#: The entry-point group an installed package registers its plug-ins in.
ENTRY_POINT_GROUP = "robofast.plugins"
#: Core's own plug-ins (module names), loaded before the entry points.
BUILTIN_PLUGINS: tuple[str, ...] = ("robofast.devices",)

_lock = threading.RLock()
#: Plug-ins already run (module or ``module:function`` names, entry points
#: as ``entry-point:<name>``), so each runs once per process.
_loaded: set[str] = set()
_started = False
#: The plug-in being run now: the origin recorded for what it registers.
_origin: str | None = None


class Registry[T]:
    """Entries of one kind (``what``: "plan type", "observation kind", ...)
    by name. Reading it (``in``, ``[]``, iteration) first loads the built-in
    plug-ins and the entry points (:func:`load_plugins`), once per process."""

    def __init__(self, what: str) -> None:
        self.what = what
        self._items: dict[str, T] = {}
        self._origins: dict[str, str] = {}

    def register(self, name: str, entry: T) -> T:
        """Add ``entry`` as ``name``; ValueError if the name is taken.
        Returns ``entry`` (so it can be used as a decorator's tail)."""
        if not isinstance(name, str) or not name:
            raise ValueError(f"{self.what}: a name is a non-empty string, not {name!r}")
        origin = _origin or _module_of(entry)
        with _lock:
            if name in self._items:
                raise ValueError(
                    f"{self.what} {name!r} is registered twice: by {self._origins[name]}"
                    f" and by {origin}"
                )
            self._items[name] = entry
            self._origins[name] = origin
        return entry

    def unregister(self, name: str) -> None:
        """Remove ``name`` (tests; a missing name is ignored)."""
        with _lock:
            self._items.pop(name, None)
            self._origins.pop(name, None)

    def origin(self, name: str) -> str:
        """Who registered ``name`` (a plug-in's name, or a module)."""
        _ensure()
        return self._origins[name]

    def lookup(self, name: Any, where: str) -> T:
        """The entry ``name``, or a ValueError for the config key ``where``
        that lists the registered names."""
        _ensure()
        try:
            return self._items[name]
        except (KeyError, TypeError):
            raise ValueError(
                f"{where}: {self.what} {name!r} is not one of {self.names()}"
                " (a plug-in can register more: docs/plugins.md)"
            ) from None

    def names(self) -> list[str]:
        _ensure()
        return list(self._items)

    def values(self) -> list[T]:
        _ensure()
        return list(self._items.values())

    def items(self) -> list[tuple[str, T]]:
        _ensure()
        return list(self._items.items())

    def get(self, name: str, default: Any = None) -> Any:
        _ensure()
        return self._items.get(name, default)

    def __getitem__(self, name: str) -> T:
        _ensure()
        return self._items[name]

    def __contains__(self, name: object) -> bool:
        _ensure()
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        _ensure()
        return iter(list(self._items))

    def __len__(self) -> int:
        _ensure()
        return len(self._items)

    def __repr__(self) -> str:
        return f"Registry({self.what!r}, {list(self._items)})"


def _module_of(entry: Any) -> str:
    return str(getattr(entry, "__module__", None) or type(entry).__module__)


def _ensure() -> None:
    if not _started:
        load_plugins()


def _run(name: str, target: Callable[[], Any] | Any) -> None:
    """Run one plug-in: a module's ``register()``, or a callable."""
    global _origin
    fn = target
    if not callable(fn):
        fn = getattr(target, "register", None)
        if not callable(fn):
            raise ValueError(f"plug-in {name}: module has no register() function")
    previous, _origin = _origin, name
    try:
        fn()
    finally:
        _origin = previous


def _import(spec: str) -> Any:
    """``module`` or ``module:attribute``."""
    module, _, attr = spec.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split(".") if attr else ():
        obj = getattr(obj, part)
    return obj


def load_plugins(modules: Iterable[str] = ()) -> list[str]:
    """Run the built-in plug-ins and every ``robofast.plugins`` entry point
    (the first time only), then each of ``modules`` not run yet (``module``
    or ``module:function``). Returns the plug-ins run by this call.

    A plug-in that fails to import or register is an error (ValueError):
    a config that names it cannot be served without it."""
    global _started
    ran: list[str] = []
    with _lock:
        if not _started:
            _started = True
            for spec in BUILTIN_PLUGINS:
                if spec not in _loaded:
                    _loaded.add(spec)
                    _run(spec, _import(spec))
                    ran.append(spec)
            for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
                key = f"entry-point:{ep.name}"
                if key in _loaded:
                    continue
                _loaded.add(key)
                try:
                    target = ep.load()
                except Exception as exc:
                    raise ValueError(f"plug-in {ep.name} ({ep.value}): {exc!r}") from exc
                _run(f"{ep.name} ({ep.value})", target)
                log.info("plug-in %s (%s) loaded", ep.name, ep.value)
                ran.append(key)
        for spec in modules:
            if not isinstance(spec, str) or not spec:
                raise ValueError(f"plugins: a module name, not {spec!r}")
            if spec in _loaded:
                continue
            try:
                target = _import(spec)
            except (ImportError, AttributeError) as exc:
                raise ValueError(f"plugins: {spec}: {exc}") from exc
            _loaded.add(spec)
            _run(spec, target)
            log.info("plug-in %s loaded", spec)
            ran.append(spec)
    return ran


def load_config_plugins(config: Any, where: str = "plugins") -> list[str]:
    """Load a config's ``plugins`` list (absent: nothing); see the module doc."""
    if not isinstance(config, dict):
        return load_plugins()
    mods = config.get("plugins") or []
    if isinstance(mods, str) or not isinstance(mods, list):
        raise ValueError(f"{where}: a list of module names, not {mods!r}")
    return load_plugins(mods)
