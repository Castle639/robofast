"""Run ConformU against the KiwiSpec auxiliary-device server, on its simulators.

    python scripts/conformu_kiwispec_aux.py --conformu PATH [--out DIR]

Starts ``robofast_core.devices.kiwispec_aux_server`` with every port
``"sim"`` (the in-process TC200, APT, Dynamixel, LED, gauge and chiller
simulators) on a free loopback port, then runs ConformU's ``conformance``
and ``alpacaprotocol`` checks against its four devices: the iodine cell
heater (``switch/0``), the iodine stage (``filterwheel/0``), the fiber
backlight (``covercalibrator/0``) and the read-only telemetry
(``switch/1``). Exits non-zero if any check reports an error or issue.
Logs and results go to ``--out``; a summary is appended to
``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conformu_aqawan import start
from conformu_tcsgo import run_check

DEVICES = ("switch/0", "filterwheel/0", "covercalibrator/0", "switch/1")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-kiwispec-aux")
    ap.add_argument("--devices", default=",".join(DEVICES))
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    cpm = 25600.0
    config = out / "kiwispec_aux.json"
    config.write_text(
        json.dumps(
            {
                "name": "conformu",
                "listen": "127.0.0.1:0",
                "heater": {"port": "sim"},
                "stage": {
                    "port": {"url": "sim", "sim": {"counts_per_mm": cpm, "velocity": 200.0}},
                    "positions": {"in": 11.0, "flat": 68.0, "out": 102.0},
                    "counts_per_mm": cpm,
                    "poll_interval": 0.05,
                    "status_max_age": 0.05,
                },
                "backlight": {"led_port": "sim", "servo_port": "sim"},
                "telemetry": {"gauges": "sim", "chiller": "sim"},
            }
        )
    )
    server, hello = start(
        ["robofast_core.devices.kiwispec_aux_server", "--config", str(config)],
        out / "server.log",
    )
    summaries = []
    try:
        base = json.loads(hello)["url"]
        print(f"KiwiSpec aux simulators; Alpaca {base}", flush=True)
        for device in args.devices.split(","):
            kind, number = device.split("/")
            url = f"{base}/api/v1/{kind}/{number}"
            for check in args.checks.split(","):
                tag = f"{kind}{number}"
                summaries.append(run_check(args.conformu, kind, check, url, out, tag=tag))
                print(json.dumps({k: v for k, v in summaries[-1].items() if k != "details"}))
    finally:
        server.terminate()
        server.wait(10)

    failed = False
    report = ["### ConformU (KiwiSpec aux Switch, FilterWheel, CoverCalibrator vs simulators)", ""]
    for s in summaries:
        bad = s["exit"] != 0 or bool(s.get("errors")) or bool(s.get("issues"))
        failed |= bad
        report.append(f"- `{s['check']}`: exit {s['exit']}; {s['verdict']}")
        for d in s.get("details", []):
            report.append(f"  - {d}")
    text = "\n".join(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
