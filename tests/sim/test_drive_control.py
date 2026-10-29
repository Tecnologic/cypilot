# SPDX-License-Identifier: Apache-2.0
"""Enabling, the watchdog, trips and recovery against the released UNIMOC simulation."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

from cypilot.core.control import Controller, SetpointMode, Waveform, WaveformKind, find_target
from cypilot.core.registers import RegisterClient
from cypilot.core.session import Session
from cypilot.core.signals import Recorder, sources, subscribe
from cypilot.core.status import Chip
from cypilot.core.transport import parse_interface
from cypilot.simulation import SimInstance
from sim.conftest import Ctl

DRIVE = 10


async def _engaged(session: Session) -> bool:
    node = next(n for n in session.nodes() if n.node_id == DRIVE)
    return Chip.ENGAGED in node.chips


async def _wait(session: Session, engaged: bool, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _engaged(session) == engaged:
            return True
        await asyncio.sleep(0.05)
    return False


def test_engage_setpoint_and_the_watchdog_disengages_a_drive_whose_owner_stalls(
    sim: SimInstance, dsdl: Path
) -> None:
    async def go() -> None:
        async with Session(parse_interface(sim.slcan_interfaces[0])) as session:
            records: list[str] = []
            session.add_record_handler(lambda record: records.append(record.text))
            await session.wait_for_node(DRIVE)
            target = await find_target(session, DRIVE)
            assert target.readiness == 1024 + DRIVE
            controller = Controller(session)
            feeding = asyncio.get_running_loop().create_task(_feed(controller))
            await controller.engage(target)
            await controller.wait_engaged(DRIVE)
            controller.set_setpoint(target, SetpointMode.CURRENT, (0.0, 2.0), ramp=0.2)
            await asyncio.sleep(0.5)
            assert await _engaged(session)
            assert "engaged" in records

            feeding.cancel()  # the owner stops: the watchdog puts the drive to sleep
            assert await _wait(session, engaged=False, timeout=3.0)
            assert controller.watchdog_tripped
            await controller.close()

    asyncio.run(go())


def test_a_square_wave_of_the_current_reaches_the_control_cycle(sim: SimInstance, dsdl: Path) -> None:
    async def go() -> None:
        async with Session(parse_interface(sim.slcan_interfaces[0])) as session:
            await session.wait_for_node(DRIVE)
            client = RegisterClient(session.node, DRIVE, timeout=1.0)
            try:
                registers = await client.scan(["uavcan.pub.telemetry.id", "uavcan.pub.telemetry.type"])
            finally:
                client.close()
            (telemetry,) = sources(registers, DRIVE)
            recorder = Recorder()
            subscriber = subscribe(session.node, telemetry, recorder)
            target = await find_target(session, DRIVE)
            controller = Controller(session)
            feeding = asyncio.get_running_loop().create_task(_feed(controller))
            await controller.engage(target)
            await controller.wait_engaged(DRIVE)
            square = Waveform(WaveformKind.SQUARE, (0.0, 0.0), (0.0, 1.5), period=0.8, cycles=2)
            controller.set_waveform(target, SetpointMode.CURRENT, square)
            await asyncio.sleep(2.0)
            subject = target.setpoints[SetpointMode.CURRENT]
            assert controller.waveform_of(SetpointMode.CURRENT, subject) is None
            assert await _engaged(session)
            _, i_q_ref = recorder.series(telemetry.key, "i_q_ref")
            levels = [round(value, 1) for value in i_q_ref if round(value, 1) in (0.0, 1.5)]
            changes = [b for a, b in itertools.pairwise(levels) if a != b]
            assert changes[:4] == [1.5, 0.0, 1.5, 0.0], i_q_ref
            assert levels[-1] == 0.0
            subscriber.close()
            feeding.cancel()
            await controller.close()

    asyncio.run(go())


def test_a_trip_refuses_the_engage_and_the_recovery_clears_it(ctl: Ctl) -> None:
    assert ctl.run("set", str(DRIVE), "unimoc.sim.dc_link_voltage", "59")[0] == 0  # trips at 58 V
    ctl.wait_for(lambda nodes: any("tripped" in n["state"] for n in nodes))
    code, _ = ctl.run("run", str(DRIVE), "--for", "1")
    assert code == 2  # refused: a trip is latched
    assert ctl.run("set", str(DRIVE), "unimoc.sim.dc_link_voltage", "48")[0] == 0
    result = ctl.json("recover", str(DRIVE))
    assert result["ready"]
    assert "STANDBY sent (acknowledges the trip)" in result["steps"]
    run = ctl.json("run", str(DRIVE), "--current", "0,1", "--for", "1")
    assert run["state"] == ["disabled"]


def test_run_with_a_square_wave_from_the_command_line(ctl: Ctl) -> None:
    args = ["--current", "0,1", "--wave", "square", "--low", "0,-1", "--period", "0.4", "--edge", "0.05"]
    run = ctl.json("run", str(DRIVE), *args, "--for", "1")
    assert run["state"] == ["disabled"]
    code, _ = ctl.run("run", str(DRIVE), "--wave", "step", "--for", "1")
    assert code != 0  # a waveform needs a setpoint as its high value


def test_stop_reaches_every_drive(ctl: Ctl) -> None:
    assert ctl.json("stop")["sleep_sent_to"] == [DRIVE]


def test_an_emergency_stop_is_refused_and_needs_a_restart(ctl: Ctl) -> None:
    assert ctl.json("command", str(DRIVE), "emergency-stop")["status"] == 0
    ctl.wait_for(lambda nodes: any("E-stop latched" in n["state"] for n in nodes))
    code, _ = ctl.run("run", str(DRIVE), "--for", "1")
    assert code == 2
    result = ctl.json_rc(1, "recover", str(DRIVE))
    assert not result["ready"] and "restart" in result["steps"][-1]
    # The restart itself re-executes the simulation and its pseudo terminal goes away,
    # so it is covered by the unit test of the recovery.


async def _feed(controller: Controller) -> None:
    while True:
        controller.feed()
        await asyncio.sleep(0.05)
