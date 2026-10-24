# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. The DSDL is taken from CYPILOT_UNIMOC (see cypilot.dsdl), compiled once."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cypilot.dsdl import ensure_dsdl

for _name in ("pycyphal", "can", "nunavut", "pydsdl"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@pytest.fixture(scope="session")
def dsdl() -> Path:
    """Makes the compiled DSDL importable (uavcan, reg, unimoc, cymon)."""
    return ensure_dsdl()
