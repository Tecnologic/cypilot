# SPDX-License-Identifier: Apache-2.0
"""One connection to a Cyphal bus: the local pycyphal node and the discovery of the others.

All methods are coroutines of one asyncio loop. The CLI runs that loop directly;
the web server runs it in uvicorn's loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from cypilot import __version__
from cypilot.core.capabilities import PortList
from cypilot.core.registers import RegisterClient, RegisterError
from cypilot.core.status import Chip, Health, Mode, chips
from cypilot.core.transport import InterfaceSpec, describe, make_transport
from cypilot.dsdl import ensure_dsdl

DEFAULT_LOCAL_NODE_ID = 127
"""The node-ID of the PC tools in UNIMOC (tests, yakut setup); a second tool on the bus needs another."""

NODE_NAME = "org.tecnologic.cypilot"

UNIMOC_PROBE_REGISTER = "unimoc.settings.pending"
"""A register every UNIMOC drive has. The GetInfo name is a role label the user sets, so a UNIMOC node is
recognized by this register; its heartbeat then follows the UNIMOC vendor status layout."""


@dataclass(frozen=True)
class LogRecord:
    """A ``uavcan.diagnostic.Record`` of a node."""

    received: float
    """Local time of reception (``time.time()``)."""
    node_id: int | None
    severity: int
    """0 trace, 1 debug, 2 info, 3 notice, 4 warning, 5 error, 6 critical, 7 alert."""
    text: str


SEVERITY_NAMES = ("trace", "debug", "info", "notice", "warning", "error", "critical", "alert")


@dataclass(frozen=True)
class NodeSummary:
    """What discovery knows about one online node."""

    node_id: int
    health: Health
    mode: Mode
    vendor_code: int
    uptime: int
    name: str | None
    software_version: tuple[int, int] | None
    hardware_version: tuple[int, int] | None
    unique_id: bytes | None
    vcs_revision: int | None
    unimoc: bool | None = None
    """True for a UNIMOC node (it has :data:`UNIMOC_PROBE_REGISTER`); None until probed."""

    @property
    def is_unimoc(self) -> bool:
        return bool(self.unimoc)

    @property
    def chips(self) -> list[Chip]:
        return chips(online=True, health=self.health, vendor_code=self.vendor_code, unimoc=self.is_unimoc)


class Session:
    """``async with Session(spec) as session:`` opens the interface and starts discovery.

    Several interfaces form one redundant bus. With ``pnp_database`` the session
    also runs a plug-and-play node-ID allocator (pycyphal's centralized one,
    persisted in that SQLite file).
    """

    def __init__(
        self,
        interface: InterfaceSpec | Sequence[InterfaceSpec],
        local_node_id: int = DEFAULT_LOCAL_NODE_ID,
        *,
        pnp_database: str | None = None,
    ) -> None:
        self.interfaces = [interface] if isinstance(interface, InterfaceSpec) else list(interface)
        self.interface = self.interfaces[0]
        """The first interface (the only one without redundancy)."""
        self.local_node_id = local_node_id
        self.pnp_database = pnp_database
        self._allocator: Any = None
        self._node: Any = None
        self._tracker: Any = None
        self._changed = asyncio.Event()
        self._unimoc: dict[int, bool] = {}
        self._probes: dict[int, asyncio.Task[None]] = {}
        self._port_lists: dict[int, PortList] = {}
        self._record_handlers: list[Callable[[LogRecord], None]] = []

    async def __aenter__(self) -> Session:
        await self.open()
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        await self.close()

    async def open(self) -> None:
        ensure_dsdl()
        import pycyphal.application
        import pycyphal.application.node_tracker
        import uavcan.diagnostic
        import uavcan.node
        import uavcan.node.port

        major, minor = (int(part) for part in __version__.split(".")[:2])
        info = uavcan.node.GetInfo_1_0.Response(
            software_version=uavcan.node.Version_1_0(major=major, minor=minor), name=NODE_NAME
        )
        transport = make_transport(self.interfaces, self.local_node_id)
        registry = pycyphal.application.make_registry(environment_variables={})
        try:
            self._node = pycyphal.application.make_node(info, registry=registry, transport=transport)
        except Exception:
            transport.close()
            raise
        self._node.start()
        if self.pnp_database is not None:
            from pycyphal.application.plug_and_play import CentralizedAllocator

            self._allocator = CentralizedAllocator(self._node, self.pnp_database)
        self._tracker = pycyphal.application.node_tracker.NodeTracker(self._node)
        self._tracker.add_update_handler(self._on_update)
        port_list = self._node.make_subscriber(uavcan.node.port.List_1_0)
        port_list.receive_in_background(self._on_port_list)
        records = self._node.make_subscriber(uavcan.diagnostic.Record_1_1)
        records.receive_in_background(self._on_record)

    async def close(self) -> None:
        for task in self._probes.values():
            task.cancel()
        self._probes.clear()
        if self._node is not None:
            self._allocator = None  # closes with the node
            self._node.close()
            self._node = None
            await asyncio.sleep(0.2)  # lets the media threads of python-can stop

    def describe(self) -> str:
        """The interfaces with their bit rates, e.g. ``canable:COM5 (1000 kbit/s, ...)``."""
        return describe(self.interfaces)

    @property
    def pnp(self) -> bool:
        """True while the session runs the plug-and-play allocator."""
        return self._allocator is not None

    @property
    def node(self) -> Any:
        if self._node is None:
            raise RuntimeError("the session is not open")
        return self._node

    def nodes(self) -> list[NodeSummary]:
        """The online nodes, by node-ID; without the local node."""
        return [
            _summary(node_id, entry, self._unimoc.get(node_id))
            for node_id, entry in sorted(self._tracker.registry.items())
        ]

    def port_list(self, node_id: int) -> PortList | None:
        """The last ``uavcan.node.port.List`` of a node (nodes publish it every 10 s), or None."""
        return self._port_lists.get(node_id)

    async def wait_port_list(self, node_id: int, timeout: float = 11.0) -> PortList | None:
        """Waits up to ``timeout`` for the port list of a node; None if none came."""
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()  # before the check, so a change during it is not lost
            found = self._port_lists.get(node_id)
            if found is not None or time.monotonic() >= deadline:
                return found
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), deadline - time.monotonic())

    def add_record_handler(self, handler: Callable[[LogRecord], None]) -> None:
        """Calls ``handler`` (on the bus loop) for every log record of any node."""
        self._record_handlers.append(handler)

    async def _on_record(self, message: Any, transfer: Any) -> None:
        record = LogRecord(
            received=time.time(),
            node_id=None if transfer.source_node_id is None else int(transfer.source_node_id),
            severity=int(message.severity.value),
            text=message.text.tobytes().decode(errors="replace"),
        )
        for handler in self._record_handlers:
            handler(record)

    async def _on_port_list(self, message: Any, transfer: Any) -> None:
        if transfer.source_node_id is not None:
            self._port_lists[int(transfer.source_node_id)] = _port_list(message)
            self._changed.set()

    def _on_update(self, node_id: int, old: Any, new: Any) -> None:
        """NodeTracker callback: probes a node once its GetInfo arrived, forgets it when it left."""
        if new is None or new.info is None:
            self._unimoc.pop(node_id, None)
            self._port_lists.pop(node_id, None)
        elif old is None or old.info is None:
            if (task := self._probes.pop(node_id, None)) is not None:
                task.cancel()
            self._probes[node_id] = asyncio.get_running_loop().create_task(self._probe(node_id))
        self._changed.set()

    async def _probe(self, node_id: int) -> None:
        client = RegisterClient(self.node, node_id, timeout=1.0, retries=1)
        try:
            register = await client.read(UNIMOC_PROBE_REGISTER)
            self._unimoc[node_id] = register.value.kind != "empty"
        except RegisterError:
            self._unimoc[node_id] = False  # no register service: not a UNIMOC node
        finally:
            client.close()
            self._probes.pop(node_id, None)
            self._changed.set()

    async def discover(self, duration: float = 2.0) -> list[NodeSummary]:
        """Listens for ``duration`` seconds (a heartbeat comes every second), then waits up to as long
        again for the GetInfo answers of the nodes found."""
        await asyncio.sleep(duration)
        deadline = time.monotonic() + duration
        while True:
            self._changed.clear()  # before the check, so a change during it is not lost
            incomplete = any(n.name is None or n.unimoc is None for n in self.nodes())
            if not incomplete or time.monotonic() >= deadline:
                return self.nodes()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), deadline - time.monotonic())

    async def wait_for_node(self, node_id: int, timeout: float = 10.0) -> NodeSummary:
        """Waits until the node is online and has answered GetInfo."""
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()  # before the check, so a change during it is not lost
            for summary in self.nodes():
                if summary.node_id == node_id and summary.name is not None and summary.unimoc is not None:
                    return summary
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"node {node_id} did not show up within {timeout} s")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    def registers(self, node_id: int, timeout: float = 1.0) -> RegisterClient:
        """Register access to a node; close the client when done."""
        return RegisterClient(self.node, node_id, timeout=timeout)

    async def execute_command(
        self, node_id: int, command: int, parameter: str = "", timeout: float = 2.0
    ) -> int:
        """Sends ``uavcan.node.ExecuteCommand`` and returns the status of the answer."""
        status, _ = await call_command(self.node, node_id, command, parameter, timeout)
        return status


async def call_command(
    node: Any, node_id: int, command: int, parameter: str = "", timeout: float = 1.0
) -> tuple[int, str]:
    """``uavcan.node.ExecuteCommand``: the status and the output text of the answer."""
    import uavcan.node

    client = node.make_client(uavcan.node.ExecuteCommand_1_3, node_id)
    client.response_timeout = timeout
    try:
        result = await client.call(
            uavcan.node.ExecuteCommand_1_3.Request(command=command, parameter=parameter)
        )
    finally:
        client.close()
    if result is None:
        raise TimeoutError(f"node {node_id} did not answer ExecuteCommand {command}")
    response = result[0]
    return int(response.status), bytes(response.output.tobytes()).decode(errors="replace")


def _subjects(subject_list: Any) -> frozenset[int]:
    if subject_list.sparse_list is not None:
        return frozenset(int(item.value) for item in subject_list.sparse_list)
    if subject_list.mask is not None:
        return frozenset(index for index, bit in enumerate(subject_list.mask) if bit)
    return frozenset()  # "total": every subject, not resolved


def _port_list(message: Any) -> PortList:
    return PortList(
        publishers=_subjects(message.publishers),
        subscribers=_subjects(message.subscribers),
        servers=frozenset(index for index, bit in enumerate(message.servers.mask) if bit),
    )


def _summary(node_id: int, entry: Any, unimoc: bool | None) -> NodeSummary:
    heartbeat = entry.heartbeat
    info = entry.info
    name = software = hardware = unique_id = vcs = None
    if info is not None:
        name = info.name.tobytes().decode(errors="replace")
        software = (int(info.software_version.major), int(info.software_version.minor))
        hardware = (int(info.hardware_version.major), int(info.hardware_version.minor))
        unique_id = info.unique_id.tobytes()
        vcs = int(info.software_vcs_revision_id)
    return NodeSummary(
        node_id=node_id,
        health=Health(int(heartbeat.health.value)),
        mode=Mode(int(heartbeat.mode.value)),
        vendor_code=int(heartbeat.vendor_specific_status_code),
        uptime=int(heartbeat.uptime),
        name=name,
        software_version=software,
        hardware_version=hardware,
        unique_id=unique_id,
        vcs_revision=vcs,
        unimoc=unimoc,
    )
