# SPDX-License-Identifier: Apache-2.0
"""Helpers of the GUI tests (the QApplication itself is the ``qapp`` fixture of conftest.py)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def wait_until(app: Any, condition: Callable[[], bool], timeout: float = 20.0) -> None:
    """Runs the Qt event loop until ``condition()`` holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        app.processEvents()
        time.sleep(0.02)
