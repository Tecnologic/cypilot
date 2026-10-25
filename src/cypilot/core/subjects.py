# SPDX-License-Identifier: Apache-2.0
"""Any subject with any DSDL type: live monitoring, and periodic publications with a dead-man lease.

From the cymon tool. These are the generic tools (the expert mode of the web
UI); the typed views of cypilot (readiness and setpoints in
:mod:`cypilot.core.control`, the plots in :mod:`cypilot.core.signals`) do not
need them.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from cypilot.core.events import EventHub
from cypilot.core.signals import flatten_builtin, load_type, type_name

_logger = logging.getLogger(__name__)

MAX_RATE = 1000.0
MAX_LEASE = 60.0


@dataclass
class Subscription:
    sub_id: int
    subject_id: int
    type_name: str
    subscriber: Any
    count: int = 0
    pending: list[dict[str, Any]] = field(default_factory=list)


class SubjectMonitor:
    """Subscribes to subjects by ID and type and emits their numeric fields in batches (event ``subject``)."""

    def __init__(self, node: Any, events: EventHub, batch_period: float = 0.05) -> None:
        self._node = node
        self._events = events
        self._batch_period = batch_period
        self._subs: dict[int, Subscription] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task[None] | None = None

    def subscribe(self, subject_id: int, data_type: str) -> dict[str, Any]:
        """Starts monitoring; the same subject and type again returns the running subscription."""
        for sub in self._subs.values():
            if sub.subject_id == subject_id and sub.type_name == data_type:
                return self._describe(sub)
        compiled = load_type(data_type)
        subscriber = self._node.make_subscriber(compiled, subject_id)
        sub = Subscription(next(self._ids), subject_id, type_name(compiled), subscriber)
        self._subs[sub.sub_id] = sub

        async def handler(message: Any, transfer: Any) -> None:
            import pycyphal.dsdl

            sub.count += 1
            data = jsonable(pycyphal.dsdl.to_builtin(message))
            sub.pending.append(
                {
                    "t": float(transfer.timestamp.system),
                    "source": transfer.source_node_id,
                    "fields": flatten_builtin(data),
                    "data": data,
                }
            )

        subscriber.receive_in_background(handler)
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._flush_loop())
        return self._describe(sub)

    def unsubscribe(self, sub_id: int) -> None:
        sub = self._subs.pop(sub_id, None)
        if sub is not None:
            sub.subscriber.close()

    def list(self) -> list[dict[str, Any]]:
        return [self._describe(sub) for sub in self._subs.values()]

    @staticmethod
    def _describe(sub: Subscription) -> dict[str, Any]:
        return {"id": sub.sub_id, "subject_id": sub.subject_id, "type": sub.type_name, "count": sub.count}

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._batch_period)
            for sub in self._subs.values():
                if sub.pending:
                    samples, sub.pending = sub.pending, []
                    # The structure of the last message is enough for a tree view.
                    self._events.emit(
                        "subject",
                        id=sub.sub_id,
                        subject_id=sub.subject_id,
                        type=sub.type_name,
                        t=[sample["t"] for sample in samples],
                        source=[sample["source"] for sample in samples],
                        fields={
                            key: [sample["fields"].get(key) for sample in samples]
                            for key in samples[-1]["fields"]
                        },
                        last=samples[-1]["data"],
                    )

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for sub_id in list(self._subs):
            self.unsubscribe(sub_id)


@dataclass
class Publication:
    pub_id: int
    subject_id: int
    type_name: str
    message: Any
    rate_hz: float
    lease_s: float
    publisher: Any
    expires: float
    sent: int = 0
    owner: str = ""
    task: asyncio.Task[None] | None = None


class PublisherManager:
    """Periodic publications that stop by themselves unless their lease is renewed.

    The web client renews the leases of its publications several times a
    second. When the browser closes, loses the connection or hangs, the
    publications stop within ``lease_s``; a drive that needs a periodic
    readiness or setpoint then falls back to its safe state by itself.
    """

    def __init__(self, node: Any, events: EventHub) -> None:
        self._node = node
        self._events = events
        self._pubs: dict[int, Publication] = {}
        self._ids = itertools.count(1)

    def start(
        self,
        subject_id: int,
        data_type: str,
        value: dict[str, Any] | None,
        rate_hz: float,
        lease_s: float = 1.0,
        priority: str = "nominal",
        owner: str = "",
    ) -> dict[str, Any]:
        import pycyphal.dsdl
        import pycyphal.transport

        if not 0.1 <= rate_hz <= MAX_RATE:
            raise ValueError(f"rate_hz must be within 0.1 .. {MAX_RATE:g}")
        if not 0.1 <= lease_s <= MAX_LEASE:
            raise ValueError(f"lease_s must be within 0.1 .. {MAX_LEASE:g}")
        compiled = load_type(data_type)
        message = pycyphal.dsdl.update_from_builtin(compiled(), value or {})
        publisher = self._node.make_publisher(compiled, subject_id)
        publisher.priority = pycyphal.transport.Priority[priority.upper()]
        publisher.send_timeout = min(1.0, 1.0 / rate_hz)
        pub = Publication(
            next(self._ids),
            subject_id,
            type_name(compiled),
            message,
            rate_hz,
            lease_s,
            publisher,
            time.monotonic() + lease_s,
            owner=owner,
        )
        self._pubs[pub.pub_id] = pub
        pub.task = asyncio.get_running_loop().create_task(self._run(pub))
        self._events.emit("publisher", action="started", **self._describe(pub))
        return self._describe(pub)

    def update(self, pub_id: int, value: dict[str, Any]) -> dict[str, Any]:
        import pycyphal.dsdl

        pub = self._pubs[pub_id]
        pub.message = pycyphal.dsdl.update_from_builtin(type(pub.message)(), value)
        return self._describe(pub)

    def renew(self, owner: str | None = None, pub_id: int | None = None) -> None:
        now = time.monotonic()
        for pub in self._pubs.values():
            if (pub_id is None or pub.pub_id == pub_id) and (owner is None or pub.owner == owner):
                pub.expires = now + pub.lease_s

    def stop(self, pub_id: int, reason: str = "stopped") -> None:
        pub = self._pubs.pop(pub_id, None)
        if pub is None:
            return
        if pub.task is not None and pub.task is not asyncio.current_task():
            pub.task.cancel()
        pub.publisher.close()
        self._events.emit("publisher", action="stopped", reason=reason, **self._describe(pub))

    def stop_all(self, owner: str | None = None, reason: str = "stopped") -> int:
        ids = [pub.pub_id for pub in self._pubs.values() if owner is None or pub.owner == owner]
        for pub_id in ids:
            self.stop(pub_id, reason)
        return len(ids)

    def list(self) -> list[dict[str, Any]]:
        return [self._describe(pub) for pub in self._pubs.values()]

    async def _run(self, pub: Publication) -> None:
        period = 1.0 / pub.rate_hz
        next_time = time.monotonic()
        while True:
            if time.monotonic() > pub.expires:
                _logger.warning("publication %d on subject %d: lease expired", pub.pub_id, pub.subject_id)
                self.stop(pub.pub_id, reason="lease expired")
                return
            try:
                if await pub.publisher.publish(pub.message):
                    pub.sent += 1
            except Exception as error:  # a transport error must not end the loop silently
                _logger.error("publication %d failed: %s", pub.pub_id, error)
            next_time += period
            await asyncio.sleep(max(0.0, next_time - time.monotonic()))

    @staticmethod
    def _describe(pub: Publication) -> dict[str, Any]:
        import pycyphal.dsdl

        return {
            "id": pub.pub_id,
            "subject_id": pub.subject_id,
            "type": pub.type_name,
            "rate_hz": pub.rate_hz,
            "lease_s": pub.lease_s,
            "sent": pub.sent,
            "owner": pub.owner,
            "value": jsonable(pycyphal.dsdl.to_builtin(pub.message)),
        }


def jsonable(value: Any) -> Any:
    """Plain JSON types from ``pycyphal.dsdl.to_builtin`` (numpy scalars become Python numbers)."""
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, int | float | bool | str):
        return item()
    return value
