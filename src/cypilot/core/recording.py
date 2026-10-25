# SPDX-License-Identifier: Apache-2.0
"""Records a run on the bus of a session: raw frames, decoded subjects, node snapshots and markers.

:class:`RunRecording` puts what the session sees into an
:class:`~cypilot.core.runs.ActiveRun`. It starts only when the user starts a
run (``docs/web_plan.md``, section 6) and runs on the session's loop:

- **frames**: a capture handler on the transport (CAN only); pycyphal cannot
  end a capture, so the handler stays and writes only while a run records;
- **subjects**: every published port with a ``.type`` register of every node,
  also of nodes that appear during the run;
- **nodes**: GetInfo and a register snapshot of each node when it is first seen;
- **markers**: control, stops, register writes, commands and captures from the
  event hub, engage, trip, E-stop and fault of the nodes, and notes of the user;
- every second it flushes the files, sends a ``run`` event with duration and
  size, and stops the run when the disk gets full.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from cypilot.core.events import EventHub
from cypilot.core.runs import ActiveRun, RunStore
from cypilot.core.session import Session
from cypilot.core.signals import Source, flatten, load_type, sources

_logger = logging.getLogger(__name__)

FLUSH_PERIOD = 1.0
NODE_PERIOD = 0.5
MARKED_CHIPS = ("engaged", "tripped", "E-stop latched", "fault", "offline")
"""State chips of a node whose coming and going is a marker."""


def marker_text(event: dict[str, Any]) -> str | None:
    """The marker of an event of the hub, or None for an event that is no marker."""
    kind = event.get("kind")
    if kind == "control":
        return f"control {event.get('reason')}"
    if kind == "stop":
        return f"STOP ALL {event.get('nodes')}"
    if kind == "estop":
        return f"E-STOP {sorted(event.get('nodes', {}))}"
    if kind == "register":
        return f"{event.get('node_id')} {event.get('name')} written" + (
            "" if event.get("accepted") else " (refused)"
        )
    if kind == "command":
        return f"{event.get('node_id')} command {event.get('command')}: {event.get('status')}"
    return None


class RunRecording:
    """One run on one session; :meth:`close` ends it."""

    def __init__(self, session: Session, store: RunStore, events: EventHub, run: ActiveRun) -> None:
        self._session = session
        self._store = store
        self._events = events
        self.run = run
        self._subscribers: list[Any] = []
        self._seen: set[int] = set()
        self._chips: dict[int, set[str]] = {}
        self._ifaces: dict[int, int] = {}
        self._fd = any(spec.is_fd for spec in session.interfaces)
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._flush_loop()),
            loop.create_task(self._node_loop()),
            loop.create_task(self._event_loop()),
        ]

    # --- Frames ------------------------------------------------------------------

    def on_capture(self, capture: Any) -> None:
        """A frame from the transport's capture (also the frames this app sends)."""
        if self._closed:
            return
        iface = 0
        inferior = getattr(capture, "inferior", None)
        if inferior is not None:  # redundant transport
            iface = self._ifaces.setdefault(capture.iface_id, len(self._ifaces))
            capture = inferior
        frame = getattr(capture, "frame", None)
        if frame is None:  # not CAN (UDP): no raw log
            return
        stamp = capture.timestamp.system_ns * 1e-9
        self.run.frame(stamp, iface, frame.identifier, bytes(frame.data), self._fd or len(frame.data) > 8)

    # --- Nodes and subjects ------------------------------------------------------

    async def _node_loop(self) -> None:
        while True:
            for node in self._session.nodes():
                self._mark_chips(node.node_id, {chip.value for chip in node.chips} & set(MARKED_CHIPS))
                if node.node_id not in self._seen:
                    self._seen.add(node.node_id)
                    await self._add_node(node)
            await asyncio.sleep(NODE_PERIOD)

    def _mark_chips(self, node_id: int, chips: set[str]) -> None:
        before = self._chips.get(node_id)
        self._chips[node_id] = chips
        if before is None:
            return
        for chip in sorted(chips - before):
            self.run.mark(f"{node_id} {chip}", kind="state")
        for chip in sorted(before - chips):
            self.run.mark(f"{node_id} no longer {chip}", kind="state")

    async def _add_node(self, node: Any) -> None:
        from cypilot.core.hub import node_json

        self.run.set_node(node.node_id, {"info": node_json(node)})
        client = self._session.registers(node.node_id)
        try:
            registers = await client.scan()
        except Exception as error:  # a node that does not answer is still recorded by its frames
            _logger.warning("run: no registers of node %d: %s", node.node_id, error)
            self.run.set_node(node.node_id, {"registers_error": str(error)})
            return
        finally:
            client.close()
        self.run.set_node(node.node_id, {"registers": {r.name: r.value.format() for r in registers}})
        for source in sources(registers, node.node_id):
            try:
                self._subscribe(source)
            except Exception as error:  # a type that is not in the DSDL: frames only
                _logger.warning("run: cannot decode %s: %s", source.key, error)

    def _subscribe(self, source: Source) -> None:
        subscriber = self._session.node.make_subscriber(load_type(source.type_name), source.subject)
        run = self.run

        async def on_message(message: Any, transfer: Any) -> None:
            if transfer.source_node_id == source.node_id and not self._closed:
                run.sample(source, transfer.timestamp.system_ns * 1e-9, flatten(message))

        subscriber.receive_in_background(on_message)
        self._subscribers.append(subscriber)

    # --- Markers -----------------------------------------------------------------

    async def _event_loop(self) -> None:
        async with self._events.listen() as queue:
            while True:
                event = await queue.get()
                text = marker_text(event)
                if text is not None:
                    self.run.mark(text, event.get("at"), kind=str(event["kind"]))

    # --- Upkeep ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_PERIOD)
            self.run.flush()
            if self._store.out_of_space():
                _logger.error("run %s: the disk is nearly full, the run stops", self.run.run_id)
                self.run.mark("stopped: the disk is nearly full", kind="stop")
                self._events.emit("run", action="disk full", run=self.run.summary())
                asyncio.get_running_loop().create_task(self._stop_for_space())
                return
            self._events.emit("run", action="recording", run=self.run.summary())

    async def _stop_for_space(self) -> None:
        summary = await self.close("the disk is nearly full")
        self._events.emit("run", action="stopped", run=summary)

    async def close(self, reason: str) -> dict[str, Any] | None:
        """Ends the run: the subscriptions stop, the files are closed and packed."""
        if self._closed:
            return None
        self._closed = True
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for task in self._tasks:
            if task is not current:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for subscriber in self._subscribers:
            subscriber.close()
        if self._store.active is self.run:
            return await asyncio.to_thread(self._store.stop, reason)
        return None
