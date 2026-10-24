# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math
from pathlib import Path

import pytest

from cypilot.core.registers import RegisterValue, from_dsdl, parse_like, to_dsdl


def test_format_and_parse_round_trip() -> None:
    cases = [
        RegisterValue("real32", [0.15]),
        RegisterValue("natural8", [15]),
        RegisterValue("integer16", [-3, 4]),
        RegisterValue("bit", [True]),
        RegisterValue("string", "unimoc"),
        RegisterValue("unstructured", b"\x01\xff"),
    ]
    for value in cases:
        assert parse_like(value.format(), value).same_as(value), value


def test_parse_keeps_the_register_type_and_length() -> None:
    template = RegisterValue("natural16", [106])
    assert parse_like("0x6a", template) == RegisterValue("natural16", [106])
    with pytest.raises(ValueError, match="range"):
        parse_like("65536", template)
    with pytest.raises(ValueError, match="1 value"):
        parse_like("1, 2", template)
    with pytest.raises(ValueError):
        parse_like("x", template)
    assert parse_like("off", RegisterValue("bit", [True])) == RegisterValue("bit", [False])
    with pytest.raises(ValueError, match="bit"):
        parse_like("2", RegisterValue("bit", [True]))
    with pytest.raises(ValueError, match="range"):
        parse_like("-129", RegisterValue("integer8", [0]))


def test_same_as_follows_the_rounding_of_the_type() -> None:
    assert RegisterValue("real32", [0.1]).same_as(RegisterValue("real32", [0.10000000149011612]))
    assert not RegisterValue("real64", [0.1]).same_as(RegisterValue("real64", [0.10000000149011612]))
    assert RegisterValue("real32", [math.nan]).same_as(RegisterValue("real32", [math.nan]))
    assert not RegisterValue("real32", [1.0]).same_as(RegisterValue("natural8", [1]))
    assert not RegisterValue("natural8", [1]).same_as(RegisterValue("natural8", [1, 2]))


def test_unknown_kind() -> None:
    with pytest.raises(ValueError):
        RegisterValue("float", [1.0])


def test_dsdl_round_trip(dsdl: Path) -> None:
    cases = [
        RegisterValue("empty", None),
        RegisterValue("string", "unimoc"),
        RegisterValue("unstructured", b"\x00\x7f"),
        RegisterValue("bit", [True, False]),
        RegisterValue("natural16", [106]),
        RegisterValue("integer64", [-1]),
        RegisterValue("real32", [0.25]),
    ]
    for value in cases:
        assert from_dsdl(to_dsdl(value)).same_as(value), value
