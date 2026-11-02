"""Ctrl-C under a device server's console script is an exit, not a traceback.

The ``robofast-*-server`` console scripts call ``main()`` directly (the
``if __name__ == "__main__"`` block never runs), so the KeyboardInterrupt
that arrives before a server's own stop handlers are installed must be
handled in ``main()`` itself.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

SERVERS = ("pdu_server", "pulizzi_server", "adc_server")


@pytest.mark.parametrize("name", SERVERS)
def test_ctrl_c_in_main_exits_quietly(name, monkeypatch):
    mod = importlib.import_module(f"robofast_core.devices.{name}")
    seen = []

    def interrupted(argv=None):
        seen.append(argv)
        raise KeyboardInterrupt

    monkeypatch.setattr(mod, "_main", interrupted)
    mod.main(["--config", "x.json"])  # no KeyboardInterrupt escapes
    assert seen == [["--config", "x.json"]]


def test_the_console_scripts_are_these_mains():
    root = Path(__file__).resolve().parents[2]
    scripts = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    for name in SERVERS:
        assert f"robofast_core.devices.{name}:main" in scripts.values(), name
