# SPDX-License-Identifier: Apache-2.0
"""cypilot-ctl against the released UNIMOC simulation."""

from __future__ import annotations

from sim.conftest import Ctl

DRIVE = 10


def test_nodes_shows_the_disabled_drive(ctl: Ctl) -> None:
    nodes = ctl.wait_for(lambda nodes: any(n["node_id"] == DRIVE and n["name"] for n in nodes))
    drive = next(n for n in nodes if n["node_id"] == DRIVE)
    assert drive["name"] == "unimoc"
    assert drive["unimoc"] is True  # found by its registers, not by the name
    assert drive["health"] == "NOMINAL"
    assert drive["state"] == ["disabled"]


def test_register_scan_finds_the_settings(ctl: Ctl) -> None:
    registers = {r["name"]: r for r in ctl.json("regs", str(DRIVE))}
    assert len(registers) > 90
    assert registers["uavcan.node.id"]["kind"] == "natural16"
    assert registers["uavcan.node.id"]["value"] == [DRIVE]
    i_max = registers["unimoc.motor.limits.i_max"]
    assert i_max["kind"] == "real32" and i_max["mutable"] and i_max["persistent"]
    assert registers["uavcan.sub.readiness.type"]["value"] == "reg.udral.service.common.Readiness.0.1"

    motor = ctl.json("regs", str(DRIVE), "unimoc.motor.*")
    assert motor and all(r["name"].startswith("unimoc.motor.") for r in motor)


def test_set_reports_accepted_and_rejected_writes(ctl: Ctl) -> None:
    code, out = ctl.run("set", str(DRIVE), "unimoc.motor.limits.i_max", "12.5")
    assert (code, out.strip()) == (0, "12.5")
    code, out = ctl.run("set", str(DRIVE), "unimoc.motor.limits.i_max", "5000")
    assert code == 1 and "rejected" in out
    assert ctl.json("get", str(DRIVE), "unimoc.motor.limits.i_max")["value"] == [12.5]
    code, _ = ctl.run("set", str(DRIVE), "uavcan.sub.readiness.type", "x")
    assert code == 2  # read-only


def test_missing_register(ctl: Ctl) -> None:
    code, _ = ctl.run("get", str(DRIVE), "unimoc.no.such.register")
    assert code == 1


def test_emergency_stop_is_latched_until_the_restart(ctl: Ctl) -> None:
    assert ctl.json("command", str(DRIVE), "emergency-stop")["status"] == 0
    ctl.wait_for(lambda nodes: any("E-stop latched" in n["state"] for n in nodes))
    assert ctl.json("command", str(DRIVE), "restart")["status"] == 0
    ctl.wait_for(lambda nodes: any(n["node_id"] == DRIVE and n["state"] == ["disabled"] for n in nodes))
