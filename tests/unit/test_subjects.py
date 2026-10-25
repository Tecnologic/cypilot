# SPDX-License-Identifier: Apache-2.0
"""Monitoring any subject and publications with leases, against the demo device on a virtual bus."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from demo_bus import DEVICE_ID, demo_bus, wait_for

from cypilot.core.events import EventHub
from cypilot.core.signals import SignalError, load_type, type_name
from cypilot.core.subjects import PublisherManager, SubjectMonitor

READINESS = "reg.udral.service.common.Readiness.0.1"
TELEMETRY = "uavcan.primitive.array.Real32.1.0"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_types_with_and_without_a_version(dsdl: Path) -> None:
    assert type_name(load_type("uavcan.node.Heartbeat")) == "uavcan.node.Heartbeat.1.0"
    assert type_name(load_type("uavcan.diagnostic.Record")) == "uavcan.diagnostic.Record.1.1"
    assert type_name(load_type("cymon.ReadSamples.0.1")) == "cymon.ReadSamples.0.1"
    with pytest.raises(SignalError):
        load_type("cymon.Nope")
    with pytest.raises(SignalError):
        load_type("nonexistent.Type")


def test_the_demo_device_passes_as_a_unimoc_drive(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            (node,) = session.nodes()
            assert node.node_id == DEVICE_ID and node.name == "org.tecnologic.cypilot.demo"
            assert node.unimoc is True and node.vendor_code == 0  # disabled

    run(body())


def test_the_monitor_streams_batches(dsdl: Path) -> None:
    from cypilot.demo import TELEMETRY_SUBJECT

    async def body() -> None:
        async with demo_bus() as (session, _):
            events = EventHub()
            monitor = SubjectMonitor(session.node, events)
            async with events.listen() as queue:
                sub = monitor.subscribe(TELEMETRY_SUBJECT, TELEMETRY)
                assert monitor.subscribe(TELEMETRY_SUBJECT, TELEMETRY)["id"] == sub["id"]
                event: dict[str, Any] = {}
                while event.get("kind") != "subject":
                    event = await asyncio.wait_for(queue.get(), 3.0)
            assert event["subject_id"] == TELEMETRY_SUBJECT and event["source"][0] == DEVICE_ID
            assert set(event["fields"]) == {"value[0]", "value[1]", "value[2]"}
            assert len(event["t"]) == len(event["fields"]["value[0]"])
            monitor.unsubscribe(sub["id"])
            assert monitor.list() == []
            monitor.close()

    run(body())


def test_a_publication_engages_until_it_stops(dsdl: Path) -> None:
    from cypilot.demo import READINESS_SUBJECT

    async def body() -> None:
        async with demo_bus() as (session, (device,)):
            publishers = PublisherManager(session.node, EventHub())
            engaged = {"value": 3}
            pub = publishers.start(READINESS_SUBJECT, READINESS, engaged, rate_hz=20, lease_s=5, owner="tab")
            await wait_for(lambda: device.engaged)
            assert publishers.stop_all(owner="other") == 0
            publishers.update(pub["id"], {"value": 2})  # STANDBY
            await wait_for(lambda: not device.engaged)
            publishers.update(pub["id"], {"value": 3})
            await wait_for(lambda: device.engaged)
            assert publishers.stop_all(owner="tab") == 1
            await wait_for(lambda: not device.engaged, timeout=2.0)  # the device's own readiness timeout

    run(body())


def test_a_lease_that_is_not_renewed_stops_the_publication(dsdl: Path) -> None:
    from cypilot.demo import READINESS_SUBJECT

    async def body() -> None:
        async with demo_bus() as (session, (device,)):
            events = EventHub()
            publishers = PublisherManager(session.node, events)
            async with events.listen() as queue:
                publishers.start(READINESS_SUBJECT, READINESS, {"value": 3}, rate_hz=20, lease_s=0.3)
                await wait_for(lambda: device.engaged)
                for _ in range(5):  # renewed for a while ...
                    publishers.renew()
                    await asyncio.sleep(0.1)
                assert publishers.list()
                await wait_for(lambda: not publishers.list(), timeout=1.0)  # ... then not
                stops = []
                while not queue.empty():
                    event = queue.get_nowait()
                    if event["kind"] == "publisher" and event["action"] == "stopped":
                        stops.append(event)
            assert stops[0]["reason"] == "lease expired"
            await wait_for(lambda: not device.engaged, timeout=2.0)

    run(body())


def test_bad_publications_are_refused(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            publishers = PublisherManager(session.node, EventHub())
            with pytest.raises(ValueError, match="rate_hz"):
                publishers.start(106, READINESS, {}, rate_hz=5000)
            with pytest.raises(ValueError, match="lease_s"):
                publishers.start(106, READINESS, {}, rate_hz=1, lease_s=100)
            with pytest.raises(SignalError):
                publishers.start(106, "reg.nope.Type.1.0", {}, rate_hz=1)
            with pytest.raises(Exception):  # noqa: B017 - pycyphal raises its own error for unknown fields
                publishers.start(106, READINESS, {"nope": 1}, rate_hz=1)

    run(body())
