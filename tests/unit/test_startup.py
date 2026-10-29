# SPDX-License-Identifier: Apache-2.0
"""The startup aid against demo devices on a virtual bus: a run to DONE, the refusals, the abort."""

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


async def measured(aid: StartupAid, step: int) -> StartupState:
    """The status once ``step`` has measured."""
    return await aid.wait(lambda state: state.step == step and not state.measuring)


def test_the_status_fields_follow_the_firmware() -> None:
    assert len(startup.STEPS) == 14 and startup.STEPS[startup.DONE] == "DONE"
    assert startup.STEPS[startup.CONNECT_MOTOR] == "CONNECT_MOTOR" and startup.FAULT == 13
    state = StartupState(startup.DC_LINK_VOLTAGE_CHECK, startup.NEEDS_READING, needs_code=1)
    assert state.running and state.needs == "vdc" and state.reading == "vdc" and "multimeter" in state.prompt
    assert set(state.json()["passed"]) == set(startup.STEPS) - {"IDLE", "DONE", "FAULT"}
    # The current reading is asked for in the whole step: it may come before the measurement.
    measuring = StartupState(startup.CURRENT_SENSE_CALIBRATION, startup.MEASURING, progress=40)
    assert measuring.measuring and measuring.needs is None and measuring.reading == "current"
    barrier = StartupState(startup.CONNECT_MOTOR, startup.WAITING, needs_code=3, passed_bits=0b110)
    assert barrier.json()["phase_2_next"] and barrier.passed(1) and not barrier.passed(3)
    ended = StartupState(startup.FAULT, 6, fault=2)
    assert not ended.running and ended.state_name == "aborted" and "emergency stop" in ended.prompt


def test_a_run_to_done_with_the_readings_and_the_barrier(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, devices):
            devices[0].startup.measure_time = 0.5  # long enough to continue too early
            aid = StartupAid(session.node, DEVICE_ID)
            try:
                assert (await aid.state()).step == startup.IDLE
                state = await aid.start()
                assert state.running and state.step_name == "PWM_DISABLE"
                with pytest.raises(StartupError, match="runs already"):
                    await aid.start()

                # A continue while the step measures is refused by the drive, with its reason.
                await measured(aid, 1)
                await aid.next_step()
                await aid.wait(lambda state: state.step == 2)
                with pytest.raises(StartupError, match="still measuring"):
                    await aid.next_step()
                for step in range(2, startup.DC_LINK_VOLTAGE_CHECK):
                    await measured(aid, step)
                    await aid.next_step()

                # The readings gate their steps.
                state = await measured(aid, startup.DC_LINK_VOLTAGE_CHECK)
                assert state.needs == "vdc" and state.state_name == "needs reading"
                with pytest.raises(StartupError, match="meter reading"):
                    await aid.next_step()
                with pytest.raises(StartupError, match="positive"):
                    await aid.set_reading(vdc=-1.0)
                assert (await aid.set_reading(vdc=48.0)).state_name == "waiting"
                await aid.next_step()
                await measured(aid, startup.DC_LINK_VOLTAGE_CHECK + 1)
                await aid.next_step()

                # Into phase 2 only with the confirmation.
                state = await measured(aid, startup.CONNECT_MOTOR)
                assert state.needs == startup.MOTOR_CONNECTED
                with pytest.raises(StartupError, match="motor is connected"):
                    await aid.next_step()
                assert (await aid.state()).step == startup.CONNECT_MOTOR
                await aid.next_step(motor_connected=True)
                await measured(aid, startup.CONNECT_MOTOR + 1)
                await aid.next_step()
                # The clamp-meter reading may come while the step measures.
                await aid.wait(lambda state: state.step == startup.CURRENT_SENSE_CALIBRATION)
                await aid.set_reading(current=0.85)
                await measured(aid, startup.CURRENT_SENSE_CALIBRATION)
                await aid.next_step()

                done = await aid.wait(lambda state: state.step == startup.DONE)
                assert not done.running and done.state_name == "done"
                assert all(done.json()["passed"].values())
                assert (await aid.calibration())["gain_a"] == pytest.approx(1.02)
                with pytest.raises(StartupError, match="not running"):
                    await aid.next_step()
            finally:
                aid.close()

    run(body())


def test_abort_reaches_every_drive_and_the_status_tells_the_reason(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus(devices=2) as (session, devices):
            aid = StartupAid(session.node, DEVICE_ID)
            try:
                await aid.start()
                # Node 11 is idle (ABORT is a no-op there), node 99 is not on the bus: skipped.
                assert await abort_running(session.node, [DEVICE_ID, DEVICE_ID + 1, 99]) == [
                    DEVICE_ID,
                    DEVICE_ID + 1,
                ]
                state = await aid.wait(lambda state: not state.running)
                assert (
                    state.step == startup.FAULT
                    and state.state_name == "aborted"
                    and state.fault_name == "aborted"
                )
                assert devices[1].startup.step == startup.IDLE
                # A second abort changes nothing (UNIMOC: no-op outside a run).
                await aid.abort()
                await wait_for(lambda: devices[0].startup.step == startup.FAULT)

                # Refused while engaged, with the drive's reason.
                devices[0].engaged = True
                with pytest.raises(StartupError, match="engaged"):
                    await aid.start()
            finally:
                aid.close()

    run(body())


def test_a_node_without_the_status_register_has_no_aid(dsdl: Path) -> None:
    async def body() -> None:
        async with demo_bus() as (session, devices):
            del devices[0].node.registry[startup.STATUS_ID]
            aid = StartupAid(session.node, DEVICE_ID)
            try:
                with pytest.raises(StartupError, match="no startup aid"):
                    await aid.state()
            finally:
                aid.close()

    run(body())
