# SPDX-License-Identifier: Apache-2.0
"""``cypilot-ctl``: the jobs of cypilot from the shell, on the same core as the web app."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import ipaddress
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pycyphal.transport

from cypilot import __version__
from cypilot.core import profiles
from cypilot.core.capabilities import Port, capabilities, ports
from cypilot.core.control import (
    STANDBY_HOLD,
    ControlError,
    Controller,
    DriveTarget,
    SetpointMode,
    find_target,
    recover,
)
from cypilot.core.profiles import Profile, ProfileError
from cypilot.core.registers import Register, RegisterError, parse_like
from cypilot.core.scope import TRIGGER_MODES, Capture, CaptureRequest, ScopeClient, ScopeError
from cypilot.core.session import DEFAULT_LOCAL_NODE_ID, NodeSummary, Session
from cypilot.core.signals import Recorder, SignalError, sources, subscribe
from cypilot.core.sim_control import SimControl, SimStatus, SimulationError
from cypilot.core.transport import InterfaceError, find_adapters, parse_interfaces
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
    # The media threads of python-can log every serial error with a traceback;
    # cypilot-ctl reports the failure of the command instead.
    for name in ("pycyphal", "can"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        result: int = args.handler(args)
        return result
    except (
        InterfaceError,
        DsdlError,
        ProfileError,
        ControlError,
        SimulationError,
        SignalError,
        ScopeError,
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


def app_main(argv: Sequence[str] | None = None) -> int:
    """``cypilot``: the web server on this computer, opened in the browser (``cypilot-ctl serve --open``).

    It takes the options of ``serve``; the interface comes from ``$CYPILOT_IFACE`` or the bus view.
    """
    return main(["serve", "--open", *(sys.argv[1:] if argv is None else argv)])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cypilot-ctl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"cypilot {__version__}")
    parser.add_argument(
        "-i",
        "--interface",
        action="append",
        help="interface, e.g. socketcan:vcan0, canable:COM5, slcan:/dev/pts/3, pcan:PCAN_USBBUS1, "
        "udp:127.0.0.1; repeat it for a redundant bus (default: $CYPILOT_IFACE, then $UAVCAN__CAN__IFACE)",
    )
    parser.add_argument("--mtu", type=int, help="CAN MTU: 8 (classic) or 64 (CAN FD, the default but slcan)")
    parser.add_argument("--bitrate", type=_bitrate, help="bit rates for adapters that need them: ARB[/DATA]")
    parser.add_argument(
        "--local-node-id",
        type=int,
        default=DEFAULT_LOCAL_NODE_ID,
        help=f"node-ID of cypilot on the bus (default {DEFAULT_LOCAL_NODE_ID})",
    )
    parser.add_argument(
        "--pnp",
        metavar="DB",
        help="run a plug-and-play node-ID allocator with this database file (e.g. cypilot-pnp.db)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    commands = parser.add_subparsers(dest="command", required=True)

    adapters = commands.add_parser("adapters", help="list the CAN adapters connected to this computer")
    adapters.set_defaults(handler=_cmd_adapters)

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

    run = commands.add_parser(
        "run", help="enable a drive, hold a setpoint for a time, then disable it (Ctrl+C stops it at once)"
    )
    run.add_argument("node", type=int)
    setpoint = run.add_mutually_exclusive_group()
    setpoint.add_argument("--current", type=_floats(2), metavar="I_D,I_Q", help="d/q current [A]")
    setpoint.add_argument("--torque", type=_floats(1), metavar="NM", help="torque [N m]")
    setpoint.add_argument("--speed", type=_floats(1), metavar="RAD_S", help="mechanical speed [rad/s]")
    run.add_argument("--for", dest="duration", type=float, default=2.0, help="seconds to hold (default 2)")
    run.add_argument("--ramp", type=float, default=0.5, help="seconds to ramp up and down (default 0.5)")
    run.set_defaults(handler=_cmd_run)

    stop = commands.add_parser("stop", help="SLEEP to every drive on the bus and zero setpoints, at once")
    stop.add_argument("--wait", type=float, default=1.5, help="seconds to listen for drives (default 1.5)")
    stop.set_defaults(handler=_cmd_stop)

    rescue = commands.add_parser("recover", help="clear a trip (STANDBY) or an emergency stop (--restart)")
    rescue.add_argument("node", type=int)
    rescue.add_argument("--restart", action="store_true", help="allow the restart a latched E-stop needs")
    rescue.set_defaults(handler=_cmd_recover)

    record = commands.add_parser("record", help="record published subjects of a node into CSV files")
    record.add_argument("node", type=int)
    record.add_argument("ports", nargs="*", help="port names, e.g. telemetry (default: all with a type)")
    record.add_argument("--for", dest="duration", type=float, default=2.0, help="seconds (default 2)")
    record.add_argument("--out", type=Path, required=True, help="directory for the CSV files")
    record.set_defaults(handler=_cmd_record)

    monitor = commands.add_parser("monitor", help="receive any subject with any DSDL type for a while")
    monitor.add_argument("subject", type=int, help="subject-ID")
    monitor.add_argument("type", help="DSDL type, e.g. unimoc.drive.Telemetry.0.1 or uavcan.node.Heartbeat")
    monitor.add_argument("--for", dest="duration", type=float, default=2.0, help="seconds (default 2)")
    monitor.set_defaults(handler=_cmd_monitor)

    scope = commands.add_parser("scope", help="cymon trace variables of a node, or a triggered capture")
    scope.add_argument("node", type=int)
    scope.add_argument("channels", nargs="*", type=int, help="variable indices to capture (none: list them)")
    scope.add_argument("--period", type=float, default=100.0, help="sample period [us] (default 100)")
    scope.add_argument("--samples", type=int, default=200, help="samples per channel (default 200)")
    scope.add_argument("--pretrigger", type=int, default=0, help="samples before the trigger (default 0)")
    scope.add_argument(
        "--trigger",
        choices=sorted(TRIGGER_MODES),
        default="immediate",
        help="trigger mode (default immediate)",
    )
    scope.add_argument("--source", type=int, default=0, help="trigger variable index (default 0)")
    scope.add_argument("--level", type=float, default=0.0, help="trigger level (default 0)")
    scope.add_argument("--band", type=float, default=0.0, help="hysteresis band (default 0)")
    scope.add_argument(
        "--timeout", type=float, default=30.0, help="seconds to wait for the trigger (default 30)"
    )
    scope.add_argument("--out", type=Path, help="CSV file of the capture (time column first)")
    scope.set_defaults(handler=_cmd_scope)

    sim = commands.add_parser("sim", help="pause, run, advance or reset a simulated UNIMOC drive")
    sim.add_argument("node", type=int)
    sim.add_argument("action", choices=["status", "pause", "run", "advance", "reset"])
    sim.add_argument("cycles", type=int, nargs="?", default=0, help="control cycles for advance")
    sim.set_defaults(handler=_cmd_sim)

    serve = commands.add_parser("serve", help="run the web server: REST and WebSocket API and the web UI")
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on; 0.0.0.0 for the network")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--pin",
        default=os.environ.get("CYPILOT_PIN") or None,
        help="PIN for the login (default $CYPILOT_PIN); generated when listening beyond this computer",
    )
    serve.add_argument(
        "--lease", type=float, default=0.5, help="control lease in seconds, 0.3 .. 1.0 (default 0.5)"
    )
    serve.add_argument("--demo", type=int, default=0, metavar="N", help="start with N demo devices")
    serve.add_argument(
        "--runs",
        type=Path,
        metavar="DIR",
        help="where runs are recorded (default: $CYPILOT_RUNS or the user data dir)",
    )
    serve.add_argument("--runs-warn", type=int, default=1024, metavar="MB", help="warn below this free space")
    serve.add_argument(
        "--runs-stop", type=int, default=200, metavar="MB", help="a recording run stops below this free space"
    )
    serve.add_argument("--open", action="store_true", help="open the web UI in the browser")
    serve.set_defaults(handler=_cmd_serve)

    dsdl = commands.add_parser("dsdl", help="download and compile the DSDL now, print where it is")
    dsdl.set_defaults(handler=_cmd_dsdl)
    return parser


def _floats(count: int) -> Any:
    def parse(text: str) -> tuple[float, ...]:
        values = tuple(float(item) for item in text.split(","))
        if len(values) != count:
            raise argparse.ArgumentTypeError(f"expected {count} comma-separated value(s)")
        return values

    return parse


def _bitrate(text: str) -> tuple[int, int]:
    arbitration, _, data = text.partition("/")
    return int(arbitration), int(data or arbitration)


def _session(args: argparse.Namespace) -> Session:
    texts = args.interface or [
        text for text in (os.environ.get("CYPILOT_IFACE") or os.environ.get("UAVCAN__CAN__IFACE"),) if text
    ]
    if not texts:
        raise InterfaceError("no interface: pass -i/--interface or set CYPILOT_IFACE")
    specs = parse_interfaces(texts, mtu=args.mtu, bitrate=args.bitrate)
    return Session(specs, local_node_id=args.local_node_id, pnp_database=args.pnp)


def _cmd_adapters(args: argparse.Namespace) -> int:
    found = find_adapters()
    lines = [f"{a.interface:28} {a.description}" for a in found] or ["no CAN adapter found"]
    _print(args, [vars(a) for a in found], "\n".join(lines))
    return 0


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


def _cmd_run(args: argparse.Namespace) -> int:
    chosen = [
        (mode, values)
        for mode, values in (
            (SetpointMode.CURRENT, args.current),
            (SetpointMode.TORQUE, args.torque),
            (SetpointMode.SPEED, args.speed),
        )
        if values is not None
    ]

    async def run() -> dict[str, Any]:
        async with _session(args) as session:
            await session.wait_for_node(args.node)
            target = await find_target(session, args.node)
            controller = Controller(session)
            feeder = asyncio.get_running_loop().create_task(_feed(controller))  # this process is the owner
            try:
                await controller.engage(target)
                await controller.wait_engaged(args.node)
                for mode, values in chosen:
                    controller.set_setpoint(target, mode, values, ramp=args.ramp)
                await asyncio.sleep(args.duration)
                await controller.disengage(args.node, ramp=args.ramp)
                await asyncio.sleep(STANDBY_HOLD)
                node = next(n for n in session.nodes() if n.node_id == args.node)
                return {
                    "node": args.node,
                    "readiness": target.readiness,
                    "state": [c.value for c in node.chips],
                }
            finally:
                feeder.cancel()
                await controller.close()

    data = asyncio.run(run())
    _print(
        args,
        data,
        f"node {data['node']} ran on readiness {data['readiness']}; now: {', '.join(data['state'])}",
    )
    return 0


async def _feed(controller: Controller) -> None:
    while True:
        controller.feed()
        await asyncio.sleep(0.05)


def _cmd_stop(args: argparse.Namespace) -> int:
    async def run() -> list[int]:
        async with _session(args) as session:
            targets: list[DriveTarget] = []
            for node in await session.discover(args.wait):
                if node.is_unimoc:
                    try:
                        targets.append(await find_target(session, node.node_id))
                    except (ControlError, RegisterError):
                        continue
            controller = Controller(session)
            await controller.stop_all(targets)
            await controller.close()
            return [t.node_id for t in targets]

    stopped = asyncio.run(run())
    _print(args, {"sleep_sent_to": stopped}, "SLEEP sent to " + (", ".join(map(str, stopped)) or "no drive"))
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    async def run() -> Any:
        async with _session(args) as session:
            await session.wait_for_node(args.node)
            controller = Controller(session)
            try:
                return await recover(session, controller, args.node, allow_restart=args.restart)
            finally:
                await controller.close()

    result = asyncio.run(run())
    _print(args, {"steps": result.steps, "ready": result.ready}, "\n".join(result.steps))
    return 0 if result.ready else 1


def _cmd_record(args: argparse.Namespace) -> int:
    async def run() -> Recorder:
        async with _session(args) as session:
            _, registers = await _scan(session, args.node)
            found = sources(registers, args.node)
            if args.ports:
                unknown = set(args.ports) - {s.port for s in found}
                if unknown:
                    raise SignalError(f"node {args.node} publishes no decodable {', '.join(sorted(unknown))}")
                found = [s for s in found if s.port in args.ports]
            recorder = Recorder()
            subscribers = [subscribe(session.node, source, recorder) for source in found]
            try:
                await asyncio.sleep(args.duration)
            finally:
                for subscriber in subscribers:
                    subscriber.close()
            return recorder

    recorder = asyncio.run(run())
    recorder.export(args.out)
    counts = {key: recorder.count(key) for key in recorder.source_keys()}
    lines = [f"{key}: {count} samples" for key, count in counts.items()] or ["nothing received"]
    _print(args, {"out": str(args.out), "samples": counts}, "\n".join(lines))
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    from cypilot.core.events import EventHub
    from cypilot.core.subjects import SubjectMonitor

    async def run() -> tuple[str, list[dict[str, Any]]]:
        async with _session(args) as session:
            events = EventHub()
            monitor = SubjectMonitor(session.node, events)
            batches: list[dict[str, Any]] = []
            try:
                async with events.listen() as queue:
                    subscription = monitor.subscribe(args.subject, args.type)
                    deadline = asyncio.get_running_loop().time() + args.duration
                    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                        try:
                            event = await asyncio.wait_for(queue.get(), remaining)
                        except TimeoutError:
                            break
                        if event["kind"] == "subject":
                            batches.append(event)
            finally:
                monitor.close()
            return str(subscription["type"]), batches

    data_type, batches = asyncio.run(run())
    count = sum(len(batch["t"]) for batch in batches)
    sources = sorted({source for batch in batches for source in batch["source"] if source is not None})
    last = batches[-1]["last"] if batches else None
    result = {"subject": args.subject, "type": data_type, "messages": count, "sources": sources, "last": last}
    text = f"{args.subject}:{data_type}: {count} messages from {sources or 'no node'}"
    if last is not None:
        text += f"\nlast: {json.dumps(last)}"
    _print(args, result, text)
    return 0 if count else 1


def _cmd_scope(args: argparse.Namespace) -> int:
    async def run() -> dict[str, Any] | Capture:
        async with _session(args) as session:
            await session.wait_for_node(args.node)
            scope = ScopeClient(session)
            try:
                if not args.channels:
                    return await scope.info(args.node)
                request = CaptureRequest(
                    channels=args.channels,
                    sample_period_us=args.period,
                    num_samples=args.samples,
                    pretrigger_samples=args.pretrigger,
                    trigger_mode=args.trigger,
                    trigger_source=args.source,
                    trigger_level=args.level,
                    hysteresis_band=args.band,
                    timeout_s=args.timeout,
                )
                return await scope.capture(args.node, request)
            finally:
                scope.close()

    result = asyncio.run(run())
    if isinstance(result, dict):
        lines = [f"{v['id']:3} {v['name']} [{v['unit']}]" for v in result["variables"]]
        buffer = result["buffer"]
        lines.append(
            f"buffer {buffer['buffer_bytes']} bytes, up to {buffer['max_channels']} channels, "
            f"tick {buffer['tick_period_us']:g} us"
        )
        _print(args, result, "\n".join(lines))
        return 0
    if args.out is not None:
        import csv

        with args.out.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time", *(f"var{channel}" for channel in args.channels)])
            for index, stamp in enumerate(result.t):
                writer.writerow([f"{stamp:.9f}", *(repr(values[index]) for values in result.channels)])
    summary = {
        "node_id": result.node_id,
        "sample_period_us": result.sample_period_us,
        "samples": len(result.t),
        "pretrigger_samples": result.pretrigger_samples,
        "out": str(args.out) if args.out else None,
    }
    text = f"{len(result.t)} samples at {result.sample_period_us:g} us" + (
        f", written to {args.out}" if args.out else ""
    )
    _print(args, summary, text)
    return 0


def _cmd_sim(args: argparse.Namespace) -> int:
    if args.action == "advance" and args.cycles <= 0:
        raise ValueError("advance needs a positive number of control cycles")

    async def run() -> SimStatus:
        async with _session(args) as session:
            await session.wait_for_node(args.node)
            control = SimControl(session.node, args.node)
            try:
                if args.action == "pause":
                    await control.set_paused(True)
                elif args.action == "run":
                    await control.set_paused(False)
                elif args.action == "advance":
                    return await control.advance(args.cycles)
                elif args.action == "reset":
                    await control.reset()
                return await control.status()
            finally:
                control.close()

    status = asyncio.run(run())
    data = {
        "paused": status.paused,
        "cycle_count": status.cycle_count,
        "cycle_period_ns": status.cycle_period_ns,
        "virtual_time": status.virtual_time,
    }
    text = (
        f"{'paused' if status.paused else 'running'}, cycle {status.cycle_count}, {status.virtual_time:.4f} s"
    )
    _print(args, data, text)
    return 0


def _cmd_dsdl(args: argparse.Namespace) -> int:
    source = DsdlSource.from_environment()
    path = ensure_dsdl(source)
    origin = str(source.unimoc_path) if source.unimoc_path else f"unimoc {source.unimoc_ref}"
    _print(args, {"source": origin, "compiled": str(path)}, f"{origin}\n{path}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import contextlib
    import secrets
    import webbrowser
    from collections.abc import AsyncIterator

    import uvicorn
    from fastapi import FastAPI

    from cypilot.core.hub import BusConfig, BusHub
    from cypilot.core.runs import RunLimits, RunStore
    from cypilot.web.app import create_app

    texts = args.interface or [
        text for text in (os.environ.get("CYPILOT_IFACE") or os.environ.get("UAVCAN__CAN__IFACE"),) if text
    ]
    config: BusConfig | None = None
    if args.demo or texts:
        config = BusConfig(
            interfaces=texts,
            local_node_id=args.local_node_id,
            mtu=args.mtu,
            bitrate=args.bitrate,
            pnp=args.pnp,
            demo_devices=args.demo,
        )
    pin = args.pin
    if pin is None and not _loopback(args.host):
        pin = f"{secrets.randbelow(10**6):06d}"
    ensure_dsdl()
    limits = RunLimits(warn_free=args.runs_warn << 20, stop_free=args.runs_stop << 20)
    hub = BusHub(lease_time=args.lease, runs=RunStore(args.runs, limits))
    app = create_app(hub, pin=pin)
    url = f"http://{'localhost' if _loopback(args.host) else args.host}:{args.port}/"

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if config is not None:
            try:
                await hub.connect(config)
            except Exception as error:  # the server runs; connect again from the UI
                print(f"cypilot-ctl: cannot connect: {error}", file=sys.stderr)
        print(f"cypilot on {url}" + (f"  PIN {pin}" if pin else ""), flush=True)
        assert hub.runs is not None
        print(f"runs in {hub.runs.root}", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            yield
        finally:
            await hub.close()  # SLEEP to every engaged drive

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


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
