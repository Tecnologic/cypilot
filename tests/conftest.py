# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. The DSDL is taken from CYPILOT_UNIMOC (see cypilot.dsdl), compiled once."""

from __future__ import annotations

import gc
import logging
import os
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


@pytest.fixture(scope="session")
def qapp() -> Iterator[Any]:
    """The QApplication of the GUI tests, on the offscreen platform unless another one is set.

    At the end it deletes every top-level widget while the application still
    exists: widgets left to the interpreter's shutdown can outlive the
    QApplication, which ended a Windows test run with exit code 1 after all
    tests had passed.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.processEvents()
    gc.collect()
    app.processEvents()
