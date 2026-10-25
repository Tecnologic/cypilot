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
        page.close()
