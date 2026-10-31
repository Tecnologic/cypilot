# SPDX-License-Identifier: Apache-2.0
"""The web UI in headless Chromium against the released UNIMOC simulation."""

from __future__ import annotations

from typing import Any

import pytest

from cypilot.simulation import SimInstance
from web_support import WebServer

pytestmark = pytest.mark.browser

DRIVE = 10


def engaged(page: Any, value: bool) -> None:
    check = "" if value else "!"
    page.wait_for_function(
        f"() => {check}document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )


def test_enable_a_current_setpoint_the_registers_and_the_clock(browser: Any, sim: SimInstance) -> None:
    with WebServer() as web:
        web.post("/api/connect", {"interfaces": [sim.slcan_interfaces[0]]})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(web.url)
        page.wait_for_selector(f"[data-node='{DRIVE}']", timeout=30000)
        assert "disabled" in page.inner_text(f"[data-node='{DRIVE}'] .chips")

        page.click("text=Take control")
        page.click("nav.views button[data-view='control']")
        page.wait_for_selector("text=readiness subject 1034")
        page.click("#enable")
        engaged(page, True)
        page.select_option("#setpoint-mode", "current")
        page.locator(".setpoint-value").nth(1).fill("1.5")
        page.click("#send-setpoint")
        page.wait_for_timeout(500)
        page.click("#stop-all")
        engaged(page, False)

        page.click("nav.views button[data-view='registers']")
        row = page.locator("[data-register='unimoc.motor.limits.i_max']")
        row.wait_for(timeout=30000)
        assert row.locator("input").input_value() != ""
        unit = "document.querySelector(\"[data-register='unimoc.motor.limits.i_max'] td.unit\").innerText"
        page.wait_for_function(
            f"() => {unit}.includes('A') || document.body.innerText.includes('no register description file')"
        )

        page.click("nav.views button[data-view='sim']")
        page.wait_for_selector("#sim-clock")
        page.click("#sim-run")  # pause
        page.wait_for_function("() => document.querySelector('#sim-clock').innerText.startsWith('paused')")
        page.click("#sim-advance")
        page.click("#sim-run")  # run
        page.wait_for_function("() => document.querySelector('#sim-clock').innerText.startsWith('running')")
        page.close()


def shaft_mode(web: WebServer, mode: int) -> None:
    """Writes ``unimoc.sim.shaft_mode`` as a client of its own (it takes control and releases it)."""
    import httpx

    setup = {"X-Cypilot-Client": "setup"}
    take = httpx.post(f"{web.url}/api/control/take", json={"force": True}, headers=setup, timeout=10)
    take.raise_for_status()
    url = f"{web.url}/api/nodes/{DRIVE}/registers/unimoc.sim.shaft_mode"
    httpx.put(url, json={"value": str(mode)}, headers=setup, timeout=10).raise_for_status()
    httpx.post(f"{web.url}/api/control/release", headers=setup, timeout=10).raise_for_status()


def test_home_a_drive_in_position_control(browser: Any, sim: SimInstance) -> None:
    with WebServer() as web:
        web.post("/api/connect", {"interfaces": [sim.slcan_interfaces[0]]})
        # A locked shaft (imposed 0 rad/s) is the stop: the run stalls where it starts.
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web.url)
        page.wait_for_selector(f"[data-node='{DRIVE}']", timeout=30000)
        shaft_mode(web, 1)
        try:
            page.click("text=Take control")
            page.click("nav.views button[data-view='control']")
            page.wait_for_selector("text=readiness subject 1034")
            page.select_option("#setpoint-mode", "position")
            # Waits: Alpine shows Home after the mode change re-renders, not within select_option.
            page.wait_for_selector("#home", state="visible")
            assert page.is_disabled("#home")  # needs the enabled drive

            page.click("#enable")
            engaged(page, True)
            page.click("#send-setpoint")  # position 0: the drive is in position control
            page.wait_for_timeout(300)
            page.click("#home")
            done = "() => document.querySelector('#homing-state').innerText === 'homing done'"
            page.wait_for_function(done)
            page.click("#stop-all")
            engaged(page, False)
        finally:
            shaft_mode(web, 0)
            page.close()
