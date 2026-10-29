# SPDX-License-Identifier: Apache-2.0
"""The bus of the web app: one connection at a time, the services on it, the control lease and events.

The web app starts with no bus or with one from the command line; a client
connects and disconnects at run time. :class:`BusHub` owns the
:class:`~cypilot.core.session.Session` of the connection and everything that
runs on it (the controller, the subject monitor, the publications, the
scope), and an :class:`~cypilot.core.events.EventHub` that outlives
connections, so open WebSockets keep working across a reconnect.

**Control lease** (``docs/web_plan.md``, section 5): one client at a time is
the operator. Only the operator enables drives, sends setpoints, writes
registers and runs commands that change a node; any client may stop. The
operator renews its lease over its WebSocket; the renewals feed the
controller's watchdog, so a phone that loses Wi-Fi, sleeps or closes the tab
puts the engaged drives to sleep within the lease time. A startup aid the
operator runs (``cypilot.core.startup``) owns the outputs without readiness,
so it is aborted instead: when the lease lapses, is released or its client
leaves, on a disconnect, and on stop all (every UNIMOC drive).

Everything runs on the one asyncio loop of the web server.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

from cypilot import __version__
from cypilot.core.control import ControlError, Controller, DriveTarget, find_target
from cypilot.core.events import EventHub
from cypilot.core.recording import RunRecording
from cypilot.core.runs import RunError, RunStore
from cypilot.core.scope import ScopeClient
from cypilot.core.session import DEFAULT_LOCAL_NODE_ID, SEVERITY_NAMES, LogRecord, NodeSummary, Session
from cypilot.core.startup import abort_running
from cypilot.core.subjects import PublisherManager, SubjectMonitor
from cypilot.core.transport import make_transport, parse_interface, parse_interfaces

_logger = logging.getLogger(__name__)

DEFAULT_LEASE = 0.5
"""Seconds the operator's lease lasts without a renewal (the controller's watchdog)."""
LEASE_LIMITS = (0.3, 1.0)
SNAPSHOT_PERIOD = 0.25
STARTUP_WATCH_PERIOD = 0.1
"""Seconds between two checks of the lease while a startup aid runs."""
LOG_LENGTH = 2000
DEMO_CHANNEL = "virtual:cypilot-demo"


class NotConnected(RuntimeError):
    """The hub has no bus connection."""


class NotOperator(PermissionError):
    """The client does not hold the control lease."""


@dataclasses.dataclass
class BusConfig:
    interfaces: list[str] = dataclasses.field(default_factory=list)
    """Interface texts; several form a redundant bus. Ignored with ``demo_devices``."""
    local_node_id: int = DEFAULT_LOCAL_NODE_ID
    mtu: int | None = None
    bitrate: tuple[int, int] | None = None
    pnp: str | None = None
    """Plug-and-play allocator database; None: no allocator."""
    demo_devices: int = 0
    """Demo devices (node-IDs 10, 11, ...) on a virtual bus instead of the interfaces."""


@dataclasses.dataclass
class _Lease:
    client: str
    expires: float


def node_json(node: NodeSummary) -> dict[str, Any]:
    """A node as the web clients see it."""
    return {
        "node_id": node.node_id,
        "name": node.name,
        "health": node.health.name.lower(),
        "mode": node.mode.name.lower(),
        "vendor_code": node.vendor_code,
        "uptime": node.uptime,
        "software_version": ".".join(map(str, node.software_version)) if node.software_version else None,
        "hardware_version": ".".join(map(str, node.hardware_version)) if node.hardware_version else None,
        "vcs_revision": f"{node.vcs_revision:016x}" if node.vcs_revision is not None else None,
        "unique_id": node.unique_id.hex() if node.unique_id else None,
        "unimoc": node.unimoc,
        "chips": [chip.value for chip in node.chips],
    }


class BusHub:
    """Connects to and disconnects from the bus at run time; owns the control lease."""

    def __init__(
        self,
        *,
        lease_time: float = DEFAULT_LEASE,
        clock: Callable[[], float] = time.monotonic,
        runs: RunStore | None = None,
    ) -> None:
        """``runs``: where runs are recorded; None: no recording."""
        if not LEASE_LIMITS[0] <= lease_time <= LEASE_LIMITS[1]:
            raise ValueError(f"the lease time must be within {LEASE_LIMITS[0]} .. {LEASE_LIMITS[1]} s")
        self.events = EventHub()
        self.lease_time = lease_time
        self._clock = clock
        self.config: BusConfig | None = None
        self.session: Session | None = None
        self.controller: Controller | None = None
        self.subjects: SubjectMonitor | None = None
        self.publishers: PublisherManager | None = None
        self.scope: ScopeClient | None = None
        self.log: collections.deque[dict[str, Any]] = collections.deque(maxlen=LOG_LENGTH)
        self._lease: _Lease | None = None
        self._devices: list[Any] = []
        self._snapshots: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._connection = 0
        self.runs = runs
        self.recording: RunRecording | None = None
        self._capturing: Session | None = None
        self._startup_runs: set[int] = set()
        self._startup_watch: asyncio.Task[None] | None = None

    # --- Connection ------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.session is not None

    def require_session(self) -> Session:
        if self.session is None:
            raise NotConnected("not connected to a bus")
        return self.session

    async def connect(self, config: BusConfig) -> dict[str, Any]:
        """Closes the current connection, if any, and opens one with ``config``."""
        async with self._lock:
            await self._close()
            if config.demo_devices:
                specs = [parse_interface(DEMO_CHANNEL)]
                self._start_demo(config.demo_devices, specs[0])
            else:
                specs = parse_interfaces(config.interfaces, mtu=config.mtu, bitrate=config.bitrate)
            session = Session(specs, local_node_id=config.local_node_id, pnp_database=config.pnp)
            try:
                await session.open()
            except Exception:
                await session.close()
                self._stop_demo()
                raise
            session.add_record_handler(self._on_record)
            self.session = session
            self.controller = Controller(
                session, watchdog_timeout=self.lease_time, on_watchdog=self._on_watchdog
            )
            self.controller.start()
            self.subjects = SubjectMonitor(session.node, self.events)
            self.publishers = PublisherManager(session.node, self.events)
            self.scope = ScopeClient(session, self.events)
            self.config = config
            self._connection += 1
            self._snapshots = asyncio.get_running_loop().create_task(self._publish_snapshots(session))
            _logger.info("connected: %s", session.describe())
            self._emit_status()
        return self.status()

    async def disconnect(self) -> dict[str, Any]:
        async with self._lock:
            if self.connected:
                await self._close()
                self._emit_status()
        return self.status()

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        if self._startup_runs:
            await self.abort_startup("the bus was disconnected", sorted(self._startup_runs))
        if self._startup_watch is not None:
            self._startup_watch.cancel()
            self._startup_watch = None
        if self.recording is not None:
            await self.stop_run("the bus was disconnected")
        if self._snapshots is not None:
            self._snapshots.cancel()
            self._snapshots = None
        if self.publishers is not None:
            self.publishers.stop_all(reason="disconnect")
        if self.subjects is not None:
            self.subjects.close()
        if self.scope is not None:
            self.scope.close()
        if self.controller is not None:
            await self.controller.close()  # SLEEP to every engaged drive
        if self.session is not None:
            await self.session.close()
        self.session = self.controller = None
        self.subjects = self.publishers = None
        self.scope = None
        self.config = None
        self._lease = None
        self._stop_demo()

    def _start_demo(self, count: int, spec: Any) -> None:
        from cypilot.demo import DemoDevice

        self._devices = [
            DemoDevice(make_transport(spec, 10 + index), name=f"org.tecnologic.cypilot.demo{index}")
            for index in range(count)
        ]
        for device in self._devices:
            device.start()

    def _stop_demo(self) -> None:
        for device in self._devices:
            device.close()
        self._devices = []

    # --- Status and events -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "version": __version__,
            "connected": self.connected,
            "connection": self._connection,
            "lease_time": self.lease_time,
            "control": self.control_state(),
        }
        if self.session is not None and self.config is not None:
            status.update(
                {
                    "interface": self.session.describe(),
                    "demo_devices": self.config.demo_devices,
                    "local_node_id": self.session.local_node_id,
                    "pnp": self.session.pnp,
                }
            )
        return status

    def nodes(self) -> list[dict[str, Any]]:
        return [node_json(node) for node in self.require_session().nodes()]

    def _emit_status(self) -> None:
        self.events.emit("status", status=self.status())

    async def _publish_snapshots(self, session: Session) -> None:
        last: list[NodeSummary] | None = None
        while True:
            nodes = session.nodes()
            if nodes != last:
                last = nodes
                self.events.emit("nodes", nodes=[node_json(node) for node in nodes])
            await asyncio.sleep(SNAPSHOT_PERIOD)

    def _on_record(self, record: LogRecord) -> None:
        entry = {
            "t": record.received,
            "node_id": record.node_id,
            "severity": SEVERITY_NAMES[record.severity] if 0 <= record.severity < 8 else str(record.severity),
            "text": record.text,
        }
        self.log.append(entry)
        self.events.emit("log", **entry)

    # --- Control lease ---------------------------------------------------------

    def control_state(self) -> dict[str, Any]:
        lease = self._current_lease()
        engaged = sorted(self.controller.engaged) if self.controller is not None else []
        return {"operator": lease.client if lease else None, "engaged": engaged}

    def _current_lease(self) -> _Lease | None:
        if self._lease is not None and self._clock() > self._lease.expires:
            self._lease = None
        return self._lease

    def is_operator(self, client: str) -> bool:
        lease = self._current_lease()
        return lease is not None and lease.client == client

    def require_operator(self, client: str) -> Controller:
        """The controller, for a client that holds the control lease."""
        self.require_session()
        if not self.is_operator(client):
            raise NotOperator("take control first: another client or nobody holds the control lease")
        assert self.controller is not None
        return self.controller

    def take_control(self, client: str, force: bool = False) -> dict[str, Any]:
        """Makes ``client`` the operator. A live lease of another client is only taken with ``force``."""
        self.require_session()
        lease = self._current_lease()
        if lease is not None and lease.client != client and not force:
            raise NotOperator(f"client {lease.client} is the operator; take over with force")
        if lease is not None and lease.client != client:
            _logger.warning("control taken over from %s by %s", lease.client, client)
        self._lease = _Lease(client, self._clock() + self.lease_time)
        assert self.controller is not None
        self.controller.feed()
        self._emit_control("taken")
        return self.control_state()

    async def release_control(self, client: str) -> dict[str, Any]:
        """Gives the lease back; the engaged drives are put to sleep first."""
        if self.is_operator(client):
            await self._stop_engaged("control released")
            await self._abort_startup_runs("control released")
            self._lease = None
            self._emit_control("released")
        return self.control_state()

    def renew(self, client: str) -> None:
        """A client is alive (every 100 ms over its WebSocket).

        Renews its publications and, for the operator, the lease.
        """
        if self.publishers is not None:
            self.publishers.renew(owner=client)
        if self.is_operator(client) and self._lease is not None and self.controller is not None:
            self._lease.expires = self._clock() + self.lease_time
            self.controller.feed()

    async def client_gone(self, client: str) -> None:
        """The client's WebSocket closed: its publications stop, an operator's drives go to sleep at once."""
        if self.publishers is not None:
            stopped = self.publishers.stop_all(owner=client, reason="client disconnected")
            if stopped:
                _logger.warning("client %s left: stopped %d publication(s)", client, stopped)
        if self._lease is not None and self._lease.client == client:
            await self._stop_engaged("operator disconnected")
            await self._abort_startup_runs("operator disconnected")
            self._lease = None
            self._emit_control("operator disconnected")

    # --- Stops (any client, never locked) -------------------------------------

    async def stop_all(self) -> list[int]:
        """SLEEP on every readiness subject the controller knows at once, then on every other UNIMOC drive.

        A running startup aid ignores readiness: it is aborted on every UNIMOC drive.
        """
        session = self.require_session()
        assert self.controller is not None
        await self.controller.stop_all()
        stopped = set(self.controller.known)
        stopped.update(
            await self.abort_startup("stop all", [node.node_id for node in session.nodes() if node.is_unimoc])
        )
        others: list[DriveTarget] = []
        for node in session.nodes():
            if node.is_unimoc and node.node_id not in stopped:
                try:
                    others.append(await find_target(session, node.node_id))
                except Exception:  # a node that does not answer must not end the stop
                    continue
        if others:
            await self.controller.stop_all(others)
        if self.publishers is not None:
            self.publishers.stop_all(reason="stop all")
        result = sorted(stopped | {target.node_id for target in others})
        self.events.emit("stop", nodes=result)
        return result

    async def emergency_stop(self) -> dict[int, int | None]:
        """Stops everything this app publishes, then ExecuteCommand EMERGENCY_STOP to every UNIMOC node."""
        session = self.require_session()
        assert self.controller is not None
        if self.publishers is not None:
            self.publishers.stop_all(reason="emergency stop")
        targets = [node.node_id for node in session.nodes() if node.is_unimoc]
        result = await self.controller.emergency_stop(targets)
        self.events.emit("estop", nodes={str(node_id): status for node_id, status in result.items()})
        return result

    # --- Startup aid ----------------------------------------------------------

    def startup_started(self, node_id: int) -> None:
        """The operator started the startup aid of a drive: it is aborted when the lease ends."""
        self._startup_runs.add(node_id)
        if self._startup_watch is None or self._startup_watch.done():
            self._startup_watch = asyncio.get_running_loop().create_task(self._watch_startup())

    async def abort_startup(self, reason: str, node_ids: list[int]) -> list[int]:
        """Aborts the startup aid where it runs on ``node_ids``; the nodes it stopped."""
        if self.session is None or not node_ids:
            return []
        stopped = await abort_running(self.session.node, node_ids)
        self._startup_runs.difference_update(node_ids)
        if stopped:
            _logger.warning("%s: startup aid aborted on %s", reason, stopped)
            self.events.emit("startup", reason=reason, nodes=stopped)
        return stopped

    async def _abort_startup_runs(self, reason: str) -> None:
        if self._startup_runs:
            await self.abort_startup(reason, sorted(self._startup_runs))

    async def _watch_startup(self) -> None:
        """Aborts the operator's runs once its lease has lapsed (the controller watches engaged drives)."""
        while self._startup_runs:
            await asyncio.sleep(STARTUP_WATCH_PERIOD)
            if self._current_lease() is None and self._startup_runs:
                await self._abort_startup_runs("lease lost")
                self._emit_control("lease lost")

    def _on_watchdog(self) -> None:
        """The controller put the drives to sleep: the operator's lease lapsed."""
        self._lease = None
        self._emit_control("lease lost")

    async def _stop_engaged(self, reason: str) -> None:
        if self.controller is not None and self.controller.engaged:
            _logger.warning("%s: SLEEP to %s", reason, sorted(self.controller.engaged))
            with contextlib.suppress(ControlError):
                await self.controller.stop_all()

    def _emit_control(self, reason: str) -> None:
        self.events.emit("control", reason=reason, **self.control_state())

    # --- Runs (started and stopped only by the user; any client) -----------------

    def require_runs(self) -> RunStore:
        if self.runs is None:
            raise RunError("this server records no runs")
        return self.runs

    def run_state(self) -> dict[str, Any]:
        if self.runs is None:
            return {"available": False, "active": None}
        active = self.recording.run.summary() if self.recording is not None else None
        return {"available": True, "active": active, "space": self.runs.space()}

    async def start_run(self, name: str = "", notes: str = "") -> dict[str, Any]:
        """Starts recording everything on the bus; only ever on a request of the user."""
        store = self.require_runs()
        session = self.require_session()
        if self.recording is not None:
            raise RunError(f"run {self.recording.run.run_id} records already")
        info = {
            "cypilot": __version__,
            "interface": session.describe(),
            "interfaces": [
                {"text": spec.text, "mtu": spec.mtu, "bitrate": list(spec.bitrate)}
                for spec in session.interfaces
            ],
            "local_node_id": session.local_node_id,
            "lease_time": self.lease_time,
        }
        run = store.start(name, notes, info)
        recording = RunRecording(session, store, self.events, run)
        self.recording = recording
        if self._capturing is not session:
            transport = session.node.presentation.transport
            with contextlib.suppress(Exception):  # transports without a capture: subjects only
                transport.begin_capture(self._on_capture)
            self._capturing = session
        recording.start()
        _logger.info("run %s started", run.run_id)
        self.events.emit("run", action="started", run=run.summary())
        return run.summary()

    async def stop_run(self, reason: str = "stopped by the user") -> dict[str, Any] | None:
        recording, self.recording = self.recording, None
        if recording is None:
            return None
        summary = await recording.close(reason)
        _logger.info("run %s stopped: %s", recording.run.run_id, reason)
        self.events.emit("run", action="stopped", run=summary)
        return summary

    def mark_run(self, text: str) -> None:
        """A note of the user on the time axis of the run."""
        if self.recording is None:
            raise RunError("no run records")
        self.recording.run.mark(text, kind="note")

    def _on_capture(self, capture: Any) -> None:
        recording = self.recording
        if recording is not None:
            recording.on_capture(capture)
