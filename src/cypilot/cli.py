# SPDX-License-Identifier: Apache-2.0
"""``cypilot-ctl``: the jobs of cypilot from the shell, on the same core as the GUI."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pycyphal.transport

from cypilot import __version__
from cypilot.core import profiles
from cypilot.core.capabilities import Port, capabilities, ports
from cypilot.core.profiles import Profile, ProfileError
from cypilot.core.registers import Register, RegisterError, parse_like
from cypilot.core.session import DEFAULT_LOCAL_NODE_ID, NodeSummary, Session
from cypilot.core.transport import InterfaceError, parse_interface
from cypilot.dsdl import DsdlError, DsdlSource, ensure_dsdl

COMMANDS = {
    "restart": 65535,
    "factory-reset": 65532,
    "emergency-stop": 65531,
    "store": 65530,
}
"""Standard ``uavcan.node.ExecuteCommand`` codes as UNIMOC uses them (UNIMOC README, "Commands")."""

COMMAND_STATUS = {
    0: "success",
    1: "failure",
    2: "not authorized",
    3: "bad command",
    4: "bad parameter",
    5: "bad state",
    6: "internal error",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.handler(args)
        return result
    except (
        InterfaceError,
        DsdlError,
        ProfileError,
        RegisterError,
        TimeoutError,
        ValueError,
        OSError,
        pycyphal.transport.TransportError,
    ) as error:
        print(f"cypilot-ctl: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cypilot-ctl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"cypilot {__version__}")
    parser.add_argument(
        "-i",
        "--interface",
        default=os.environ.get("CYPILOT_IFACE") or os.environ.get("UAVCAN__CAN__IFACE"),
        help="CAN interface, e.g. socketcan:vcan0, slcan:/dev/pts/3, slcan:COM5, pcan:PCAN_USBBUS1 "
        "(default: $CYPILOT_IFACE, then $UAVCAN__CAN__IFACE)",
    )
    parser.add_argument("--mtu", type=int, help="CAN MTU: 8 (classic) or 64 (CAN FD, the default but slcan)")
    parser.add_argument("--bitrate", type=_bitrate, help="bit rates for adapters that need them: ARB[/DATA]")
    parser.add_argument(
        "--local-node-id",
        type=int,
        default=DEFAULT_LOCAL_NODE_ID,
        help=f"node-ID of cypilot on the bus (default {DEFAULT_LOCAL_NODE_ID})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    commands = parser.add_subparsers(dest="command", required=True)

    nodes = commands.add_parser("nodes", help="list the nodes of the bus with their state")
    nodes.add_argument("--wait", type=float, default=2.0, help="seconds to listen (default 2)")
    nodes.set_defaults(handler=_cmd_nodes)

    regs = commands.add_parser("regs", help="scan and print all registers of a node")
    regs.add_argument("node", type=int)
    regs.add_argument("pattern", nargs="?", default="*", help="name filter, shell style (default *)")
    regs.set_defaults(handler=_cmd_regs)

    get = commands.add_parser("get", help="read one register")
    get.add_argument("node", type=int)
    get.add_argument("name")
    get.set_defaults(handler=_cmd_get)

    put = commands.add_parser("set", help="write one register in its own type and check the answer")
    put.add_argument("node", type=int)
    put.add_argument("name")
    put.add_argument("value", help="text in the register's type; arrays comma separated")
    put.set_defaults(handler=_cmd_set)

    command = commands.add_parser("command", help="send an ExecuteCommand")
    command.add_argument("node", type=int)
    command.add_argument("command", choices=sorted(COMMANDS))
    command.set_defaults(handler=_cmd_command)

    caps = commands.add_parser("caps", help="capabilities and ports of a node, found by discovery")
    caps.add_argument("node", type=int)
    caps.add_argument(
        "--port-list-wait",
        type=float,
        default=11.0,
        help="seconds to wait for the node's port list (published every 10 s; default 11, 0: skip)",
    )
    caps.set_defaults(handler=_cmd_caps)

    profile = commands.add_parser("profile", help="save, compare and load the settings of a node")
    actions = profile.add_subparsers(dest="action", required=True)
    save = actions.add_parser("save", help="write the persistent registers of a node to a JSON file")
    save.add_argument("node", type=int)
    save.add_argument("file", type=Path)
    save.set_defaults(handler=_cmd_profile_save)
    compare = actions.add_parser("diff", help="compare a node with a profile (exit code 1 on differences)")
    compare.add_argument("node", type=int)
    compare.add_argument("file", type=Path)
    compare.set_defaults(handler=_cmd_profile_diff)
    load = actions.add_parser("load", help="write a profile into a node, verify it and optionally store it")
    load.add_argument("node", type=int)
    load.add_argument("file", type=Path)
    load.add_argument("--include-ports", action="store_true", help="also write the port-ID registers")
    load.add_argument(
        "--include-identity",
        action="store_true",
        help="also write uavcan.node.id and uavcan.node.description",
    )
    load.add_argument("--dry-run", action="store_true", help="only show what would be written")
    load.add_argument("--store", action="store_true", help="store the settings in flash when all were taken")
    load.set_defaults(handler=_cmd_profile_load)

    dsdl = commands.add_parser("dsdl", help="download and compile the DSDL now, print where it is")
    dsdl.set_defaults(handler=_cmd_dsdl)
    return parser


def _bitrate(text: str) -> tuple[int, int]:
    arbitration, _, data = text.partition("/")
    return int(arbitration), int(data or arbitration)


def _session(args: argparse.Namespace) -> Session:
    if not args.interface:
        raise InterfaceError("no interface: pass -i/--interface or set CYPILOT_IFACE")
    spec = parse_interface(args.interface, mtu=args.mtu, bitrate=args.bitrate)
    return Session(spec, local_node_id=args.local_node_id)


def _print(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, indent=2) if args.json else text)


# --- Commands ------------------------------------------------------------------


def _cmd_nodes(args: argparse.Namespace) -> int:
    async def run() -> list[NodeSummary]:
        async with _session(args) as session:
            return await session.discover(args.wait)

    nodes = asyncio.run(run())
    data = [_node_json(node) for node in nodes]
    rows: list[tuple[str, ...]] = [("ID", "NAME", "SW", "HEALTH", "MODE", "UPTIME", "STATE")]
    for node in nodes:
        version = f"{node.software_version[0]}.{node.software_version[1]}" if node.software_version else "?"
        rows.append(
            (
                str(node.node_id),
                node.name or "?",
                version,
                node.health.name,
                node.mode.name,
                f"{node.uptime} s",
                ", ".join(chip.value for chip in node.chips) or "-",
            )
        )
    _print(args, data, _table(rows) if nodes else "no nodes")
    return 0


def _cmd_regs(args: argparse.Namespace) -> int:
    async def run() -> list[Register]:
        async with _session(args) as session:
            await session.wait_for_node(args.node)
            client = session.registers(args.node)
            try:
                names = [name for name in await client.names() if fnmatch.fnmatchcase(name, args.pattern)]
                return await client.scan(names)
            finally:
                client.close()

    registers = asyncio.run(run())
    rows: list[tuple[str, ...]] = [("NAME", "VALUE", "TYPE", "FLAGS")]
    rows += [(r.name, r.value.format(), _type(r), _flags(r)) for r in registers]
    _print(args, [_register_json(r) for r in registers], _table(rows))
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    async def run() -> Register:
        async with _session(args) as session:
            client = session.registers(args.node)
            try:
                return await client.read(args.name)
            finally:
                client.close()

    register = asyncio.run(run())
    if register.value.kind == "empty":
        print(f"cypilot-ctl: node {args.node} has no register {args.name}", file=sys.stderr)
        return 1
    _print(args, _register_json(register), register.value.format())
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    async def run() -> tuple[bool, Register]:
        async with _session(args) as session:
            client = session.registers(args.node)
            try:
                current = await client.read(args.name)
                if current.value.kind == "empty":
                    raise ValueError(f"node {args.node} has no register {args.name}")
                if not current.mutable:
                    raise ValueError(f"{args.name} is read-only")
                result = await client.write(args.name, parse_like(args.value, current.value))
                return result.accepted, result.register
            finally:
                client.close()

    accepted, register = asyncio.run(run())
    data = {**_register_json(register), "accepted": accepted}
    if accepted:
        _print(args, data, register.value.format())
        return 0
    _print(args, data, f"rejected, the value stays {register.value.format()}")
    return 1


def _cmd_command(args: argparse.Namespace) -> int:
    async def run() -> int:
        async with _session(args) as session:
            return await session.execute_command(args.node, COMMANDS[args.command])

    status = asyncio.run(run())
    text = COMMAND_STATUS.get(status, f"status {status}")
    _print(args, {"command": args.command, "status": status, "text": text}, text)
    return 0 if status == 0 else 1


async def _scan(session: Session, node_id: int) -> tuple[NodeSummary, list[Register]]:
    summary = await session.wait_for_node(node_id)
    client = session.registers(node_id)
    try:
        return summary, await client.scan()
    finally:
        client.close()


def _cmd_caps(args: argparse.Namespace) -> int:
    async def run() -> tuple[NodeSummary, list[Register], Any]:
        async with _session(args) as session:
            summary, registers = await _scan(session, args.node)
            port_list = session.port_list(args.node)
            if port_list is None and args.port_list_wait > 0:
                port_list = await session.wait_port_list(args.node, args.port_list_wait)
            return summary, registers, port_list

    summary, registers, port_list = asyncio.run(run())
    found = sorted(
        c.value
        for c in capabilities(
            registers,
            args.node,
            port_list=port_list,
            vendor_code=summary.vendor_code,
            unimoc=summary.is_unimoc,
        )
    )
    node_ports = ports(registers, args.node)
    rows: list[tuple[str, ...]] = [("PORT", "KIND", "ID", "TYPE")]
    rows += [(p.name, p.kind, _port_text(p), p.type_name or "?") for p in node_ports]
    text = "capabilities: " + (", ".join(found) or "-")
    if port_list is None:
        text += "  (no port list received)"
    data = {
        "capabilities": found,
        "port_list": port_list is not None,
        "ports": [
            {
                "kind": p.kind,
                "name": p.name,
                "configured": p.configured,
                "port_id": p.port_id,
                "type": p.type_name,
            }
            for p in node_ports
        ],
    }
    _print(args, data, text + "\n" + _table(rows))
    return 0


def _cmd_profile_save(args: argparse.Namespace) -> int:
    async def run() -> tuple[NodeSummary, list[Register]]:
        async with _session(args) as session:
            return await _scan(session, args.node)

    summary, registers = asyncio.run(run())
    profile = Profile.from_registers(
        registers,
        node_name=summary.name,
        software_version=summary.software_version,
        vcs_revision=summary.vcs_revision,
    )
    args.file.write_text(profile.to_json())
    _print(
        args,
        {"file": str(args.file), "registers": len(profile.registers)},
        f"{len(profile.registers)} registers saved to {args.file}",
    )
    return 0


def _cmd_profile_diff(args: argparse.Namespace) -> int:
    profile = Profile.from_json(args.file.read_text())

    async def run() -> list[Register]:
        async with _session(args) as session:
            return (await _scan(session, args.node))[1]

    differences = profiles.diff(profile, asyncio.run(run()))
    _print(args, [_difference_json(d) for d in differences], _differences_text(differences))
    return 1 if differences else 0


def _cmd_profile_load(args: argparse.Namespace) -> int:
    profile = Profile.from_json(args.file.read_text())
    options = {"include_ports": args.include_ports, "include_identity": args.include_identity}

    async def run() -> dict[str, Any]:
        async with _session(args) as session:
            _, registers = await _scan(session, args.node)
            if args.dry_run:
                planned = [
                    d
                    for d in profiles.diff(profile, registers)
                    if d.kind == "changed" and not profiles.skipped(d.name, **options)
                ]
                return {
                    "dry_run": True,
                    "would_write": {d.name: d.profile.format() for d in planned if d.profile},
                }
            client = session.registers(args.node)
            try:
                result = await profiles.load(profile, registers, client, **options)
                remaining = [
                    d.name
                    for d in profiles.diff(profile, await client.scan())
                    if d.kind != "extra" and not profiles.skipped(d.name, **options)
                ]
            finally:
                client.close()
            stored = None
            if args.store and result.complete and not remaining:
                stored = await session.execute_command(args.node, COMMANDS["store"])
            return {
                "written": result.written,
                "rejected": {name: value.format() for name, value in result.rejected.items()},
                "skipped": result.skipped,
                "missing": result.missing,
                "incompatible": result.incompatible,
                "still_different": remaining,
                "store_status": stored,
            }

    data = asyncio.run(run())
    if data.get("dry_run"):
        lines = [f"{name} = {value}" for name, value in data["would_write"].items()]
        _print(args, data, "\n".join(lines) or "nothing to write")
        return 0
    lines = [f"written: {len(data['written'])}, skipped: {len(data['skipped'])}"]
    lines += [f"rejected: {name} stays {value}" for name, value in data["rejected"].items()]
    lines += [f"not on the node: {name}" for name in data["missing"]]
    lines += [f"other type on the node: {name}" for name in data["incompatible"]]
    lines += [f"still different: {name}" for name in data["still_different"]]
    if data["store_status"] is not None:
        lines.append("store: " + COMMAND_STATUS.get(data["store_status"], str(data["store_status"])))
    _print(args, data, "\n".join(lines))
    ok = not data["rejected"] and not data["incompatible"] and not data["still_different"]
    return 0 if ok and data["store_status"] in (None, 0) else 1


def _cmd_dsdl(args: argparse.Namespace) -> int:
    source = DsdlSource.from_environment()
    path = ensure_dsdl(source)
    origin = str(source.unimoc_path) if source.unimoc_path else f"unimoc {source.unimoc_ref}"
    _print(args, {"source": origin, "compiled": str(path)}, f"{origin}\n{path}")
    return 0


# --- Output --------------------------------------------------------------------


def _table(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip() for row in rows
    )


def _port_text(port: Port) -> str:
    if port.port_id is None:
        return "off" if port.configured is not None else "?"
    if port.configured != port.port_id:
        return f"{port.port_id} (node-ID)"
    return str(port.port_id)


def _difference_json(difference: profiles.Difference) -> dict[str, Any]:
    return {
        "name": difference.name,
        "kind": difference.kind,
        "profile": difference.profile.format() if difference.profile else None,
        "node": difference.node.format() if difference.node else None,
        "skipped_on_load": profiles.skipped(difference.name),
    }


def _differences_text(differences: Sequence[profiles.Difference]) -> str:
    if not differences:
        return "no differences"
    rows: list[tuple[str, ...]] = [("NAME", "PROFILE", "NODE", "")]
    for d in differences:
        note = d.kind if d.kind != "changed" else ""
        if profiles.skipped(d.name):
            note = (note + " skipped on load").strip()
        rows.append(
            (d.name, d.profile.format() if d.profile else "-", d.node.format() if d.node else "-", note)
        )
    return _table(rows)


def _type(register: Register) -> str:
    content = register.value.content
    return f"{register.value.kind}[{len(content)}]" if isinstance(content, list) else register.value.kind


def _flags(register: Register) -> str:
    return ("m" if register.mutable else "-") + ("p" if register.persistent else "-")


def _node_json(node: NodeSummary) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "name": node.name,
        "health": node.health.name,
        "mode": node.mode.name,
        "uptime": node.uptime,
        "vendor_code": node.vendor_code,
        "software_version": list(node.software_version) if node.software_version else None,
        "vcs_revision": f"{node.vcs_revision:x}" if node.vcs_revision is not None else None,
        "unique_id": node.unique_id.hex() if node.unique_id is not None else None,
        "unimoc": node.unimoc,
        "state": [chip.value for chip in node.chips],
    }


def _register_json(register: Register) -> dict[str, Any]:
    content = register.value.content
    return {
        "name": register.name,
        "kind": register.value.kind,
        "value": content.hex() if isinstance(content, bytes) else content,
        "mutable": register.mutable,
        "persistent": register.persistent,
    }


if __name__ == "__main__":
    sys.exit(main())
