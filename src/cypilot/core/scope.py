# SPDX-License-Identifier: Apache-2.0
"""Triggered captures of the cymon trace protocol (cymon-lib on the node), from the cymon tool.

The cymon services have no fixed port-IDs: :meth:`ScopeClient.discover`
reads the node's port registers and takes the ``uavcan.srv.*.id`` of each
service whose ``.type`` is ``cymon.<Service>.0.1`` (``docs/plan.md``,
section 8.2). A capture is set up, armed, polled until complete and read out
page by page; the time axis is relative to the trigger sample.

**Several nodes on one trigger** (:meth:`ScopeClient.multi_capture`): a node
publishes ``cymon.TriggerEvent`` when its capture triggers, and an armed node
that subscribes to it triggers at its next sample. With the trigger ports
(``uavcan.pub.cymon_trigger.id``, ``uavcan.sub.cymon_trigger.id``) on one
subject on every node, the other nodes are armed on a condition that never
holds, then the trigger node on the user's condition. Their trigger samples are
late by the time the event takes over the bus (about 1 to 2 ms on UNIMOC).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass
from typing import Any

from cypilot.core.capabilities import ports
from cypilot.core.events import EventHub
from cypilot.core.registers import RegisterError

SERVICES = {
    "variables": "cymon.GetVariableList.0.1",
    "buffer_info": "cymon.GetBufferInfo.0.1",
    "setup": "cymon.SetupCapture.0.1",
    "arm": "cymon.ArmTrigger.0.1",
    "read": "cymon.ReadSamples.0.1",
}
"""The service types of the protocol by the key used here."""

TRIGGER_MODES = {"rising": 0, "falling": 1, "hysteresis": 2, "immediate": 3}
STATES = {0: "idle", 1: "configured", 2: "armed", 3: "triggered", 4: "complete"}
ERRORS = {
    0: "none",
    1: "bad channel id",
    2: "buffer too small",
    3: "not configured",
    5: "already capturing",
    9: "bad config",
}
_COMPLETE = 4
TRIGGER_PORTS = ("uavcan.pub.cymon_trigger.id", "uavcan.sub.cymon_trigger.id")
DEFAULT_TRIGGER_SUBJECT = 220
"""The subject of the shared trigger that cypilot offers to set (free on UNIMOC)."""
_NEVER = 3.0e38
"""A level no variable reaches: the followers fire only on the trigger event."""
_PAGE = 255
"""Frames asked for per ReadSamples request; the node answers with as many as fit."""


class ScopeError(RuntimeError):
    """A node has no cymon services, refused a capture or did not answer."""


@dataclass(frozen=True)
class CaptureRequest:
    channels: list[int]
    sample_period_us: float
    num_samples: int
    pretrigger_samples: int = 0
    trigger_mode: str = "immediate"
    trigger_source: int = 0
    trigger_level: float = 0.0
    hysteresis_band: float = 0.0
    timeout_s: float = 30.0


@dataclass(frozen=True)
class Capture:
    node_id: int
    sample_period_us: float
    pretrigger_samples: int
    t: list[float]
    """Seconds from the trigger sample (negative before it)."""
    channels: list[list[float]]
    """One list of samples per requested channel, in request order."""


class ScopeClient:
    """Finds the cymon services of nodes and runs captures on them, on one open session."""

    def __init__(self, session: Any, events: EventHub | None = None) -> None:
        self._session = session
        self._events = events or EventHub()
        self._service_ids: dict[int, dict[str, int]] = {}
        self._clients: dict[tuple[int, str], Any] = {}
        self._periods: dict[int, float] = {}

    async def discover(self, node_id: int, refresh: bool = False) -> dict[str, int]:
        """The service-IDs of the cymon services by key; empty when the node has none."""
        if refresh or node_id not in self._service_ids:
            client = self._session.registers(node_id)
            try:
                names = [name for name in await client.names() if name.startswith("uavcan.srv.")]
                registers = await client.scan(names)
            except RegisterError as error:
                raise ScopeError(f"cannot read the port registers of node {node_id}: {error}") from error
            finally:
                client.close()
            by_type = {
                port.type_name: port.port_id
                for port in ports(registers, node_id)
                if port.kind == "srv" and port.port_id is not None
            }
            self.forget(node_id)
            self._service_ids[node_id] = {
                key: by_type[name] for key, name in SERVICES.items() if name in by_type
            }
        return self._service_ids[node_id]

    def forget(self, node_id: int) -> None:
        """Drops what is known of a node (after its restart or a change of its ports)."""
        self._service_ids.pop(node_id, None)
        for key in [key for key in self._clients if key[0] == node_id]:
            self._clients.pop(key).close()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    async def _call(
        self, node_id: int, key: str, request: Any, timeout: float = 0.5, attempts: int = 3
    ) -> Any:
        import cymon  # the DSDL namespace

        ids = await self.discover(node_id)
        missing = [name for name in SERVICES if name not in ids]
        if missing:
            raise ScopeError(f"node {node_id} has no cymon services ({', '.join(missing)} missing)")
        if (node_id, key) not in self._clients:
            service = getattr(cymon, SERVICES[key].split(".")[1] + "_0_1")
            self._clients[(node_id, key)] = self._session.node.presentation.make_client(
                service, ids[key], node_id
            )
        client = self._clients[(node_id, key)]
        client.response_timeout = timeout
        for _ in range(attempts):
            result = await client.call(request)
            if result is not None:
                return result[0]
        raise ScopeError(f"node {node_id} did not answer {SERVICES[key]}")

    async def info(self, node_id: int) -> dict[str, Any]:
        """The variables (index, name, unit) and the buffer limits of a node."""
        import cymon

        variables = []
        for index in range(256):
            answer = await self._call(node_id, "variables", cymon.GetVariableList_0_1.Request(index=index))
            name = answer.name.tobytes().decode(errors="replace")
            if not name:
                break
            variables.append(
                {"id": index, "name": name, "unit": answer.unit.tobytes().decode(errors="replace")}
            )
        buffer = await self._call(node_id, "buffer_info", cymon.GetBufferInfo_0_1.Request())
        return {
            "node_id": node_id,
            "services": await self.discover(node_id),
            "variables": variables,
            "buffer": {
                "buffer_bytes": int(buffer.buffer_bytes),
                "max_channels": int(buffer.max_channels),
                "max_frames": int(buffer.max_frames),
                "max_read_samples": int(buffer.max_read_samples),
                "tick_period_us": float(buffer.tick_period_us),
            },
        }

    async def status(self, node_id: int) -> dict[str, Any]:
        import cymon

        answer = await self._call(
            node_id, "read", cymon.ReadSamples_0_1.Request(frame_offset=0, max_frames=0)
        )
        return {
            "state": STATES.get(int(answer.state.value), str(answer.state.value)),
            "num_channels": int(answer.num_channels),
            "num_samples": int(answer.num_samples),
            "pretrigger_samples": int(answer.pretrigger_samples),
            "sample_period_us": float(answer.sample_period_us),
        }

    async def disarm(self, node_id: int) -> None:
        import cymon

        await self._call(node_id, "arm", cymon.ArmTrigger_0_1.Request(arm=False))

    async def capture(self, node_id: int, request: CaptureRequest) -> Capture:
        """Set up, arm, wait for completion, read out; emits ``capture`` events on the way."""
        if request.trigger_mode not in TRIGGER_MODES:
            raise ScopeError(f"unknown trigger mode {request.trigger_mode!r}")
        await self._setup(node_id, request)
        await self._arm(node_id, request)
        loop = asyncio.get_running_loop()
        await self._wait(node_id, loop.time() + request.timeout_s, request.timeout_s)
        return await self.read(node_id)

    async def multi_capture(
        self, channels: dict[int, list[int]], trigger_node: int, request: CaptureRequest
    ) -> dict[int, Capture]:
        """One capture on several nodes, triggered by ``trigger_node`` on the condition of ``request``.

        ``channels``: the variable IDs per node (``request.channels`` is ignored); the
        period, the length and the pre-trigger part are those of ``request`` on every node.
        The nodes need the shared trigger subject (:meth:`trigger_ports`).
        """
        if trigger_node not in channels:
            raise ScopeError(f"the trigger node {trigger_node} takes part in the capture")
        if request.trigger_mode not in TRIGGER_MODES:
            raise ScopeError(f"unknown trigger mode {request.trigger_mode!r}")
        if len(channels) > 1:
            state = await self.trigger_ports(list(channels))
            if not state["shared"]:
                raise ScopeError(f"the nodes have no shared trigger subject: {state['problem']}")
        followers = [node for node in channels if node != trigger_node]
        for node, node_channels in channels.items():
            await self._setup(node, dataclasses.replace(request, channels=node_channels))
        armed: list[int] = []
        try:
            for node in followers:
                never = dataclasses.replace(
                    request, trigger_mode="rising", trigger_source=channels[node][0], trigger_level=_NEVER
                )
                await self._arm(node, never)
                armed.append(node)
            await self._arm(trigger_node, request)
            armed.append(trigger_node)
            deadline = asyncio.get_running_loop().time() + request.timeout_s
            for node in [trigger_node, *followers]:
                await self._wait(node, deadline, request.timeout_s)
        except BaseException:
            for node in armed:
                with contextlib.suppress(ScopeError):
                    await self.disarm(node)
            raise
        return {node: await self.read(node) for node in [trigger_node, *followers]}

    async def trigger_ports(self, node_ids: list[int]) -> dict[str, Any]:
        """The trigger subjects of the nodes and whether they share one (published and subscribed)."""
        nodes: dict[int, dict[str, int | None]] = {}
        for node_id in node_ids:
            client = self._session.registers(node_id)
            try:
                registers = await client.scan(list(TRIGGER_PORTS))
            except RegisterError as error:
                raise ScopeError(f"cannot read the trigger ports of node {node_id}: {error}") from error
            finally:
                client.close()
            values: dict[str, int | None] = {"pub": None, "sub": None}
            for register in registers:
                content = register.value.content
                if register.value.kind != "empty" and isinstance(content, list) and content:
                    values["pub" if ".pub." in register.name else "sub"] = int(content[0])
            nodes[node_id] = values
        problem = ""
        subjects = {v["pub"] for v in nodes.values()} | {v["sub"] for v in nodes.values()}
        missing = [n for n, v in nodes.items() if v["pub"] is None or v["sub"] is None]
        if missing:
            problem = f"node {missing[0]} has no trigger ports"
        elif len(subjects) != 1 or next(iter(subjects)) is None or int(next(iter(subjects)) or 0) > 8191:
            problem = "the trigger ports differ or are disabled: " + ", ".join(
                f"{n} pub {v['pub']} sub {v['sub']}" for n, v in nodes.items()
            )
        shared = next(iter(subjects)) if not problem else None
        return {
            "nodes": {str(n): v for n, v in nodes.items()},
            "shared": not problem,
            "subject": shared,
            "problem": problem,
        }

    async def set_trigger_ports(self, node_ids: list[int], subject: int = DEFAULT_TRIGGER_SUBJECT) -> None:
        """Writes the shared trigger subject into the nodes; it takes effect after a store and a restart."""
        from cypilot.core.registers import RegisterValue

        if not 0 <= subject <= 8191:
            raise ScopeError(f"a subject-ID is 0 .. 8191, not {subject}")
        for node_id in node_ids:
            client = self._session.registers(node_id)
            try:
                for name in TRIGGER_PORTS:
                    result = await client.write(name, RegisterValue("natural16", [subject]))
                    if not result.accepted:
                        raise ScopeError(f"node {node_id} refused {name} = {subject}")
            except RegisterError as error:
                raise ScopeError(f"cannot write the trigger ports of node {node_id}: {error}") from error
            finally:
                client.close()

    async def _setup(self, node_id: int, request: CaptureRequest) -> None:
        import cymon

        setup = await self._call(
            node_id,
            "setup",
            cymon.SetupCapture_0_1.Request(
                channel_ids=request.channels,
                sample_period_us=request.sample_period_us,
                num_samples=request.num_samples,
                pretrigger_samples=request.pretrigger_samples,
            ),
        )
        _check(setup.result.value, "SetupCapture")
        self._periods[node_id] = float(setup.actual_sample_period_us)

    async def _arm(self, node_id: int, request: CaptureRequest) -> None:
        import cymon

        arm = await self._call(
            node_id,
            "arm",
            cymon.ArmTrigger_0_1.Request(
                arm=True,
                source_variable_id=request.trigger_source,
                mode=cymon.TriggerMode_0_1(TRIGGER_MODES[request.trigger_mode]),
                level=request.trigger_level,
                hysteresis_band=request.hysteresis_band,
            ),
        )
        _check(arm.result.value, "ArmTrigger")
        self._events.emit("capture", node_id=node_id, state="armed", period_us=self._periods.get(node_id))

    async def _wait(self, node_id: int, deadline: float, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        state = "armed"
        while state != "complete":
            if loop.time() > deadline:
                with contextlib.suppress(ScopeError):
                    await self.disarm(node_id)
                raise ScopeError(f"capture on node {node_id} did not complete within {timeout_s} s ({state})")
            await asyncio.sleep(0.05)
            new_state = (await self.status(node_id))["state"]
            if new_state != state:
                state = new_state
                self._events.emit("capture", node_id=node_id, state=state)
            if state in ("idle", "configured"):
                raise ScopeError(f"capture on node {node_id} was disarmed")

    async def read(self, node_id: int) -> Capture:
        """Reads a complete capture again."""
        import cymon

        frames: list[list[float]] = []
        answer: Any = None
        while True:
            answer = await self._call(
                node_id, "read", cymon.ReadSamples_0_1.Request(frame_offset=len(frames), max_frames=_PAGE)
            )
            if int(answer.state.value) != _COMPLETE:
                raise ScopeError(f"capture on node {node_id} is not complete")
            width = int(answer.num_channels)
            values = [float(value) for value in answer.samples]
            frames += [values[i : i + width] for i in range(0, len(values) - width + 1, width)]
            if answer.end_of_data or not values:
                break
        period = float(answer.sample_period_us) * 1e-6
        pretrigger = int(answer.pretrigger_samples)
        capture = Capture(
            node_id=node_id,
            sample_period_us=float(answer.sample_period_us),
            pretrigger_samples=pretrigger,
            t=[(index - pretrigger) * period for index in range(len(frames))],
            channels=[[frame[channel] for frame in frames] for channel in range(int(answer.num_channels))],
        )
        self._events.emit("capture", node_id=node_id, state="read", frames=len(frames))
        return capture


def _check(code: int, what: str) -> None:
    if int(code) != 0:
        raise ScopeError(f"{what} refused: {ERRORS.get(int(code), code)}")
