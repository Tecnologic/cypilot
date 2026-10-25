# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. The DSDL is taken from CYPILOT_UNIMOC (see cypilot.dsdl), compiled once."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cypilot.dsdl import ensure_dsdl

for _name in ("pycyphal", "can", "nunavut", "pydsdl"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@pytest.fixture(scope="session")
def dsdl() -> Path:
    """Makes the compiled DSDL importable (uavcan, reg, unimoc, cymon)."""
    return ensure_dsdl()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    """Headless Chromium of Playwright for the web UI tests.

    Per module: Playwright's sync API runs an event loop on the main thread while
    it is open, and the other tests use ``asyncio.run``.

    ``CYPILOT_CHROMIUM`` names a Chromium binary when Playwright's own is not
    installed (``python -m playwright install chromium``). The tests skip without
    a browser, but not on Linux in CI.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    executable = os.environ.get("CYPILOT_CHROMIUM")
    with sync_api.sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(executable_path=executable or None)
        except Exception as error:
            if os.environ.get("CI") and sys.platform == "linux":
                raise
            pytest.skip(f"no Chromium for Playwright: {error}")
        yield chromium
        chromium.close()


@pytest.fixture
def fake_nmcli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """An executable ``nmcli`` (tests/fake_nmcli.py) on the access point; its state file and its path."""
    import json

    if sys.platform == "win32":
        pytest.skip("NetworkManager is Linux only")
    state = tmp_path / "nmcli.json"
    state.write_text(
        json.dumps(
            {
                "state": "connected",
                "connection": "cypilot-ap",
                "address": "10.42.0.1",
                "known": ["workshop"],
                "ap_ssid": "cypilot",
                "ap_psk": "initial-key",
                "networks": [["workshop", 70, "WPA2"], ["office", 82, "WPA2"], ["Cafe:Gast", 40, ""]],
                "passwords": {"workshop": "secret-workshop", "office": "secret-office", "Cafe:Gast": None},
            }
        )
    )
    nmcli = tmp_path / "nmcli"
    script = Path(__file__).parent / "fake_nmcli.py"
    nmcli.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(script)!r}, run_name='__main__')\n"
    )
    nmcli.chmod(0o755)
    monkeypatch.setenv("FAKE_NMCLI_STATE", str(state))
    yield state, nmcli
