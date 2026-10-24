# SPDX-License-Identifier: Apache-2.0
"""What a node supports, found by discovery instead of a node type (``docs/plan.md``, section 4).

The sources are the scanned registers (the port registers
``uavcan.<pub|sub|srv|cln>.<name>.id`` with their read-only ``.type``
companions, and register prefixes), the node's ``uavcan.node.port.List`` and
the heartbeat. A drive with or without the cart registers, the e-bike and an
unknown Cyphal node all get what they actually support.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass

from cypilot.core.registers import Register
from cypilot.core.status import VendorStatus

UNSET_PORT_ID = 65535
READINESS_FOLLOWS_NODE_ID = 65534
"""UNIMOC: the readiness subject is ``READINESS_BASE + node-ID`` (UNIMOC v0.2.0)."""
READINESS_BASE = 1024

EXECUTE_COMMAND_SERVICE = 435
REGISTER_LIST_SERVICE = 385
DIAGNOSTIC_RECORD_SUBJECT = 8184

SETPOINTS = ("current_setpoint", "torque_setpoint", "speed_setpoint", "position_setpoint", "servo_setpoint")
CYMON_SERVICES = ("cymon_variables", "cymon_buffer_info", "cymon_setup", "cymon_arm", "cymon_read")

_PORT_REGISTER = re.compile(r"^uavcan\.(pub|sub|srv|cln)\.([^.]+)\.(id|type)$")


class Capability(enum.Enum):
    REGISTERS = "registers"
    COMMANDS = "commands"
    ARMABLE = "armable"
    SETPOINTS = "setpoints"
    CART = "cart drive"
    SCOPE = "scope"
    SIMULATION = "simulation"
    LOCAL_CONTROL = "local control"
    LOG = "log"


@dataclass(frozen=True)
class Port:
    """One configurable port of a node."""

    kind: str  # pub, sub, srv or cln
    name: str
    configured: int | None
    """The value of the ``.id`` register."""
    port_id: int | None
    """The port-ID in use; None when disabled (or not yet known for a node-ID based one)."""
    type_name: str | None

    @property
    def enabled(self) -> bool:
        return self.port_id is not None


@dataclass(frozen=True)
class PortList:
    """The parts of a node's ``uavcan.node.port.List`` that discovery uses."""

    publishers: frozenset[int]
    subscribers: frozenset[int]
    servers: frozenset[int]


def ports(registers: Iterable[Register], node_id: int | None) -> list[Port]:
    """The ports named by the port registers, sorted by kind and name."""
    ids: dict[tuple[str, str], int | None] = {}
    types: dict[tuple[str, str], str] = {}
    for register in registers:
        match = _PORT_REGISTER.match(register.name)
        if not match:
            continue
        key = (match.group(1), match.group(2))
        content = register.value.content
        if match.group(3) == "type" and isinstance(content, str):
            types[key] = content
        elif match.group(3) == "id":
            ids[key] = int(content[0]) if isinstance(content, list) and content else None
    result = []
    for key in sorted(ids.keys() | types.keys()):
        configured = ids.get(key)
        result.append(Port(key[0], key[1], configured, _effective(key, configured, node_id), types.get(key)))
    return result


def capabilities(
    registers: Iterable[Register],
    node_id: int | None,
    *,
    port_list: PortList | None = None,
    vendor_code: int | None = None,
    unimoc: bool = False,
) -> set[Capability]:
    """The capabilities of a node from its scan, its port list and its heartbeat."""
    registers = list(registers)
    found = {p.name: p for p in ports(registers, node_id) if p.enabled}
    result: set[Capability] = set()
    if registers or (port_list is not None and REGISTER_LIST_SERVICE in port_list.servers):
        result.add(Capability.REGISTERS)
    if port_list is not None:
        if EXECUTE_COMMAND_SERVICE in port_list.servers:
            result.add(Capability.COMMANDS)
        if DIAGNOSTIC_RECORD_SUBJECT in port_list.publishers:
            result.add(Capability.LOG)
    if "readiness" in found:
        result.add(Capability.ARMABLE)
    if any(name in found for name in SETPOINTS):
        result.add(Capability.SETPOINTS)
    if "wheel_command" in found:
        result.add(Capability.CART)
    if all(name in found for name in CYMON_SERVICES):
        result.add(Capability.SCOPE)
    if any(r.name.startswith("unimoc.sim.") for r in registers):
        result.add(Capability.SIMULATION)
    if unimoc and vendor_code is not None and VendorStatus.decode(vendor_code).local_control:
        result.add(Capability.LOCAL_CONTROL)
    return result


def setpoint_ports(registers: Iterable[Register], node_id: int | None) -> list[Port]:
    """The enabled setpoint subscriptions, in the order of :data:`SETPOINTS`."""
    found = {p.name: p for p in ports(registers, node_id) if p.kind == "sub" and p.enabled}
    return [found[name] for name in SETPOINTS if name in found]


def _effective(key: tuple[str, str], configured: int | None, node_id: int | None) -> int | None:
    if configured is None or configured == UNSET_PORT_ID:
        return None
    if key == ("sub", "readiness") and configured == READINESS_FOLLOWS_NODE_ID:
        return READINESS_BASE + node_id if node_id is not None and 0 <= node_id <= 127 else None
    return configured
