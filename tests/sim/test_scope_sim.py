# SPDX-License-Identifier: Apache-2.0
"""Two simulated drives on one trigger: the multi-node capture of the web API, with the delay measured.

Each native_sim drive is its own bus of slcan terminals; a bridge relays the
frames between a terminal of each, so both drives and cypilot are on one bus.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import serial

from cypilot.core.hub import BusHub
from cypilot.core.session import Session
from cypilot.core.transport import parse_interface
from cypilot.simulation import SimInstance
from cypilot.web.app import create_app

pytestmark = pytest.mark.sim

TRIGGER = 220
OPERATOR = {"X-Cypilot-Client": "operator"}


class Bridge:
    """Relays the frames between two slcan terminals of two drives, as text.

    A drive writes a received frame to a terminal as ``T<id><len><data>\\r``,
    which is also the command to send that frame: the lines pass through as
    they are, in chunks (decoding every frame in Python was too slow for the
    traffic of two drives). Only frame lines pass, so the acknowledgements of
    one drive do not reach the other.
    """

    def __init__(self, a: str, b: str) -> None:
        self.ports = [serial.Serial(path, timeout=0.01) for path in (a, b)]
        self.relayed = [0, 0]
        """Frames relayed from a to b and from b to a."""
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._relay, args=(i,), daemon=True) for i in (0, 1)]

    def _relay(self, index: int) -> None:
        source, target = self.ports[index], self.ports[1 - index]
        pending = b""
        while not self._stop.is_set():
            pending += source.read(4096)
            *lines, pending = pending.split(b"\r")
            frames = [line for line in lines if line[:1] in (b"T", b"D", b"B")]
            if frames:
                target.write(b"\r".join(frames) + b"\r")
                self.relayed[index] += len(frames)

    def __enter__(self) -> Bridge:
        for port in self.ports:
            port.write(b"O\r")  # open the terminal: the drive writes the bus to it from now on
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        for thread in self._threads:
            if thread.ident is not None:  # started
                thread.join(2)
        for port in self.ports:
            port.close()


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
                # First the drive on cypilot's own terminal, then the other one over the bridge (slow on
                # CI: both drives over one classic slcan terminal; the other tests wait 30 s too).
                await session.wait_for_node(10, timeout=30)
                bridge.__enter__()
                await session.wait_for_node(11, timeout=30)

                subscriber = session.node.make_subscriber(cymon.TriggerEvent_0_1, TRIGGER)

                async def on_event(_: Any, transfer: Any) -> None:
                    events.append((transfer.source_node_id, float(transfer.timestamp.monotonic)))

                subscriber.receive_in_background(on_event)
                response = await client.get("/api/scope/trigger?nodes=10,11")
                assert response.status_code == 200, response.text
                trigger = response.json()
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
        except BaseException:
            seen = [(n["node_id"], n["name"]) for n in hub.nodes()] if hub.connected else "not connected"
            print(f"--- cypilot saw {seen}; the bridge relayed {bridge.relayed} frames")
            raise
        finally:
            await hub.close()

    terminals = [instance.slcan_interfaces[-1].removeprefix("slcan:") for instance in (first, second)]
    bridge = Bridge(*terminals)
    try:
        asyncio.run(body())
    except BaseException:
        for instance in (first, second):
            print(f"--- output of drive {instance.node_id}:", *list(instance.output)[-30:], sep="\n")
        raise
    finally:
        bridge.__exit__()
    times = dict(events)
    assert {10, 11} <= set(times), events  # the follower published its own event when the trigger reached it
    delay = times[11] - times[10]
    print(f"trigger delay of the second drive: {delay * 1e3:.1f} ms (over the slcan bridge)")
    assert 0 <= delay < 0.1
