# SPDX-License-Identifier: Apache-2.0
"""A cypilot session and demo devices on a python-can virtual bus: bus tests without the simulation."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any

from cypilot.core.session import Session
from cypilot.core.transport import make_transport, parse_interface

DEVICE_ID = 10
_channels = itertools.count()


def new_channel() -> str:
    """A virtual bus of its own for each test."""
    return f"virtual:cypilot-test{next(_channels)}"


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def demo_bus(channel: str | None = None, devices: int = 1) -> AsyncIterator[tuple[Session, list[Any]]]:
    """A session (node 127) and ``devices`` demo devices (10, 11, ...) that have answered GetInfo."""
    from cypilot.demo import DemoDevice

    channel = channel or new_channel()
    spec = parse_interface(channel)
    nodes = [DemoDevice(make_transport(spec, DEVICE_ID + index)) for index in range(devices)]
    for node in nodes:
        node.start()
    session = Session(spec)
    await session.open()
    try:
        await session.wait_for_node(DEVICE_ID + devices - 1)
        yield session, nodes
    finally:
        await session.close()
        for node in nodes:
            node.close()
