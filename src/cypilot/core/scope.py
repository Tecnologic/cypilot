# SPDX-License-Identifier: Apache-2.0
"""Triggered captures of the cymon trace protocol (cymon-lib on the node), from the cymon tool.

The cymon services have no fixed port-IDs: :meth:`ScopeClient.discover`
reads the node's port registers and takes the ``uavcan.srv.*.id`` of each
service whose ``.type`` is ``cymon.<Service>.0.1`` (``docs/plan.md``,
section 8.2). A capture is set up, armed, polled until complete and read out
page by page; the time axis is relative to the trigger sample.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        import cymon

        if request.trigger_mode not in TRIGGER_MODES:
            raise ScopeError(f"unknown trigger mode {request.trigger_mode!r}")
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
        self._events.emit(
            "capture", node_id=node_id, state="armed", period_us=float(setup.actual_sample_period_us)
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_s
        state = "armed"
        while state != "complete":
            if loop.time() > deadline:
                with contextlib.suppress(ScopeError):
                    await self.disarm(node_id)
                raise ScopeError(
                    f"capture on node {node_id} did not complete within {request.timeout_s} s ({state})"
                )
            await asyncio.sleep(0.05)
            new_state = (await self.status(node_id))["state"]
            if new_state != state:
                state = new_state
                self._events.emit("capture", node_id=node_id, state=state)
            if state in ("idle", "configured"):
                raise ScopeError(f"capture on node {node_id} was disarmed")
        return await self.read(node_id)

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
