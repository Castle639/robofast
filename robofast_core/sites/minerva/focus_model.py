"""MINERVA's legacy focus model as robofast's (docs/autofocus.md): the
``guessFocus`` model of ``telescope_N.ini``, a starting point that
:mod:`robofast_core.focusmodel` refits from autofocus history."""

from __future__ import annotations

from robofast_core.autofocus import FocusModel

__all__ = ["legacy_model"]


def legacy_model(c: dict[str, float]) -> FocusModel:
    """The legacy ``guessFocus`` model (``telescope_N.ini`` F0, C0, T0, C1,
    dT0, C2, G0; ``sites/minerva/instruments.json``) as a :class:`FocusModel`
    on robofast's values: C0 on the primary's temperature (here the
    focuser's until the primary's is in the registry), C1 on M1 minus
    ambient (``{tel}.focuser.delta_t``, when published), C2 on sin(alt)."""
    return FocusModel(
        terms={
            "{tel}.focuser.temperature": float(c["C0"]),
            "{tel}.focuser.delta_t": float(c["C1"]),
            "sin:{tel}.tel.alt": float(c["C2"]),
        },
        reference={
            "{tel}.focuser.temperature": float(c["T0"]),
            "{tel}.focuser.delta_t": float(c["dT0"]),
            "sin:{tel}.tel.alt": float(c["G0"]),
        },
        zero_point=float(c["F0"]),
    )
