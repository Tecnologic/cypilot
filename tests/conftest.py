# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. The DSDL is taken from CYPILOT_UNIMOC (see cypilot.dsdl), compiled once."""

from __future__ import annotations

import logging
import os
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
def qapp() -> Any:
    """The QApplication of the GUI tests, on the offscreen platform unless another one is set."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])
