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
