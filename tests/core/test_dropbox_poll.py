"""The drop box is taken in every ``dropbox_poll``; waiting ToOs are
re-checked every ``too_poll``, not at every drop-box tick."""

from __future__ import annotations

import asyncio
import json

from robofast.dispatch import DispatchConfig
from tests.core.test_dispatch import group, make, too


async def _wait_for(pred, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def test_dropped_too_is_taken_in_within_dropbox_poll(tmp_path):
    box = tmp_path / "too"
    box.mkdir()
    d = make([group("a")], too_dropbox=box, config=DispatchConfig())  # the shipped defaults
    task = asyncio.create_task(d.serve())
    try:
        await asyncio.sleep(0.2)  # the first tick has run; the next re-check is 10 s away
        tmp = tmp_path / "x.json"
        tmp.write_text(json.dumps(group("burst", too=too("next"))))
        tmp.rename(box / "x.json")
        assert await _wait_for(lambda: "burst-r" in d.requests, 1.5)
    finally:
        task.cancel()


async def test_waiting_toos_are_rechecked_every_too_poll_only(tmp_path):
    box = tmp_path / "too"
    box.mkdir()
    d = make([group("a")], too_dropbox=box, config=DispatchConfig(too_poll=0.5, dropbox_poll=0.05))
    calls = []
    orig = d.check_toos

    def counting():
        calls.append(asyncio.get_running_loop().time())
        orig()

    d.check_toos = counting
    task = asyncio.create_task(d.serve())
    try:
        await asyncio.sleep(1.2)  # about 24 drop-box ticks, 3 re-checks (0, 0.5, 1.0 s)
    finally:
        task.cancel()
    assert 2 <= len(calls) <= 4, calls
