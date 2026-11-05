"""Device servers robofast writes itself (see docs/design.md, section 3.1).

The package is also a built-in plug-in (:data:`robofast.plugins.BUILTIN_PLUGINS`):
:func:`register` adds the handlers of the hardware it drives to core's
registries, the way any plug-in does (docs/plugins.md).
"""


def register() -> None:
    from robofast.devices import kiwispec_instrument
    from robofast.guider.hardware import BACKLIGHTS
    from robofast.instruments import INSTRUMENT_TYPES

    INSTRUMENT_TYPES.register(kiwispec_instrument.KIWISPEC.name, kiwispec_instrument.KIWISPEC)
    # The fiber backlight behind the FAU, switched by the KiwiSpec service.
    BACKLIGHTS.register("kiwispec", kiwispec_instrument.backlight)
