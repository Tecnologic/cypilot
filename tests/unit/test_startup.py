# SPDX-License-Identifier: Apache-2.0
"""The startup aid against demo devices on a virtual bus: a run to DONE, the gates, the abort."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from demo_bus import DEVICE_ID, demo_bus, wait_for

from cypilot.core import startup
from cypilot.core.startup import StartupAid, StartupError, StartupState, abort_running


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


async def until_step(aid: StartupAid, step: int) -> StartupState:
    for _ in range(100):
        state = await aid.state()
        if state.step == step:
            return state
        await asyncio.sleep(0.05)
    raise TimeoutError(f"the aid did not reach {startup.STEPS[step]}")


def test_step_names_follow_the_firmware() -> None:
    assert len(startup.STEPS) == 14 and startup.STEPS[startup.DONE] == "DONE"
    assert startup.STEPS[startup.CONNECT_MOTOR] == "CONNECT_MOTOR" and startup.FAULT == 13
    state = StartupState(True, startup.DC_LINK_VOLTAGE_CHECK, (False,) * 14, 0.0, 0.0)
    assert state.needs == "vdc" and "multimeter" in state.prompt
    assert set(state.json()["passed"]) == set(startup.STEPS) - {"IDLE"}


def test_a_run_to_done_with_the_readings_and_the_barrier(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, _):
            aid = StartupAid(session.node, DEVICE_ID)
            try:
                assert (await aid.state()).step == startup.IDLE
                state = await aid.start()
                assert state.running and state.step_name == "PWM_DISABLE"
                for step in range(1, startup.DC_LINK_VOLTAGE_CHECK):
                    await aid.next_step()
                    await until_step(aid, step + 1)

                # The readings gate their steps.
                with pytest.raises(StartupError, match="DC-link voltage"):
                    await aid.next_step()
                await aid.set_external(vdc=48.0)
                await aid.next_step()
                await until_step(aid, startup.DC_LINK_VOLTAGE_CHECK + 1)
                await aid.next_step()
                await until_step(aid, startup.CONNECT_MOTOR)

                # Into phase 2 only with the confirmation; a second click does not cross the barrier.
                with pytest.raises(StartupError, match="motor is connected"):
                    await aid.next_step()
                assert (await aid.state()).step == startup.CONNECT_MOTOR
                await aid.next_step(motor_connected=True)
                await until_step(aid, startup.CONNECT_MOTOR + 1)
                await aid.next_step()
                await until_step(aid, startup.CURRENT_SENSE_CALIBRATION)
                with pytest.raises(StartupError, match="clamp meter"):
                    await aid.next_step()
                assert (await aid.set_external(current=0.85)).ext_current == pytest.approx(0.85)
                await aid.next_step()

                done = await until_step(aid, startup.DONE)
                assert not done.running and all(
                    done.json()["passed"].get(name)
                    for name in ("ADC_OFFSET_CAL", "CURRENT_SENSE_CALIBRATION")
                )
                assert (await aid.calibration())["gain_a"] == pytest.approx(1.02)
                with pytest.raises(StartupError, match="not running"):
                    await aid.next_step()
            finally:
                aid.close()

    run(body())


def test_abort_running_stops_only_running_aids(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus(devices=2) as (session, devices):
            aid = StartupAid(session.node, DEVICE_ID)
            try:
                await aid.start()
                # Node 11 is idle, node 99 is not on the bus: both are skipped.
                assert await abort_running(session.node, [DEVICE_ID, DEVICE_ID + 1, 99]) == [DEVICE_ID]
                state = await aid.state()
                assert not state.running and state.step == startup.FAULT
                assert devices[1].startup.step == startup.IDLE
                # A second abort changes nothing (UNIMOC: no-op outside a run).
                assert await abort_running(session.node, [DEVICE_ID]) == []
                await wait_for(lambda: devices[0].startup.step == startup.FAULT)
            finally:
                aid.close()

    run(body())
