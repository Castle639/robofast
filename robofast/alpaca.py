"""Build a :class:`DeviceOwner` for an Alpaca device.

The driver is robofast's own :class:`~robofast.alpaca_client.AlpacaDevice`
(asyncio, one HTTP client per device, per-call timeouts, no process-wide
lock). It is built lazily by the owner, and connected (``Connected`` = True)
before its first command; a fresh one replaces it after a timeout.
"""

from __future__ import annotations

from robofast.alpaca_client import AlpacaDevice
from robofast.owner import DeviceOwner
from robofast.timeline import Timeline

#: The device types robofast drives.
DEVICE_TYPES = frozenset(
    {
        "telescope",
        "camera",
        "filterwheel",
        "focuser",
        "rotator",
        "dome",
        "safetymonitor",
        "observingconditions",
        "switch",
    }
)


def alpaca_owner(
    name: str,
    device_type: str,
    address: str,
    timeline: Timeline,
    *,
    number: int = 0,
    default_timeout: float = 5.0,
    privileged_members: frozenset[str] | set[str] = frozenset(),
    imagebytes: bool = True,
) -> DeviceOwner:
    """An owner for device ``number`` of ``device_type`` at ``address``
    (host:port). ``imagebytes``: a camera's images come by the ImageBytes
    transfer (else JSON; :mod:`robofast.alpaca_client`)."""
    if device_type.lower() not in DEVICE_TYPES:
        raise ValueError(f"unsupported device type {device_type!r}")
    return DeviceOwner(
        name,
        lambda: AlpacaDevice(address, device_type, number, imagebytes=imagebytes),
        timeline,
        default_timeout=default_timeout,
        privileged_members=privileged_members,
    )
