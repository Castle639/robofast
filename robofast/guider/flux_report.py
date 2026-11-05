"""Report a guider's fiber flux to the spectrograph service.

With ``guide.fiber_flux`` on, the guide loop measures every guide frame's
counts inside the fiber's footprint (:func:`robofast.guider.loop.fiber_counts`).
:class:`FluxReporter` sends them, every ``interval`` seconds, to the
spectrograph service's ``POST /flux`` as::

    {"telescope": "T1", "frames": [[t_start, t_end, counts], ...]}

(times in Unix seconds, UTC; docs/kiwispec.md, "Flux from the FAU guide
frames"). The guider service's config::

    "flux_report": {"url": "http://127.0.0.1:8130", "telescope": "T1",
                    "interval": 2.0, "timeout": 5.0}

``telescope`` is the spectrograph service's name for this telescope. Loose
coupling: a report that fails is kept (bounded) and sent with the next one;
the guide loop never waits for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from robofast.cancel import isolated

log = logging.getLogger(__name__)


@dataclass
class FluxReportConfig:
    url: str
    telescope: str
    interval: float = 2.0
    timeout: float = 5.0
    #: Frames kept while the spectrograph service does not answer.
    max_pending: int = 5000

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FluxReportConfig:
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"flux_report: unknown keys {sorted(unknown)}")
        c = cls(**d)
        if not c.url or not c.telescope:
            raise ValueError("flux_report needs url and telescope")
        if c.interval <= 0 or c.timeout <= 0:
            raise ValueError("flux_report: interval and timeout must be positive")
        return c


class FluxReporter:
    """Drains a :class:`~robofast.guider.loop.Guider`'s ``flux_frames``
    and posts them (module doc)."""

    def __init__(self, guider: Any, config: FluxReportConfig) -> None:
        self.guider = guider
        self.config = config
        self.pending: list[list[float]] = []
        self.sent = 0
        self.failures = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    def _drain(self) -> None:
        """Move the guider's new frames to ``pending``, in Unix time."""
        q = self.guider.flux_frames
        if not q:
            return
        offset = time.time() - self.guider.clock.now()
        while q:
            t0, t1, c = q.popleft()
            self.pending.append([t0 + offset, t1 + offset, c])
        del self.pending[: -self.config.max_pending]

    async def report(self) -> bool:
        """Send what is pending now; True if it was accepted (or nothing to send)."""
        import httpx

        self._drain()
        if not self.pending:
            return True
        frames = list(self.pending)
        body = {"telescope": self.config.telescope, "frames": frames}
        url = self.config.url.rstrip("/") + "/flux"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout, trust_env=False) as c:
                r = await isolated(c.post(url, json=body))
            if r.status_code != 200:
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("flux report to %s failed: %s", url, self.last_error)
            return False
        del self.pending[: len(frames)]
        self.sent += len(frames)
        self.last_error = None
        return True

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.interval)
            await self.report()

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self.config.timeout):
                await self.report()  # the last frames

    def status(self) -> dict[str, Any]:
        return {
            "telescope": self.config.telescope,
            "sent": self.sent,
            "pending": len(self.pending) + len(self.guider.flux_frames),
            "failures": self.failures,
            "last_error": self.last_error,
        }
