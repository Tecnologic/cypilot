# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from cypilot.core.capabilities import (
    DIAGNOSTIC_RECORD_SUBJECT,
    EXECUTE_COMMAND_SERVICE,
    REGISTER_LIST_SERVICE,
    Capability,
    PortList,
    capabilities,
    ports,
    setpoint_ports,
)
from cypilot.core.registers import Register, RegisterValue


def port(kind: str, name: str, port_id: int, type_name: str) -> list[Register]:
    return [
        Register(f"uavcan.{kind}.{name}.id", RegisterValue("natural16", [port_id]), True, True),
        Register(f"uavcan.{kind}.{name}.type", RegisterValue("string", type_name), False, False),
    ]


CYMON = [
    register
    for index, name in enumerate(("variables", "buffer_info", "setup", "arm", "read"))
    for register in port("srv", f"cymon_{name}", 120 + index, f"cymon.{name}")
]
DRIVE = [
    *port("sub", "readiness", 65534, "reg.udral.service.common.Readiness.0.1"),
    *port("sub", "torque_setpoint", 100, "uavcan.si.unit.torque.Scalar.1.0"),
    *port("sub", "current_setpoint", 107, "unimoc.drive.CurrentSetpoint.0.1"),
    *port("sub", "speed_setpoint", 65535, "uavcan.si.unit.angular_velocity.Scalar.1.0"),
    *port("pub", "telemetry", 212, "unimoc.drive.Telemetry.0.1"),
    *CYMON,
    Register("unimoc.motor.pole_pairs", RegisterValue("natural8", [15]), True, True),
]


def test_ports_resolve_the_readiness_of_the_node_id_and_disabled_ports() -> None:
    found = {(p.kind, p.name): p for p in ports(DRIVE, node_id=10)}
    readiness = found[("sub", "readiness")]
    assert (readiness.configured, readiness.port_id) == (65534, 1034)
    assert readiness.type_name == "reg.udral.service.common.Readiness.0.1"
    assert not found[("sub", "speed_setpoint")].enabled
    assert found[("pub", "telemetry")].port_id == 212
    # Without a node-ID (plug-and-play) the readiness has no subject yet.
    without = {(p.kind, p.name): p for p in ports(DRIVE, node_id=None)}
    assert not without[("sub", "readiness")].enabled


def test_capabilities_of_a_drive() -> None:
    port_list = PortList(
        publishers=frozenset({212, DIAGNOSTIC_RECORD_SUBJECT}),
        subscribers=frozenset({1034, 100, 107}),
        servers=frozenset({EXECUTE_COMMAND_SERVICE, REGISTER_LIST_SERVICE}),
    )
    found = capabilities(DRIVE, 10, port_list=port_list, vendor_code=0x80, unimoc=True)
    assert found == {
        Capability.REGISTERS,
        Capability.COMMANDS,
        Capability.LOG,
        Capability.ARMABLE,
        Capability.SETPOINTS,
        Capability.SCOPE,
        Capability.LOCAL_CONTROL,
    }
    assert [p.name for p in setpoint_ports(DRIVE, 10)] == ["current_setpoint", "torque_setpoint"]


def test_an_unknown_node_gets_only_what_it_has() -> None:
    assert capabilities([], 42) == set()
    only_registers = PortList(frozenset(), frozenset(), frozenset({REGISTER_LIST_SERVICE}))
    assert capabilities([], 42, port_list=only_registers) == {Capability.REGISTERS}
    # Bit 7 means local control only on a UNIMOC node.
    assert Capability.LOCAL_CONTROL not in capabilities(DRIVE, 10, vendor_code=0x80, unimoc=False)
