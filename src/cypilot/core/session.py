# SPDX-License-Identifier: Apache-2.0
"""One connection to a Cyphal bus: the local pycyphal node and the discovery of the others.

All methods are coroutines of one asyncio loop. The CLI runs that loop directly;
the GUI will run it in its bus thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from cypilot import __version__
from cypilot.core.registers import RegisterClient, RegisterError
from cypilot.core.status import Chip, Health, Mode, chips
from cypilot.core.transport import InterfaceSpec, make_transport
from cypilot.dsdl import ensure_dsdl

DEFAULT_LOCAL_NODE_ID = 127
"""The node-ID of the PC tools in UNIMOC (tests, yakut setup); a second tool on the bus needs another."""

NODE_NAME = "org.tecnologic.cypilot"

UNIMOC_PROBE_REGISTER = "unimoc.settings.pending"
"""A register every UNIMOC drive has. The GetInfo name is a role label the user sets, so a UNIMOC node is
recognized by this register; its heartbeat then follows the UNIMOC vendor status layout."""


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
    """``async with Session(spec) as session:`` opens the interface and starts discovery."""

    def __init__(self, interface: InterfaceSpec, local_node_id: int = DEFAULT_LOCAL_NODE_ID) -> None:
        self.interface = interface
        self.local_node_id = local_node_id
        self._node: Any = None
        self._tracker: Any = None
        self._changed = asyncio.Event()
        self._unimoc: dict[int, bool] = {}
        self._probes: dict[int, asyncio.Task[None]] = {}

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
        import uavcan.node

        major, minor = (int(part) for part in __version__.split(".")[:2])
        info = uavcan.node.GetInfo_1_0.Response(
            software_version=uavcan.node.Version_1_0(major=major, minor=minor), name=NODE_NAME
        )
        transport = make_transport(self.interface, self.local_node_id)
        registry = pycyphal.application.make_registry(environment_variables={})
        self._node = pycyphal.application.make_node(info, registry=registry, transport=transport)
        self._node.start()
        self._tracker = pycyphal.application.node_tracker.NodeTracker(self._node)
        self._tracker.add_update_handler(self._on_update)

    async def close(self) -> None:
        for task in self._probes.values():
            task.cancel()
        self._probes.clear()
        if self._node is not None:
            self._node.close()
            self._node = None
            await asyncio.sleep(0.2)  # lets the media threads of python-can stop

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

    def _on_update(self, node_id: int, old: Any, new: Any) -> None:
        """NodeTracker callback: probes a node once its GetInfo arrived, forgets it when it left."""
        if new is None or new.info is None:
            self._unimoc.pop(node_id, None)
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
        while time.monotonic() < deadline and any(n.name is None or n.unimoc is None for n in self.nodes()):
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), deadline - time.monotonic())
            except TimeoutError:
                break
        return self.nodes()

    async def wait_for_node(self, node_id: int, timeout: float = 10.0) -> NodeSummary:
        """Waits until the node is online and has answered GetInfo."""
        deadline = time.monotonic() + timeout
        while True:
            for summary in self.nodes():
                if summary.node_id == node_id and summary.name is not None and summary.unimoc is not None:
                    return summary
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"node {node_id} did not show up within {timeout} s")
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    def registers(self, node_id: int, timeout: float = 1.0) -> RegisterClient:
        """Register access to a node; close the client when done."""
        return RegisterClient(self.node, node_id, timeout=timeout)

    async def execute_command(
        self, node_id: int, command: int, parameter: str = "", timeout: float = 2.0
    ) -> int:
        """Sends ``uavcan.node.ExecuteCommand`` and returns the status of the answer."""
        import uavcan.node

        client = self.node.make_client(uavcan.node.ExecuteCommand_1_3, node_id)
        client.response_timeout = timeout
        try:
            result = await client.call(
                uavcan.node.ExecuteCommand_1_3.Request(command=command, parameter=parameter)
            )
        finally:
            client.close()
        if result is None:
            raise TimeoutError(f"node {node_id} did not answer ExecuteCommand {command}")
        return int(result[0].status)


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
