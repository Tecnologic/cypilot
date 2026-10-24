# SPDX-License-Identifier: Apache-2.0
"""Capabilities and profiles against the released UNIMOC simulation, with two drives."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cypilot.simulation import SimInstance
from sim.conftest import Ctl

DRIVE = 10


@pytest.fixture(scope="module")
def second_sim(
    sim_executable: Path, tmp_path_factory: pytest.TempPathFactory, dsdl: Path
) -> Iterator[SimInstance]:
    """A second simulated drive on its own slcan bus."""
    with SimInstance(sim_executable, tmp_path_factory.mktemp("sim_b") / "flash.bin") as instance:
        yield instance


@pytest.fixture
def ctl_b(second_sim: SimInstance, capsys: pytest.CaptureFixture[str]) -> Ctl:
    return Ctl(second_sim, capsys)


def test_caps_finds_the_drive_functions(ctl: Ctl) -> None:
    data = ctl.json("caps", str(DRIVE))
    expected = {"registers", "commands", "log", "armable", "setpoints", "cart drive", "scope", "simulation"}
    assert expected <= set(data["capabilities"])
    assert "local control" not in data["capabilities"]  # the release build has no e-bike
    found = {p["name"]: p for p in data["ports"]}
    assert (found["readiness"]["configured"], found["readiness"]["port_id"]) == (65534, 1024 + DRIVE)
    assert found["telemetry"]["type"] == "unimoc.drive.Telemetry.0.1"


def test_a_profile_copies_the_settings_to_another_drive(ctl: Ctl, ctl_b: Ctl, tmp_path: Path) -> None:
    profile = tmp_path / "drive_a.json"
    # Drive A gets other settings; drive B has its own name.
    for name, value in (("unimoc.motor.limits.i_max", "12.5"), ("unimoc.motor.pole_pairs", "7")):
        assert ctl.run("set", str(DRIVE), name, value)[0] == 0
    assert ctl_b.run("set", str(DRIVE), "uavcan.node.description", "FR drive")[0] == 0
    assert ctl.json("profile", "save", str(DRIVE), str(profile))["registers"] > 90

    differences = {d["name"]: d for d in ctl_b.json_rc(1, "profile", "diff", str(DRIVE), str(profile))}
    assert differences["unimoc.motor.limits.i_max"]["node"] == "15"
    assert differences["unimoc.motor.pole_pairs"]["profile"] == "7"
    assert differences["uavcan.node.description"]["skipped_on_load"]

    dry = ctl_b.json("profile", "load", str(DRIVE), str(profile), "--dry-run")
    assert set(dry["would_write"]) == {"unimoc.motor.limits.i_max", "unimoc.motor.pole_pairs"}

    result = ctl_b.json("profile", "load", str(DRIVE), str(profile), "--store")
    assert sorted(result["written"]) == ["unimoc.motor.limits.i_max", "unimoc.motor.pole_pairs"]
    assert result["rejected"] == {}
    assert result["still_different"] == []
    assert "uavcan.node.description" in result["skipped"]
    assert result["store_status"] == 0

    assert ctl_b.json("get", str(DRIVE), "unimoc.motor.pole_pairs")["value"] == [7]
    assert ctl_b.json("get", str(DRIVE), "uavcan.node.description")["value"] == "FR drive"
    remaining = ctl_b.json_rc(1, "profile", "diff", str(DRIVE), str(profile))
    assert {d["name"] for d in remaining} == {"uavcan.node.description"}  # only the skipped label


def test_a_rejected_value_is_reported(ctl: Ctl, tmp_path: Path) -> None:
    profile = tmp_path / "too_much.json"
    ctl.json("profile", "save", str(DRIVE), str(profile))
    data = json.loads(profile.read_text())
    current = data["registers"]["unimoc.motor.limits.i_max"]["value"]
    data["registers"]["unimoc.motor.limits.i_max"]["value"] = [5000.0]  # above the power stage
    profile.write_text(json.dumps(data))
    code, out = ctl.run("--json", "profile", "load", str(DRIVE), str(profile))
    assert code == 1
    result = json.loads(out)
    assert result["rejected"] == {
        "unimoc.motor.limits.i_max": f"{current[0]:.7g}"
    }  # the drive keeps its value
    assert result["store_status"] is None
