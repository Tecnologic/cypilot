# SPDX-License-Identifier: Apache-2.0
"""The homing run against a demo device on a virtual bus: the refusals, a run to done, the disengage."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from demo_bus import DEVICE_ID, demo_bus

from cypilot.core.capabilities import Capability, capabilities
from cypilot.core.control import find_target
from cypilot.core.homing import HomingError, HomingState, HomingWatch, start_homing


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def engage(device: Any) -> None:
    device.engaged = True
    device.readiness_deadline = time.monotonic() + 60.0  # no keep-alive needed here


def test_the_state_names_follow_the_firmware() -> None:
    assert HomingState(1).name == "searching" and HomingState(1).running
    assert HomingState(3).name == "done" and not HomingState(3).running
    assert HomingState(4).name == "fault" and HomingState(9).name == "9"


def test_a_homing_run_to_done_and_its_refusals(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, devices):
            target = await find_target(session, DEVICE_ID)
            assert target.homing == 204
            client = session.registers(DEVICE_ID)
            try:
                assert Capability.HOMING in capabilities(await client.scan(), DEVICE_ID, unimoc=True)
            finally:
                client.close()
            watch = HomingWatch(session.node)
            try:
                # Refused while not engaged, with the drive's reason.
                with pytest.raises(HomingError, match="position control"):
                    await start_homing(session.node, DEVICE_ID, target.homing, watch)
                with pytest.raises(HomingError, match="no homing"):
                    await start_homing(session.node, DEVICE_ID, None, watch)

                engage(devices[0])
                state = await start_homing(session.node, DEVICE_ID, target.homing, watch)
                assert state.name == "searching"
                with pytest.raises(HomingError, match="runs already"):
                    await start_homing(session.node, DEVICE_ID, target.homing, watch)
                done = await watch.wait(DEVICE_ID, lambda state: not state.running)
                assert done.name == "done" and watch.latest(DEVICE_ID) == done

                # A disengage ends a run.
                devices[0].homing_time = 30.0
                await start_homing(session.node, DEVICE_ID, target.homing, watch)
                devices[0].engaged = False
                assert (await watch.wait(DEVICE_ID, lambda state: not state.running)).name == "fault"
            finally:
                watch.close()

    run(body())
