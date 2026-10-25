# SPDX-License-Identifier: Apache-2.0
"""The Wi-Fi view of the Pi in the web UI, with a fake nmcli."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cypilot.wifi import Wifi
from web_support import WebServer

pytestmark = pytest.mark.browser


def test_join_a_network_forget_one_and_change_the_access_point(
    browser: Any, dsdl: Path, fake_nmcli: tuple[Path, Path]
) -> None:
    state, nmcli = fake_nmcli
    with WebServer(wifi=Wifi(nmcli=str(nmcli))) as web:
        web.post("/api/connect", {"demo_devices": 1})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(web.url)
        page.wait_for_selector("[data-node='10']")
        page.click("nav.views button[data-view='wifi']")
        page.wait_for_selector("#wifi-status")
        assert "own access point cypilot" in page.inner_text("#wifi-status")
        assert page.locator("#wifi-join").is_disabled()  # nobody has control

        page.click("text=Take control")
        page.click("#wifi-scan")
        page.click("tr[data-ssid='office']")
        assert page.input_value("#wifi-ssid") == "office"
        page.fill("#wifi-password", "secret-office")
        page.click("#wifi-join")
        page.wait_for_selector("text=Joined office: 192.168.1.50")
        page.wait_for_selector("[data-known='office']")

        page.locator("[data-known='workshop'] button").click()
        page.wait_for_selector("[data-known='workshop']", state="detached")
        page.fill("#ap-ssid", "cart")
        page.fill("#ap-key", "new-key-123")
        page.click("#ap-save")
        page.wait_for_selector("text=Saved: the access point")
        saved = json.loads(state.read_text())
        assert saved["ap_ssid"] == "cart" and saved["ap_psk"] == "new-key-123"
        page.click("#ap-start")
        page.wait_for_function(
            "() => document.querySelector('#wifi-status').innerText.includes('own access point')"
        )
        page.close()


def test_no_wifi_view_without_wifi_management(browser: Any, dsdl: Path) -> None:
    with WebServer() as web:
        page = browser.new_page()
        page.goto(web.url)
        page.wait_for_selector("nav.views button[data-view='bus']")
        assert page.locator("nav.views button[data-view='wifi']").count() == 0
        page.close()
