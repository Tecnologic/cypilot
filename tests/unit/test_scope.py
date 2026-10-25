# SPDX-License-Identifier: Apache-2.0
"""cymon captures through the scope client against the demo device (cymon-lib's capture logic)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from demo_bus import DEVICE_ID, demo_bus, new_channel

from cypilot.core.scope import CaptureRequest, ScopeClient, ScopeError


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_info_names_the_services_variables_and_buffer(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            info = await ScopeClient(session).info(DEVICE_ID)
            assert info["services"] == {
                "variables": 120,
                "buffer_info": 121,
                "setup": 122,
                "arm": 123,
                "read": 124,
            }
            assert [v["name"] for v in info["variables"]] == ["sine", "cosine", "ramp", "engaged"]
            assert info["variables"][0]["unit"] == "A"
            assert info["buffer"]["max_frames"] == 1024 and info["buffer"]["tick_period_us"] == 100.0

    run(body())


def test_an_immediate_capture_reads_all_pages(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            scope = ScopeClient(session)
            capture = await scope.capture(
                DEVICE_ID, CaptureRequest(channels=[0, 1, 2], sample_period_us=250, num_samples=100)
            )
            assert capture.sample_period_us == 300.0  # 2.5 ticks round to 3
            assert len(capture.t) == 100 and [len(c) for c in capture.channels] == [100, 100, 100]
            assert capture.t[0] == 0.0 and capture.t[1] == pytest.approx(300e-6)
            assert (await scope.status(DEVICE_ID))["state"] == "complete"
            again = await scope.read(DEVICE_ID)
            assert again.channels == capture.channels

    run(body())


def test_a_rising_trigger_with_pretrigger(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            request = CaptureRequest(
                channels=[0],
                sample_period_us=100,
                num_samples=60,
                pretrigger_samples=20,
                trigger_mode="rising",
                trigger_level=0.5,
            )
            capture = await ScopeClient(session).capture(DEVICE_ID, request)
            sine = capture.channels[0]
            assert capture.t[20] == 0.0
            assert sine[19] < 0.5 <= sine[20]  # the trigger sample is the first one at or above the level

    run(body())


def test_refused_captures_name_the_reason(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            scope = ScopeClient(session)
            with pytest.raises(ScopeError, match="bad channel id"):
                await scope.capture(
                    DEVICE_ID, CaptureRequest(channels=[9], sample_period_us=100, num_samples=10)
                )
            with pytest.raises(ScopeError, match="buffer too small"):
                await scope.capture(
                    DEVICE_ID, CaptureRequest(channels=[0, 1], sample_period_us=100, num_samples=600)
                )
            with pytest.raises(ScopeError, match="unknown trigger mode"):
                await scope.capture(
                    DEVICE_ID,
                    CaptureRequest(channels=[0], sample_period_us=100, num_samples=10, trigger_mode="x"),
                )
            request = CaptureRequest(
                channels=[0],
                sample_period_us=100,
                num_samples=10,
                trigger_mode="rising",
                trigger_level=100.0,
                timeout_s=0.5,
            )
            with pytest.raises(ScopeError, match="did not complete"):
                await scope.capture(DEVICE_ID, request)
            assert (await scope.status(DEVICE_ID))["state"] == "configured"  # disarmed

    run(body())


def test_a_node_without_cymon(dsdl: Path) -> None:
    from cypilot.core.session import Session
    from cypilot.core.transport import parse_interface

    async def body() -> None:
        channel = new_channel()
        async with demo_bus(channel) as (session, _):
            other = Session(parse_interface(channel), local_node_id=50)  # registers, no cymon services
            await other.open()
            try:
                await session.wait_for_node(50)
                with pytest.raises(ScopeError, match="no cymon services"):
                    await ScopeClient(session).info(50)
                with pytest.raises(ScopeError, match="cannot read the port registers"):
                    await ScopeClient(session).info(99)
            finally:
                await other.close()

    run(body())


def test_two_nodes_on_one_trigger(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus(devices=2) as (session, devices):
            scope = ScopeClient(session)
            follower = DEVICE_ID + 1
            ports = await scope.trigger_ports([DEVICE_ID, follower])
            assert ports["shared"] is False and "disabled" in ports["problem"]
            request = CaptureRequest(
                channels=[],
                sample_period_us=100,
                num_samples=60,
                pretrigger_samples=20,
                trigger_mode="rising",
                trigger_level=0.5,
                timeout_s=5,
            )
            with pytest.raises(ScopeError, match="no shared trigger subject"):
                await scope.multi_capture({DEVICE_ID: [0], follower: [1]}, DEVICE_ID, request)

            await scope.set_trigger_ports([DEVICE_ID, follower])
            for node in (DEVICE_ID, follower):  # the ports take effect after a restart
                assert await session.execute_command(node, 65535) == 0
            ports = await scope.trigger_ports([DEVICE_ID, follower])
            assert ports["shared"] is True and ports["subject"] == 220

            captures = await scope.multi_capture({DEVICE_ID: [0], follower: [1, 2]}, DEVICE_ID, request)
            assert set(captures) == {DEVICE_ID, follower}
            trigger, other = captures[DEVICE_ID], captures[follower]
            assert trigger.channels[0][19] < 0.5 <= trigger.channels[0][20]  # on its own condition
            assert len(other.channels) == 2 and len(other.t) == 60 and other.t[20] == 0.0
            assert (await scope.status(follower))["state"] == "complete"  # triggered by the event

            with pytest.raises(ScopeError, match="takes part"):
                await scope.multi_capture({follower: [1]}, DEVICE_ID, request)

            # A follower that misses the trigger event fails soon and says so, not after timeout_s.
            devices[1]._trigger_sub.close()
            devices[1]._trigger_sub = None
            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(ScopeError, match="did not trigger on the trigger event of node 10"):
                await scope.multi_capture({DEVICE_ID: [0], follower: [1]}, DEVICE_ID, request)
            assert loop.time() - start < 4.0 < request.timeout_s * 2

    run(body())
