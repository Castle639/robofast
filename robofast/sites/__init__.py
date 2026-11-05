"""Site code: each site's adapters, as plug-ins (docs/plugins.md).

Nothing here is loaded by core. A site's config lists the modules it needs
in its ``plugins`` (``sites/minerva/weather.yaml``:
``plugins: ["robofast.sites.minerva"]``, MINERVA's weather pages), or
an installed site package registers them through the ``robofast.plugins``
entry points (:mod:`robofast.plugins`).
"""
