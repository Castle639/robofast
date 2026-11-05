"""MINERVA's legacy AO calibration as a guider calibration matrix
(docs/guider.md, "Calibration"): the SX AO model of ``minerva_library/ao.py``
and ``config/ao_*.ini``, a seed to compare with a measurement."""

from __future__ import annotations

import math

import numpy as np

from robofast.guider.calibration import Calibration, sky_matrix


def from_sx_legacy(
    arcsec_per_step: float,
    rotoffset_deg: float,
    plate_scale: float,
    pa_deg: float = 0.0,
    parity: int = 1,
    *,
    device: str = "sxao",
    port: str = "",
) -> Calibration:
    """The legacy MINERVA AO model (``minerva_library/ao.py``: ``tip =
    -(E cos r - N sin r)/aps``, ``tilt = (E sin r + N cos r)/aps``) as a
    matrix. A seed only: the legacy unit was never commissioned and its
    sign conventions were never verified."""
    r = math.radians(rotoffset_deg)
    steps_per_sky = np.array([[-math.cos(r), math.sin(r)], [math.sin(r), math.cos(r)]])
    steps_per_sky /= arcsec_per_step
    sky_per_step = np.linalg.inv(steps_per_sky)
    pix_per_step = np.linalg.inv(sky_matrix(plate_scale, pa_deg, parity)) @ sky_per_step
    return Calibration(
        device,
        port,
        pix_per_step.tolist(),
        units="steps",
        plate_scale=plate_scale,
        pa=pa_deg,
        parity=parity,
        notes="from legacy ARCSEC_PER_STEP/ROTOFFSET (unverified)",
    )
