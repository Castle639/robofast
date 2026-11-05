"""Run ConformU against the PWI Telescope, Focuser and Rotator server, backed
by the PWI4 and PWI2 simulators.

    python scripts/conformu_pwi.py --conformu PATH [--out DIR] [--backends pwi4,pwi2]

For each backend, starts ``robofast.devices.pwi_sim`` (connected,
enabled and homed, PWI4 reporting it: ConformU must be able to slew; the
server's FindHome is allowed here) and
``robofast.devices.pwi_server`` on free loopback ports, then runs
ConformU's ``conformance`` and ``alpacaprotocol`` checks on each device.
Exits non-zero if any check reports an error or an issue. Logs and results
go to ``--out``; a summary is appended to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
from conformu_aqawan import start
from conformu_tcsgo import run_check

from robofast.devices.pwi_sim import SIM_LATITUDE, SIM_LONGITUDE


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--conformu", required=True)
    ap.add_argument("--out", default="conformu-out")
    ap.add_argument("--backends", default="pwi4,pwi2")
    ap.add_argument("--devices", default="telescope,focuser,rotator")
    ap.add_argument("--checks", default="conformance,alpacaprotocol")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    summaries = []
    for backend in args.backends.split(","):
        procs = []
        try:
            sim, line = start(
                [
                    "robofast.devices.pwi_sim",
                    *("--api", backend, "--port", "0", "--ready", "--homed", "--report-homed"),
                    *("--slew-rate", "6", "--home-time", "1", "--m3-time", "0.5"),
                ],
                out / f"{backend}-sim.log",
            )
            procs.append(sim)
            host, port = line.rsplit(" ", 1)[-1].rsplit(":", 1)
            config = out / f"{backend}.json"
            config.write_text(
                json.dumps(
                    {
                        "name": f"conformu-{backend}",
                        "backend": backend,
                        "latitude": SIM_LATITUDE,
                        "longitude": SIM_LONGITUDE,
                        "elevation": 2000.0,
                        # ConformU picks its own slew targets, some low in the sky.
                        "min_alt": 0.0,
                        "max_alt": 90.0,
                        "park_alt": 30.0,
                        "park_az": 90.0,
                        "focus_min": 0,
                        "focus_max": 33000,
                        "listen": "127.0.0.1:0",
                        "pwi": {"host": host, "port": int(port)},
                        "status_max_age": 0.1,
                        "slew_report_min": 0.5,
                        "watch_interval": 0,
                        # ConformU tests FindHome; the sims home harmlessly.
                        "allow_find_home": True,
                    }
                )
            )
            server, hello = start(
                ["robofast.devices.pwi_server", "--config", str(config)],
                out / f"{backend}-server.log",
            )
            procs.append(server)
            base = json.loads(hello)["url"]
            print(f"{backend} simulator {host}:{port}; Alpaca {base}", flush=True)
            for device in args.devices.split(","):
                url = f"{base}/api/v1/{device}/0"
                for check in args.checks.split(","):
                    s = run_check(args.conformu, device, check, url, out, f"{backend}-{device}")
                    summaries.append(s)
                    print(json.dumps({k: v for k, v in s.items() if k != "details"}), flush=True)
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                p.wait(10)

    failed = not summaries
    report = ["### ConformU (PWI server vs PWI4 and PWI2 simulators)", ""]
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
