# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio

import pytest

from cypilot.core import profiles
from cypilot.core.profiles import Profile, ProfileError
from cypilot.core.registers import Register, RegisterValue, WriteResult


def reg(name: str, value: RegisterValue, *, mutable: bool = True, persistent: bool = True) -> Register:
    return Register(name, value, mutable, persistent)


def real(x: float) -> RegisterValue:
    return RegisterValue("real32", [x])


def nat16(x: int) -> RegisterValue:
    return RegisterValue("natural16", [x])


SCAN = [
    reg("uavcan.node.id", nat16(10)),
    reg("uavcan.node.description", RegisterValue("string", "FL drive")),
    reg("uavcan.sub.readiness.id", nat16(65534)),
    reg(
        "uavcan.sub.readiness.type",
        RegisterValue("string", "reg.udral.service.common.Readiness.0.1"),
        mutable=False,
    ),
    reg("unimoc.motor.limits.i_max", real(15.0)),
    reg("unimoc.motor.pole_pairs", RegisterValue("natural8", [15])),
    reg("unimoc.settings.pending", RegisterValue("bit", [False]), mutable=False, persistent=False),
    reg("uavcan.node.unique_id", RegisterValue("unstructured", b"\x01\x02"), mutable=False),
]


class FakeNode:
    """A register writer that refuses a write until another one was made, like UNIMOC's validation."""

    def __init__(self, registers: list[Register], refuse: dict[str, str] | None = None) -> None:
        self.values = {r.name: r.value for r in registers}
        self.refuse = refuse or {}  # name -> the name that must be written first
        self.order: list[str] = []

    async def write(self, name: str, value: RegisterValue) -> WriteResult:
        needed = self.refuse.get(name)
        if needed is not None and needed not in self.order:
            return WriteResult(False, reg(name, self.values[name]))
        self.values[name] = value
        self.order.append(name)
        return WriteResult(True, reg(name, value))


def test_profile_keeps_only_mutable_persistent_registers_and_round_trips() -> None:
    profile = Profile.from_registers(SCAN, node_name="FL drive", software_version=(0, 2), vcs_revision=0xABC)
    assert set(profile.registers) == {
        "uavcan.node.id",
        "uavcan.node.description",
        "uavcan.sub.readiness.id",
        "unimoc.motor.limits.i_max",
        "unimoc.motor.pole_pairs",
    }
    again = Profile.from_json(profile.to_json())
    assert again.registers.keys() == profile.registers.keys()
    assert all(again.registers[name].same_as(value) for name, value in profile.registers.items())
    assert (again.node_name, again.software_version, again.vcs_revision) == ("FL drive", (0, 2), 0xABC)


def test_unstructured_and_bit_values_survive_json() -> None:
    for value in (RegisterValue("unstructured", b"\x00\xff"), RegisterValue("bit", [True, False])):
        assert profiles.value_from_json(profiles.value_to_json(value)) == value


@pytest.mark.parametrize("text", ["", "[]", '{"format": 2, "registers": {}}', '{"format": 1}'])
def test_invalid_profiles_are_refused(text: str) -> None:
    with pytest.raises(ProfileError):
        Profile.from_json(text)


def test_diff_kinds() -> None:
    profile = Profile(
        registers={
            "unimoc.motor.limits.i_max": real(20.0),  # changed
            "unimoc.motor.pole_pairs": RegisterValue("real32", [15.0]),  # another type
            "unimoc.old.setting": real(1.0),  # not on the node
            "uavcan.node.id": nat16(10),  # equal
        }
    )
    kinds = {d.name: d.kind for d in profiles.diff(profile, SCAN)}
    assert kinds == {
        "unimoc.motor.limits.i_max": "changed",
        "unimoc.motor.pole_pairs": "type",
        "unimoc.old.setting": "missing",
        "uavcan.node.description": "extra",
        "uavcan.sub.readiness.id": "extra",
    }


def test_identity_and_port_registers_are_skipped_unless_asked() -> None:
    assert profiles.skipped("uavcan.sub.readiness.id")
    assert profiles.skipped("uavcan.pub.telemetry.id")
    assert profiles.skipped("uavcan.srv.cymon_read.id")
    assert profiles.skipped("uavcan.node.id")
    assert profiles.skipped("uavcan.node.description")
    assert not profiles.skipped("uavcan.sub.readiness.id", include_ports=True)
    assert not profiles.skipped("uavcan.node.id", include_identity=True)
    assert not profiles.skipped("unimoc.motor.limits.i_max")
    assert not profiles.skipped("uavcan.sub.readiness.type")


def test_load_writes_the_differences_and_skips_the_identity() -> None:
    source = Profile(
        registers={
            "uavcan.node.id": nat16(11),
            "uavcan.node.description": RegisterValue("string", "FR drive"),
            "uavcan.sub.readiness.id": nat16(1100),
            "unimoc.motor.limits.i_max": real(20.0),
            "unimoc.motor.pole_pairs": RegisterValue("natural8", [15]),
        }
    )
    node = FakeNode(SCAN)
    result = asyncio.run(profiles.load(source, SCAN, node))
    assert result.written == ["unimoc.motor.limits.i_max"]
    assert sorted(result.skipped) == ["uavcan.node.description", "uavcan.node.id", "uavcan.sub.readiness.id"]
    assert result.complete
    assert node.values["uavcan.node.id"] == nat16(10)

    everything = asyncio.run(
        profiles.load(source, SCAN, FakeNode(SCAN), include_ports=True, include_identity=True)
    )
    assert len(everything.written) == 4
    assert not everything.skipped


def test_load_repeats_writes_that_depend_on_each_other() -> None:
    scan = [reg("a", real(1.0)), reg("b", real(1.0)), reg("c", real(1.0))]
    source = Profile(registers={"a": real(2.0), "b": real(2.0), "c": real(2.0)})
    node = FakeNode(scan, refuse={"a": "c", "b": "a"})  # c first, then a, then b
    result = asyncio.run(profiles.load(source, scan, node))
    assert result.complete
    assert node.order == ["c", "a", "b"]


def test_load_reports_what_the_node_keeps() -> None:
    scan = [reg("a", real(1.0)), reg("b", real(1.0))]
    source = Profile(registers={"a": real(2.0), "b": real(2.0), "gone": real(1.0)})
    node = FakeNode(scan, refuse={"a": "never"})
    result = asyncio.run(profiles.load(source, scan, node))
    assert result.written == ["b"]
    assert result.rejected == {"a": real(1.0)}
    assert result.missing == ["gone"]
    assert not result.complete
