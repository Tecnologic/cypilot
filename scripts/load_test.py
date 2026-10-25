#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Load test of the web app (docs/web_plan.md, W6): drives, a recording run and browsers, for a while.

    python scripts/load_test.py                          # 4 demo devices, 2 browsers, 60 s
    python scripts/load_test.py -i socketcan:can0 --browsers 0 --seconds 600   # on the Pi

The server runs in this process (uvicorn on a thread), records a run of
everything on the bus, and every browser shows the plots of every node. It
reports the CPU time of the process, the latency of /api/status (the event
loop's lag), the run's frames, samples and size, and the events the
browsers received. The browsers need Playwright with Chromium
(``CYPILOT_CHROMIUM`` names a Chromium binary).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))

import httpx

from cypilot.dsdl import ensure_dsdl
from web_support import WebServer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-i", "--interface", action="append", help="a real bus instead of demo devices")
    parser.add_argument("--demo", type=int, default=4, help="demo devices without --interface")
    parser.add_argument("--browsers", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()
    ensure_dsdl()

    with tempfile.TemporaryDirectory() as runs, WebServer(runs=Path(runs)) as web:
        connect = {"interfaces": args.interface} if args.interface else {"demo_devices": args.demo}
        web.post("/api/connect", connect)
        time.sleep(3)  # discovery
        nodes = httpx.get(web.url + "/api/nodes").json()
        print(f"{len(nodes)} nodes: {[n['node_id'] for n in nodes]}", flush=True)
        web.post("/api/runs/start", {"name": "load test"})

        playwright = browser = None
        pages = []
        if args.browsers:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(executable_path=os.environ.get("CYPILOT_CHROMIUM") or None)
            for _ in range(args.browsers):
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(web.url)
                page.wait_for_selector("[data-node]")
                page.evaluate(
                    "window.__events = 0; const ws = Alpine.$data(document.body).ws; "
                    "ws.addEventListener('message', () => window.__events++)"
                )
                page.click("nav.views button[data-view='plots']")
                for node in nodes:  # every published subject of every node in the plot
                    page.select_option("#node-select", str(node["node_id"]))
                    page.wait_for_timeout(500)
                    for key in page.eval_on_selector_all("#plot-source option", "os => os.map(o => o.value)"):
                        page.select_option("#plot-source", key)
                        page.click("#add-source")
                pages.append(page)

        cpu0, wall0 = time.process_time(), time.monotonic()
        latencies = []
        while time.monotonic() - wall0 < args.seconds:
            start = time.monotonic()
            httpx.get(web.url + "/api/status", timeout=10)
            latencies.append((time.monotonic() - start) * 1e3)
            if pages:
                pages[0].wait_for_timeout(500)  # the browsers' own loop keeps running
            else:
                time.sleep(0.5)
        cpu, wall = time.process_time() - cpu0, time.monotonic() - wall0

        active = httpx.get(web.url + "/api/runs").json()["active"]
        events = [page.evaluate("window.__events") for page in pages]
        web.post("/api/runs/stop")
        if browser is not None and playwright is not None:
            browser.close()
            playwright.stop()

    report = {
        "nodes": len(nodes),
        "browsers": args.browsers,
        "seconds": round(wall, 1),
        "cpu_percent": round(100 * cpu / wall, 1),
        "status_latency_ms": {
            "median": round(statistics.median(latencies), 1),
            "max": round(max(latencies), 1),
        },
        "run": {k: active[k] for k in ("frames", "samples", "size")},
        "frames_per_s": round(active["frames"] / wall),
        "browser_events_per_s": [round(e / wall) for e in events],
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
