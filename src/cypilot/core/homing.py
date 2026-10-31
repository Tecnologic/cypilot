# SPDX-License-Identifier: Apache-2.0
"""UNIMOC's homing run: the START_HOMING command and the homing state subject.

The drive, engaged in position control, runs its shaft against the stop and
takes the stop as position zero (UNIMOC README, "Homing"). The command answer
says at once whether the run started, with the reason of a refusal; the
state comes from ``uavcan.pub.homing_state`` (``uavcan.primitive.scalar.Natural8.1.0``),
on every change. A drive without that port has no homing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

HOMING_STATE_ID = "uavcan.pub.homing_state.id"
"""Register of the homing state subject; discovery finds homing by it."""
HOMING_STATE_TYPE = "uavcan.primitive.scalar.Natural8.1.0"
COMMAND_START_HOMING = 0x0400
"""UNIMOC vendor command (motion commands 0x0400..0x04FF)."""

STATES = ("idle", "searching", "zeroing", "done", "fault")
STATE_TIMEOUT = 2.5
"""Seconds to wait for a state: the drive publishes every 2 s at least."""
STATUS_SUCCESS = 0


class HomingError(RuntimeError):
    """Homing refused or not available; the text says why."""


@dataclass(frozen=True)
class HomingState:
    value: int

    @property
    def name(self) -> str:
        return STATES[self.value] if self.value < len(STATES) else str(self.value)

    @property
    def running(self) -> bool:
        return self.name in ("searching", "zeroing")


class HomingWatch:
    """The latest homing state of each node, by subscription (one subscriber per subject)."""

    def __init__(self, node: Any, on_change: Callable[[int, HomingState], None] | None = None) -> None:
        self._node = node
        self._on_change = on_change
        self._subscribers: dict[int, Any] = {}
        self._states: dict[int, tuple[HomingState, float]] = {}
        self._changed = asyncio.Condition()

    def watch(self, subject_id: int) -> None:
        if subject_id in self._subscribers:
            return
        from cypilot.core.signals import load_type

        subscriber = self._node.make_subscriber(load_type(HOMING_STATE_TYPE), subject_id)

        async def handler(message: Any, transfer: Any) -> None:
            node_id = transfer.source_node_id
            if node_id is None:
                return
            state = HomingState(int(message.value))
            old = self._states.get(node_id)
            self._states[node_id] = state, time.monotonic()
            if self._on_change is not None and (old is None or old[0] != state):
                self._on_change(node_id, state)
            async with self._changed:
                self._changed.notify_all()

        subscriber.receive_in_background(handler)
        self._subscribers[subject_id] = subscriber

    def latest(self, node_id: int) -> HomingState | None:
        found = self._states.get(node_id)
        return found[0] if found is not None else None

    async def wait(
        self,
        node_id: int,
        predicate: Callable[[HomingState], bool] = lambda _: True,
        timeout: float = STATE_TIMEOUT,
        since: float | None = None,
    ) -> HomingState:
        """The first state of ``node_id`` that holds ``predicate`` (received after ``since``)."""

        def found() -> bool:
            entry = self._states.get(node_id)
            return entry is not None and (since is None or entry[1] >= since) and predicate(entry[0])

        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(found), timeout)
            except TimeoutError:
                raise HomingError(f"no homing state of node {node_id}") from None
        return self._states[node_id][0]

    def close(self) -> None:
        for subscriber in self._subscribers.values():
            subscriber.close()
        self._subscribers.clear()


async def start_homing(
    node: Any, node_id: int, subject: int | None, watch: HomingWatch, timeout: float = 1.0
) -> HomingState:
    """START_HOMING on a drive engaged in position control; the state once the run searches.

    ``subject``: the homing state subject of the drive (``DriveTarget.homing``);
    None: the drive has no homing.
    """
    from cypilot.core.session import call_command

    if subject is None:
        raise HomingError(f"node {node_id} has no homing ({HOMING_STATE_ID})")
    watch.watch(subject)
    since = time.monotonic()
    try:
        status, output = await call_command(node, node_id, COMMAND_START_HOMING, "", timeout)
    except TimeoutError as error:
        raise HomingError(str(error)) from error
    if status != STATUS_SUCCESS:
        raise HomingError(f"node {node_id} refused homing: {output or f'status {status}'}")
    # The run may end before the first message (a shaft at the stop); any state after the start counts.
    return await watch.wait(node_id, lambda state: state.name != "idle", since=since)
