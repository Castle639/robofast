"""KiwiSpec as an instrument type (``"kiwispec"``; docs/kiwispec.md).

MINERVA's fiber-fed spectrograph, driven through robofast's KiwiSpec
service (:mod:`robofast.devices.kiwispec_service`). Registered in
:data:`robofast.instruments.INSTRUMENT_TYPES` by the devices package's
built-in plug-in (:func:`robofast.devices.register`):

* requests: ``SPECTRUM`` configurations (always guided), with KiwiSpec's
  own instrument-config ``extra_params``: ``iodine`` (the iodine cell in
  the beam; None: the service's default; sent as the stage position
  ``"in"``/``"out"``) and ``target_counts`` (exposure-meter counts that end
  the exposure; ``exposure_time`` is then the maximum);
* calibration plans: the service's calibration frames (``thar``,
  ``slitflat``, ``fiberflat``, ``bias``, ``dark``; it refuses the ones the
  spectrograph cannot take -- MINERVA's takes only biases and darks);
* the sequencer: an enclosure config's ``"spectrograph": {"type":
  "kiwispec", "url": ..., "timeout": s, "abort_timeout": s, "queue_wait":
  s, ...}`` builds a
  :class:`~robofast.devices.kiwispec_service.KiwiSpecRemote`
  (whose ``record_keys`` put the iodine stage each frame was taken with at
  the top of its sidecar record);
* the guider: the fiber backlight behind the FAU, a guider backlight kind
  (:data:`robofast.guider.hardware.BACKLIGHTS`): ``"backlight":
  {"kind": "kiwispec", "url": "http://host:port"}`` switches it with the
  service's ``POST /backlight``.
"""

from __future__ import annotations

from typing import Any

from robofast.instruments import InstrumentType, Param, flag, number

#: The service's calibration frame kinds (its ``KINDS`` less ``science``).
CALIBRATION_KINDS = ("thar", "slitflat", "fiberflat", "bias", "dark")


def _client(entry: dict[str, Any], *, name: str, queue_wait: float) -> Any:
    from robofast.devices.kiwispec_service import KiwiSpecRemote

    if "url" not in entry:
        raise ValueError("spectrograph: 'url' is required")
    return KiwiSpecRemote(
        str(entry["url"]),
        timeout=float(entry.get("timeout", 10.0)),
        abort_timeout=float(entry.get("abort_timeout", 45.0)),
        client=name,
        queue_wait=float(entry.get("queue_wait", queue_wait)),
    )


KIWISPEC = InstrumentType(
    "kiwispec",
    "SPECTRA",
    accepts=("SPECTRUM",),
    guided=True,
    params={
        "iodine": Param(
            flag,
            ("SPECTRUM",),
            frame=lambda v: "in" if v else "out",
            doc="the iodine cell in the beam (None: the service's default)",
        ),
        "target_counts": Param(
            number(1.0, 1e15),
            ("SPECTRUM",),
            doc="exposure-meter counts that end the exposure (exposure_time: the maximum)",
        ),
    },
    calibration_kinds=CALIBRATION_KINDS,
    client_factory=_client,
    client_keys=frozenset({"url", "timeout", "abort_timeout", "queue_wait"}),
    status_fields=(
        ("exp. meter", "expmeter_total", 0, ""),
        ("CCD", "ccd_temp", 1, "C"),
        ("iodine stage", "stage", None, ""),
        ("pressure", "spec_pressure", 4, ""),
    ),
)


def backlight(spec: dict[str, Any], ctx: Any) -> Any:
    """The guider's backlight kind ``kiwispec``: the KiwiSpec service's
    ``POST /backlight`` (docs/kiwispec.md) at ``spec["url"]``."""
    from robofast.cancel import isolated
    from robofast.guider.base import GuiderError
    from robofast.guider.loop import Backlight

    if not isinstance(spec.get("url"), str) or not spec["url"]:
        raise ValueError("backlight: the KiwiSpec service's 'url' is required")
    url = spec["url"].rstrip("/")
    timeout = float(spec.get("timeout", 15.0))

    async def _set(on: bool) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await isolated(client.post(url + "/backlight", json={"on": on}))
        if r.status_code >= 400:
            raise GuiderError(f"backlight {on}: {r.status_code} {r.text[:200]}")

    return Backlight(_set)
