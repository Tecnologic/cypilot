# SPDX-License-Identifier: Apache-2.0
"""Two simulated drives on one trigger: the multi-node capture of the web API, with the delay measured.

Each native_sim drive is its own bus of slcan terminals; a bridge relays the
frames between a terminal of each, so both drives and cypilot are on one bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import can
import httpx
import pytest

from cypilot.core.hub import BusHub
from cypilot.core.session import Session
from cypilot.core.transport import parse_interface
from cypilot.simulation import SimInstance
from cypilot.web.app import create_app

pytestmark = pytest.mark.sim

TRIGGER = 220
OPERATOR = {"X-Cypilot-Client": "operator"}


class Bridge:
    """Relays classic CAN frames between two slcan terminals in both directions."""

    def __init__(self, a: str, b: str) -> None:
        self.buses = [can.Bus(interface="slcan", channel=path, bitrate=1_000_000) for path in (a, b)]
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._relay, args=(self.buses[i], self.buses[1 - i]), daemon=True)
            for i in (0, 1)
        ]

    def _relay(self, source: Any, target: Any) -> None:
        while not self._stop.is_set():
            message = source.recv(0.05)
            if message is not None:
                with contextlib.suppress(can.CanError):
                    target.send(message)

    def __enter__(self) -> Bridge:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(2)
        for bus in self.buses:
            bus.shutdown()


async def set_trigger_ports(interface: str, node_id: int) -> None:
    """Writes the shared trigger subject and stores it; it takes effect at the next start."""
    from cypilot.core.registers import RegisterValue

    async with Session(parse_interface(interface), local_node_id=120) as session:
        await session.wait_for_node(node_id)
        client = session.registers(node_id)
        try:
            for name in ("uavcan.pub.cymon_trigger.id", "uavcan.sub.cymon_trigger.id"):
                assert (await client.write(name, RegisterValue("natural16", [TRIGGER]))).accepted
        finally:
            client.close()
        assert await session.execute_command(node_id, 65530) == 0  # store


@pytest.fixture
def drives(sim_executable: Path, tmp_path: Path, dsdl: Path) -> Iterator[tuple[SimInstance, SimInstance]]:
    instances = [SimInstance(sim_executable, tmp_path / f"flash{n}.bin", node_id=n) for n in (10, 11)]
    for instance in instances:  # configure, then start again with the stored ports
        instance.start()
        asyncio.run(set_trigger_ports(instance.slcan_interfaces[0], int(instance.node_id or 0)))
        instance.stop()
    for instance in instances:
        instance.start()
    try:
        yield instances[0], instances[1]
    finally:
        for instance in instances:
            instance.stop()


def test_two_drives_on_one_trigger(drives: tuple[SimInstance, SimInstance]) -> None:
    import cymon

    first, second = drives
    events: list[tuple[int, float]] = []

    async def body() -> None:
        hub = BusHub(lease_time=1.0)
        transport = httpx.ASGITransport(app=create_app(hub))
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://cypilot", timeout=120
            ) as client:
                response = await client.post("/api/connect", json={"interfaces": [first.slcan_interfaces[0]]})
                assert response.status_code == 200, response.text
                session = hub.require_session()
                await session.wait_for_node(10)
                await session.wait_for_node(11, timeout=10)

                subscriber = session.node.make_subscriber(cymon.TriggerEvent_0_1, TRIGGER)

                async def on_event(_: Any, transfer: Any) -> None:
                    events.append((transfer.source_node_id, float(transfer.timestamp.monotonic)))

                subscriber.receive_in_background(on_event)
                trigger = (await client.get("/api/scope/trigger?nodes=10,11")).json()
                assert trigger["shared"] is True and trigger["subject"] == TRIGGER, trigger

                body = {
                    "nodes": {"10": [0, 1], "11": [0]},
                    "trigger_node": 10,
                    "sample_period_us": 200,
                    "num_samples": 50,
                    "pretrigger_samples": 10,
                    "trigger_mode": "immediate",
                    "timeout_s": 20,
                }
                response = await client.post("/api/scope/capture", json=body)
                assert response.status_code == 200, response.text
                captures = response.json()["captures"]
                assert set(captures) == {"10", "11"}
                assert len(captures["10"]["channels"]) == 2 and len(captures["11"]["channels"]) == 1
                assert all(len(c["t"]) == 50 and c["t"][10] == 0.0 for c in captures.values())
                await asyncio.sleep(0.5)
                subscriber.close()
        finally:
            await hub.close()

    terminals = [instance.slcan_interfaces[-1].removeprefix("slcan:") for instance in (first, second)]
    with Bridge(*terminals):
        asyncio.run(body())
    times = dict(events)
    assert {10, 11} <= set(times), events  # the follower published its own event when the trigger reached it
    delay = times[11] - times[10]
    print(f"trigger delay of the second drive: {delay * 1e3:.1f} ms (over the slcan bridge)")
    assert 0 <= delay < 0.1
