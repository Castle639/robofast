"""tres-guider's tip/tilt calibration as a guider calibration matrix
(docs/guider.md, "Calibration"): the FLWO 60-inch PI tip/tilt model of
tres-guider's ``tiptilt.move_x_y``, a seed to compare with a measurement."""

from __future__ import annotations

import math

import numpy as np

from robofast_core.guider.calibration import Calibration


def from_tres_legacy(
    steps_per_pixel_tip: float,
    steps_per_pixel_tilt: float,
    theta_deg: float,
    flip: bool,
    *,
    device: str = "pi",
    port: str = "",
) -> Calibration:
    """tres-guider's ``tiptilt.move_x_y`` model (``tip = s*spp_tip*(x cos t
    - y sin t)``, ``tilt = spp_tilt*(x sin t + y cos t)``, ``s = -1`` if
    FLIP) as a matrix."""
    t = math.radians(theta_deg)
    s = -1.0 if flip else 1.0
    dev_per_pix = np.array(
        [
            [s * steps_per_pixel_tip * math.cos(t), -s * steps_per_pixel_tip * math.sin(t)],
            [steps_per_pixel_tilt * math.sin(t), steps_per_pixel_tilt * math.cos(t)],
        ]
    )
    return Calibration(
        device,
        port,
        np.linalg.inv(dev_per_pix).tolist(),
        notes="from tres-guider STEPS_PER_PIXEL/THETA/FLIP",
    )
