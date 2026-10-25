# SPDX-License-Identifier: Apache-2.0
"""Reading the UNIMOC register description file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cypilot.core.descriptions import DescriptionError, load_descriptions

FILE = {
    "schema": 1,
    "firmware": {
        "name": "unimoc",
        "version": "0.3.0",
        "vcs_revision": "49facfdeeb13b920",
        "target": "native_sim",
    },
    "registers": [
        {
            "name": "unimoc.sim.imposed_speed",
            "type": "real32",
            "size": 1,
            "default": [0],
            "mutable": True,
            "persistent": False,
            "unit": "rad/s",
            "applies": "immediate",
            "help": "Mechanical speed of the shaft in imposed-speed mode",
        },
        {
            "name": "unimoc.sim.shaft_mode",
            "type": "natural8",
            "size": 1,
            "mutable": True,
            "persistent": False,
            "min": 0,
            "max": 1,
        },
    ],
}


def test_a_file_is_read_from_a_path_text_or_data(tmp_path: Path) -> None:
    path = tmp_path / "registers.json"
    path.write_text(json.dumps(FILE))
    for source in (path, json.dumps(FILE), FILE):
        descriptions = load_descriptions(source)
        assert descriptions.firmware["version"] == "0.3.0"
        speed = descriptions.get("unimoc.sim.imposed_speed")
        assert speed is not None and speed.unit == "rad/s" and speed.applies == "immediate"
        mode = descriptions.get("unimoc.sim.shaft_mode")
        assert mode is not None and (mode.min, mode.max, mode.unit, mode.help) == (0, 1, None, "")
        assert descriptions.get("no.such") is None


def test_other_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(DescriptionError, match="schema 2"):
        load_descriptions(FILE | {"schema": 2})
    with pytest.raises(DescriptionError, match="no registers list"):
        load_descriptions({"schema": 1})
    with pytest.raises(DescriptionError, match="bad register entry"):
        load_descriptions({"schema": 1, "registers": [{"name": "x"}]})
    with pytest.raises(DescriptionError, match="cannot read"):
        load_descriptions(tmp_path / "missing.json")
