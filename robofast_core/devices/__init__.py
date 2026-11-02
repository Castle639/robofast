"""Device servers robofast writes itself (see docs/design.md, section 3.1).

The package is also a built-in plug-in (:data:`robofast_core.plugins.BUILTIN_PLUGINS`):
:func:`register` adds the handlers of the hardware it drives to core's
registries, the way any plug-in does (docs/plugins.md).
"""


def register() -> None:
    from robofast_core.devices.kiwispec_instrument import KIWISPEC
    from robofast_core.instruments import INSTRUMENT_TYPES

    INSTRUMENT_TYPES.register(KIWISPEC.name, KIWISPEC)
