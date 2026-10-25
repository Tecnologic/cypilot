# SPDX-License-Identifier: Apache-2.0
"""Runs in the web UI: record, stop, list, download, replay and delete, with the demo devices."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from web_support import WebServer

pytestmark = pytest.mark.browser

DRIVE = 10


def test_record_stop_download_replay_and_delete(browser: Any, dsdl: Path, tmp_path: Path) -> None:
    with WebServer(runs=tmp_path / "runs") as web:
        web.post("/api/connect", {"demo_devices": 1})
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        dialogs: list[str] = []

        def answer(dialog: Any) -> None:
            dialogs.append(dialog.message)
            dialog.accept("bench run" if dialog.type == "prompt" else None)

        page.on("dialog", answer)
        page.goto(web.url)
        page.wait_for_selector(f"[data-node='{DRIVE}']")
        assert page.locator("#recording").count() == 0  # nothing records by itself

        page.click("#record")
        page.wait_for_selector("#recording")
        page.click("text=Take control")
        page.click("nav.views button[data-view='control']")
        page.click("#enable")
        page.wait_for_selector(f"[data-node='{DRIVE}'] .chip.on", state="attached")
        page.click("#stop-all")
        page.wait_for_function("() => /REC 0:0[2-9]/.test(document.querySelector('#recording').innerText)")
        page.click("#stop-run")
        page.wait_for_selector("#recording", state="detached")

        page.click("nav.views button[data-view='runs']")
        row = page.locator("tr[data-run]")
        row.wait_for()
        assert "bench run" in row.inner_text()
        with page.expect_download() as info:
            row.locator("text=Download").click()
        data = Path(info.value.path()).read_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
        assert any(n.endswith("/frames.log") for n in names) and any(
            n.endswith("node10_telemetry.csv") for n in names
        )

        row.locator("a", has_text="bench run").click()  # the details: files, notes, markers
        page.wait_for_selector("#run-detail")
        assert "control taken" in page.inner_text("#run-detail")
        page.fill("#run-notes", "fine")
        page.click("#run-detail >> text=Save")
        page.wait_for_selector("text=Saved.")

        row.locator("text=Replay").click()
        page.wait_for_selector("#replay-banner:visible")
        page.wait_for_selector("#plot-main canvas")
        assert "10/telemetry value[0]" in page.inner_text("#plot-main .u-legend")

        page.click("nav.views button[data-view='runs']")
        row.locator("input[type=checkbox]").check()
        page.click("text=Delete")
        page.wait_for_selector("tr[data-run]", state="detached")
        assert any("cannot be undone" in d for d in dialogs)
        assert web.hub.runs is not None and web.hub.runs.summaries() == []
        page.close()
