# SPDX-License-Identifier: Apache-2.0
"""The web UI in headless Chromium against demo devices: nodes, registers, control, stops, plots, phone."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from web_support import WebServer

pytestmark = pytest.mark.browser

DRIVE = 10
PHONE = {"width": 390, "height": 844}


@pytest.fixture
def server(dsdl: Path) -> Iterator[WebServer]:
    with WebServer() as web:
        web.post("/api/connect", {"demo_devices": 2})
        yield web


@pytest.fixture
def page(browser: Any, server: WebServer) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(server.url)
    page.wait_for_selector(f"[data-node='{DRIVE}']")
    yield page
    context.close()
    assert not errors, errors


def view(page: Any, name: str) -> None:
    page.click(f"nav.views button[data-view='{name}']")


def chips(page: Any) -> str:
    return str(page.inner_text(f"[data-node='{DRIVE}'] .chips"))


def test_nodes_and_registers_need_control_to_write(page: Any, server: WebServer) -> None:
    assert "nominal" in page.inner_text(f"[data-node='{DRIVE}']")
    view(page, "registers")
    row = page.locator("[data-register='demo.amplitude']")
    row.wait_for()
    row.locator("input").fill("2.5")
    assert row.locator("button", has_text="Write").is_disabled()  # nobody has control

    page.click("text=Take control")
    page.wait_for_selector("text=you have control")
    row.locator("button", has_text="Write").click()
    page.wait_for_selector("[data-register='demo.amplitude'] input.dirty", state="detached")
    assert row.locator("input").input_value() == "2.5"


def test_enable_stop_all_and_the_lease_of_a_closed_page(browser: Any, page: Any, server: WebServer) -> None:
    page.click("text=Take control")
    view(page, "control")
    page.click("#enable")
    page.wait_for_function(
        f"() => document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )
    page.click("#stop-all")
    page.wait_for_function(
        f"() => !document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )

    page.click("#enable")
    page.wait_for_function(
        f"() => document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )
    page.keyboard.press("Escape")  # STOP ALL from the keyboard
    page.wait_for_function(
        f"() => !document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )

    # A second page watches while the operator's page closes with the drive enabled.
    page.click("#enable")
    page.wait_for_function(
        f"() => document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )
    viewer = browser.new_page()
    viewer.goto(server.url)
    viewer.wait_for_selector(f"[data-node='{DRIVE}'] .chip.on")
    assert "you have control" not in viewer.inner_text("header")
    page.close()
    viewer.wait_for_selector(f"[data-node='{DRIVE}'] .chip.on", state="detached", timeout=5000)
    viewer.wait_for_selector("text=nobody has control")
    viewer.close()


def test_a_square_wave_and_a_step_from_the_setpoint_panel(page: Any, server: WebServer) -> None:
    page.click("text=Take control")
    view(page, "control")
    page.click("#enable")
    page.wait_for_function(
        f"() => document.querySelector(\"[data-node='{DRIVE}'] .chips\").innerText.includes('engaged')"
    )
    page.select_option("#setpoint-mode", "torque")
    page.select_option("#setpoint-signal", "square")
    page.locator("#send-setpoint").wait_for(state="hidden")
    page.locator(".wave-low").first.fill("-1")
    page.locator(".setpoint-value").first.fill("2")
    page.fill("#wave-period", "0.4")
    page.click("#start-waveform")
    device = server.hub._devices[0]
    for value in (2.0, -1.0, 2.0):
        eventually(lambda value=value: device.torque == value)

    page.select_option("#setpoint-signal", "step")
    page.locator("#wave-period").wait_for(state="hidden")
    page.locator(".wave-low").first.fill("0")
    page.locator(".setpoint-value").first.fill("1.5")
    page.fill("#wave-delay", "0.3")
    page.click("#start-waveform")
    eventually(lambda: device.torque == 0.0)
    eventually(lambda: device.torque == 1.5)
    page.click("#stop-all")
    eventually(lambda: device.torque == 0.0)


def eventually(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_a_viewer_can_stop_and_e_stop(browser: Any, page: Any, server: WebServer) -> None:
    viewer = browser.new_page()
    viewer.goto(server.url)
    viewer.wait_for_selector(f"[data-node='{DRIVE}']")
    viewer.click("#estop")
    viewer.wait_for_selector(f"[data-node='{DRIVE}'] .chip.bad")  # E-stop latched
    assert "E-stop latched" in chips(viewer)
    viewer.close()


def test_plots_show_the_telemetry(page: Any) -> None:
    view(page, "plots")
    page.wait_for_selector("#plot-source option", state="attached")
    page.click("#add-source")
    page.wait_for_selector("#plot-main canvas")
    page.wait_for_function("() => /[1-9][0-9]* messages/.test(document.body.innerText)")
    legend = page.inner_text("#plot-main .u-legend")
    assert "10/telemetry value[0]" in legend and "10/telemetry value[1]" in legend
    page.click("#stop-all")  # commands become markers on the time axis
    page.wait_for_function("() => markers.some(m => m.text === 'STOP ALL' && m.at > plotEnd - 10)")

    # Cursors on the scrolling plot stay at their time; own Y axes per channel.
    over = page.locator("#plot-main .u-over")
    box = over.bounding_box()
    assert box is not None
    over.click(position={"x": box["width"] * 0.5, "y": box["height"] / 2})
    over.click(position={"x": box["width"] * 0.9, "y": box["height"] / 2})
    readout = page.locator("#readout-main")
    readout.wait_for()
    dt = float(readout.locator("tbody tr").first.locator("td.num").nth(2).inner_text().split()[0])
    assert dt > 0
    assert readout.locator("tbody tr").count() == 1 + page.evaluate("() => plots.main.series.length - 1")
    page.locator(".own-axes:visible").check()
    page.wait_for_function(
        "() => plots.main"
        " && document.querySelectorAll('#plot-main .u-axis').length === plots.main.series.length"
    )
    assert page.evaluate("() => JSON.parse(localStorage.getItem('cypilot.ownAxes'))") is True


def test_the_phone_layout_keeps_the_stops_on_screen(browser: Any, server: WebServer) -> None:
    page = browser.new_page(viewport=PHONE)
    page.goto(server.url)
    page.wait_for_selector(f"[data-node='{DRIVE}']")
    for name in ("registers", "control", "plots"):
        view(page, name)
        for button in ("#stop-all", "#estop"):
            box = page.locator(button).bounding_box()
            assert box is not None and box["y"] >= 0 and box["y"] + box["height"] <= PHONE["height"]
    nav = page.locator("nav.views").bounding_box()
    assert nav is not None and nav["y"] + nav["height"] >= PHONE["height"] - 1  # at the bottom
    page.close()


def test_the_pin_login(browser: Any, dsdl: Path) -> None:
    with WebServer(pin="2468") as web:
        page = browser.new_page()
        page.goto(web.url)
        page.wait_for_selector("#pin")
        page.fill("#pin", "1111")
        page.click("text=Log in")
        page.wait_for_selector(".toast.bad")
        page.fill("#pin", "2468")
        page.click("text=Log in")
        page.wait_for_selector("#pin", state="hidden")
        page.wait_for_selector("#connect")  # not connected: the bus view
        page.close()
