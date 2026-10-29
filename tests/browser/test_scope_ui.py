# SPDX-License-Identifier: Apache-2.0
"""The multi-node scope in the web UI: two demo devices on one trigger."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from web_support import WebServer

pytestmark = pytest.mark.browser


def pick(page: Any, node: int, variable: str) -> None:
    page.locator(f"[data-scope-node='{node}'] input[data-var='{variable}']").check()


def test_two_nodes_on_one_trigger(browser: Any, dsdl: Path) -> None:
    with WebServer() as web:
        web.post("/api/connect", {"demo_devices": 2})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web.url)
        page.wait_for_selector("[data-node='11']")
        page.click("nav.views button[data-view='scope']")
        page.wait_for_selector("[data-scope-node='11']")
        pick(page, 10, "sine")
        pick(page, 11, "cosine")
        page.wait_for_selector("#trigger-problem:visible")
        assert page.locator("#capture").is_disabled()  # no shared trigger yet

        page.click("text=Take control")
        page.click("text=Set up the shared trigger")
        page.wait_for_selector("text=Shared trigger on subject 220", timeout=15000)
        assert page.locator(
            "[data-scope-node='11'] input[data-var='cosine']"
        ).is_checked()  # kept over the restart
        page.select_option("select[x-model='scope.mode']", "rising")
        page.fill("input[x-model='scope.level']", "0.5")
        page.fill("input[x-model='scope.samples']", "100")
        page.click("#capture")
        try:
            page.wait_for_function("() => /on 2 node/.test(document.querySelector('#scope-state').innerText)")
        except Exception:
            state, toast = page.inner_text("#scope-state"), page.locator(".toast").inner_text()
            raise AssertionError(f"no capture: state {state!r}, message {toast!r}") from None
        legend = page.inner_text("#scope-plot .u-legend")
        assert "10 sine [A]" in legend and "11 cosine [A]" in legend

        # Cursors: a click sets A, the next B; the readout shows the times and each channel there.
        over = page.locator("#scope-plot .u-over")
        box = over.bounding_box()
        assert box is not None
        over.click(position={"x": box["width"] * 0.25, "y": box["height"] / 2})
        over.click(position={"x": box["width"] * 0.75, "y": box["height"] / 2})
        readout = page.locator("#readout-scope")
        readout.wait_for()
        rows = readout.locator("tbody tr")
        assert rows.count() == 3  # t and the two channels
        a, b, dt = (float(rows.nth(0).locator("td.num").nth(i).inner_text().split()[0]) for i in range(3))
        assert b > a and dt == pytest.approx(b - a, rel=1e-3)
        expected = page.evaluate(
            "() => { const u = plots.scope; const x = cursors.scope.a; return valueAt(u, 1, x); }"
        )
        assert float(rows.nth(1).locator("td.num").first.inner_text()) == pytest.approx(expected, rel=1e-3)
        assert page.locator("#scope-plot .u-axis").count() == 2  # one x, one y

        page.locator(".own-axes:visible").check()
        page.wait_for_function("() => document.querySelectorAll('#scope-plot .u-axis').length === 3")
        assert readout.is_visible()  # the cursors stay over the redraw
        page.locator("button:visible", has_text="Clear cursors").click()
        readout.wait_for(state="hidden")
        page.close()
