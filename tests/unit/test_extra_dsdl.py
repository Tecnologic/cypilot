# SPDX-License-Identifier: Apache-2.0
"""Extra root namespaces (CYPILOT_DSDL): compiled against the standard ones and importable."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from cypilot.dsdl import DsdlError, ensure_dsdl


def _namespace(directory: Path, name: str) -> Path:
    root = directory / name
    (root / "cart").mkdir(parents=True)
    (root / "cart" / "Lights.0.1.dsdl").write_text(
        "uavcan.primitive.scalar.Bit.1.0 on\nfloat32 brightness\n@sealed\n"
    )
    return root


def test_an_extra_namespace_is_compiled_and_importable(dsdl: Path, tmp_path: Path) -> None:
    root = _namespace(tmp_path, "cypilot_test_vdc")
    ensure_dsdl(extra_roots=[root])
    lights = importlib.import_module("cypilot_test_vdc.cart").Lights_0_1(brightness=0.5)
    assert lights.brightness == pytest.approx(0.5) and not lights.on.value


def test_a_clashing_or_empty_namespace_is_refused(dsdl: Path, tmp_path: Path) -> None:
    with pytest.raises(DsdlError, match="part of the UNIMOC DSDL"):
        ensure_dsdl(extra_roots=[_namespace(tmp_path, "unimoc")])
    (tmp_path / "empty").mkdir()
    with pytest.raises(DsdlError, match="no DSDL root namespace"):
        ensure_dsdl(extra_roots=[tmp_path / "empty"])
