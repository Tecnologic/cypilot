# SPDX-License-Identifier: Apache-2.0
"""A run in the web UI against the released simulation: engage, a setpoint step and a trip."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from cypilot.simulation import SimInstance
from web_support import WebServer

pytestmark = pytest.mark.browser

DRIVE = 10


def test_a_run_of_an_engage_a_step_and_a_trip(browser: Any, sim: SimInstance, tmp_path: Path) -> None:
    with WebServer(runs=tmp_path / "runs") as web:
        web.post("/api/connect", {"interfaces": [sim.slcan_interfaces[0]]})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda dialog: dialog.accept("step and trip" if dialog.type == "prompt" else None))
        page.goto(web.url)
        page.wait_for_selector(f"[data-node='{DRIVE}']", timeout=30000)

        page.click("#record")
        page.wait_for_selector("#recording")
        page.click("text=Take control")
        page.click("nav.views button[data-view='control']")
        page.wait_for_selector("text=readiness subject 1034")
        page.click("#enable")
        page.wait_for_selector(f"[data-node='{DRIVE}'] .chip.on", state="attached")
        page.select_option("#setpoint-mode", "current")
        page.locator(".setpoint-value").nth(1).fill("2")
        page.click("#send-setpoint")
        page.wait_for_timeout(1000)

        # The trip: the DC link rises above its limit (58 V) in the plant of the simulation.
        page.click("nav.views button[data-view='sim']")
        row = page.locator("[data-input='unimoc.sim.dc_link_voltage']")
        row.wait_for(timeout=30000)
        before = row.locator("input").input_value()
        row.locator("input").fill("59")
        row.locator("button", has_text="Write").click()
        page.wait_for_selector(f"[data-node='{DRIVE}'] .chip.bad", state="attached", timeout=10000)
        page.wait_for_timeout(1500)
        page.click("#stop-run")
        page.wait_for_selector("#recording", state="detached")

        page.click("nav.views button[data-view='runs']")
        run = page.locator("tr[data-run]")
        run.wait_for()
        with page.expect_download() as info:
            run.locator("text=Download").click()
        with zipfile.ZipFile(io.BytesIO(Path(info.value.path()).read_bytes())) as archive:
            names = archive.namelist()
            meta = json.loads(archive.read(next(n for n in names if n.endswith("/run.json"))))
            frames = archive.read(next(n for n in names if n.endswith("/frames.log"))).decode()
        markers = [m["text"] for m in meta["markers"]]
        assert f"{DRIVE} engaged" in markers and f"{DRIVE} tripped" in markers, markers
        assert any("unimoc.sim.dc_link_voltage written" in m for m in markers)
        assert meta["nodes"][str(DRIVE)]["registers"]["unimoc.motor.limits.i_max"]
        assert frames.count("\n") > 100
        assert any(n.endswith(".csv") and "node10_" in n for n in names)

        run.locator("text=Replay").click()
        page.wait_for_selector("#plot-main canvas")
        page.click("nav.views button[data-view='runs']")
        run.locator("input[type=checkbox]").check()
        page.click("text=Delete")
        page.wait_for_selector("tr[data-run]", state="detached")

        # Recover for the next tests of the module: the plant back, the trip acknowledged.
        page.click("nav.views button[data-view='sim']")
        row.locator("input").fill(before)
        row.locator("button", has_text="Write").click()
        page.click("nav.views button[data-view='control']")
        page.click("text=Recover")
        page.wait_for_selector(f"[data-node='{DRIVE}'] .chip.bad", state="detached", timeout=10000)
        page.close()
