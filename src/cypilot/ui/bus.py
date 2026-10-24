# SPDX-License-Identifier: Apache-2.0
"""The bus thread of the GUI (``docs/plan.md``, section 2: threads).

All Cyphal I/O runs on one asyncio loop in its own thread. The UI never awaits:
it submits a job (a coroutine function taking the :class:`Session`) and gets
the result through a Qt signal on the UI thread, so a slow bus never freezes
the window and the window never blocks the bus. Discovery snapshots and log
records arrive the same way.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot

from cypilot.core.control import Controller, DriveTarget, find_target
from cypilot.core.session import LogRecord, NodeSummary, Session
from cypilot.core.transport import InterfaceSpec

T = TypeVar("T")

SNAPSHOT_PERIOD = 0.25
"""Seconds between two node snapshots for the UI."""


class Bus(QObject):
    """Owns the bus thread and the session; lives on the UI thread."""

    nodes_changed = Signal(object)
    """``list[NodeSummary]``, whenever discovery changed."""
    record_received = Signal(object)
    """``LogRecord`` of any node."""
    connected = Signal(str)
    """The interface text, once the session is open."""
    disconnected = Signal(str)
    """The reason: empty after a disconnect by the user, else the error."""

    _deliver = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="cypilot bus", daemon=True)
        self._thread.start()
        self._session: Session | None = None
        self._controller: Controller | None = None
        self._snapshots: asyncio.Task[None] | None = None
        self._deliver.connect(self._on_deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    # --- Jobs ----------------------------------------------------------------

    def call(
        self,
        job: Callable[[Session], Awaitable[T]],
        done: Callable[[T | BaseException], None] | None = None,
    ) -> Future[T]:
        """Runs ``job(session)`` on the bus loop; ``done(result or exception)`` follows on the UI thread."""
        future = asyncio.run_coroutine_threadsafe(self._with_session(job), self._loop)
        if done is not None:
            future.add_done_callback(lambda f: self._deliver.emit(done, _outcome(f)))
        return future

    async def _with_session(self, job: Callable[[Session], Awaitable[T]]) -> T:
        if self._session is None:
            raise ConnectionError("not connected")
        return await job(self._session)

    def control(
        self,
        job: Callable[[Session, Controller], Awaitable[T]],
        done: Callable[[T | BaseException], None] | None = None,
    ) -> Future[T]:
        """Like :meth:`call`, with the session's :class:`Controller` (readiness, setpoints, stop)."""

        async def with_controller(session: Session) -> T:
            if self._controller is None:
                raise ConnectionError("not connected")
            return await job(session, self._controller)

        return self.call(with_controller, done)

    def feed(self) -> None:
        """The UI thread is alive (the watchdog of the readiness keep-alive); call every 100 ms."""
        controller = self._controller
        if controller is not None:
            controller.feed()

    def stop_all(self, done: Callable[[list[int] | BaseException], None] | None = None) -> None:
        """SLEEP on every readiness subject cypilot knows at once, then on every other drive it finds."""

        async def stop(session: Session, controller: Controller) -> list[int]:
            await controller.stop_all()
            stopped = set(controller.known)
            others: list[DriveTarget] = []
            for node in session.nodes():
                if node.is_unimoc and node.node_id not in stopped:
                    try:
                        others.append(await find_target(session, node.node_id))
                    except Exception:  # a node that does not answer must not end the stop
                        continue
            if others:
                await controller.stop_all(others)
            return sorted(stopped | {t.node_id for t in others})

        self.control(stop, done)

    # --- Connection ----------------------------------------------------------

    def connect_to(self, interface: InterfaceSpec, local_node_id: int) -> None:
        """Opens a session; ``connected`` or ``disconnected`` (with the error) follows."""
        asyncio.run_coroutine_threadsafe(self._open(interface, local_node_id), self._loop)

    def disconnect_from(self) -> None:
        asyncio.run_coroutine_threadsafe(self._close(""), self._loop)

    def shutdown(self) -> None:
        """Closes the session and stops the bus thread (when the window closes)."""
        if self._thread.is_alive():
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if not self._thread.is_alive():
                self._loop.close()

    async def _close_all(self) -> None:
        """Closes the session and ends the tasks pycyphal leaves on the loop."""
        await self._close("")
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _open(self, interface: InterfaceSpec, local_node_id: int) -> None:
        await self._close("")
        session = Session(interface, local_node_id)
        try:
            await session.open()
        except Exception as error:  # the transport and the DSDL raise many types
            await session.close()
            self.disconnected.emit(str(error) or type(error).__name__)
            return
        session.add_record_handler(self._on_record)
        self._session = session
        self._controller = Controller(session)
        self._snapshots = asyncio.get_running_loop().create_task(self._publish_snapshots(session))
        self.connected.emit(interface.text)

    async def _close(self, reason: str) -> None:
        if self._snapshots is not None:
            self._snapshots.cancel()
            self._snapshots = None
        if self._controller is not None:
            controller, self._controller = self._controller, None
            await controller.close()  # SLEEP to every engaged drive
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()
            self.disconnected.emit(reason)

    async def _publish_snapshots(self, session: Session) -> None:
        last: list[NodeSummary] | None = None
        while True:
            nodes = session.nodes()
            if nodes != last:
                last = nodes
                self.nodes_changed.emit(nodes)
            await asyncio.sleep(SNAPSHOT_PERIOD)

    def _on_record(self, record: LogRecord) -> None:
        self.record_received.emit(record)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    @Slot(object, object)
    def _on_deliver(self, done: Callable[[Any], None], outcome: Any) -> None:
        done(outcome)


def _outcome(future: Future[T]) -> T | BaseException:
    if future.cancelled():
        return asyncio.CancelledError()
    error = future.exception()
    return error if error is not None else future.result()
