# SPDX-License-Identifier: Apache-2.0
"""The controller against a fake session: refusals, keep-alive, watchdog, ramps, stop, recovery."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from cypilot.core import control
from cypilot.core.capabilities import PortList
from cypilot.core.control import ENGAGED, SLEEP, STANDBY, ControlError, Controller, DriveTarget, SetpointMode
from cypilot.core.session import NodeSummary
from cypilot.core.status import Health, Mode

TARGET = DriveTarget(10, 1034, {SetpointMode.CURRENT: 107, SetpointMode.TORQUE: 100})


def drive(vendor_code: int = 0, health: Health = Health.NOMINAL, uptime: int = 100) -> NodeSummary:
    return NodeSummary(
        node_id=10,
        health=health,
        mode=Mode.OPERATIONAL,
        vendor_code=vendor_code,
        uptime=uptime,
        name="unimoc",
        software_version=(0, 2),
        hardware_version=(1, 0),
        unique_id=b"",
        vcs_revision=0,
        unimoc=True,
    )


@dataclass
class FakePublisher:
    log: list[tuple[str, int, Any]]
    name: str
    subject: int
    priority: Any = None
    send_timeout: float = 0.0

    async def publish(self, message: Any) -> bool:
        self.log.append((self.name, self.subject, message))
        return True

    def close(self) -> None:
        pass


@dataclass
class FakeNode:
    log: list[tuple[str, int, Any]] = field(default_factory=list)

    def make_publisher(self, dtype: Any, subject: int) -> FakePublisher:
        return FakePublisher(self.log, dtype.__name__, subject)


class FakeSession:
    def __init__(self, nodes: list[NodeSummary]) -> None:
        self.node = FakeNode()
        self._nodes = nodes
        self.port_lists: dict[int, PortList] = {}
        self.commands: list[tuple[int, int]] = []

    def nodes(self) -> list[NodeSummary]:
        return list(self._nodes)

    def set_nodes(self, nodes: list[NodeSummary]) -> None:
        self._nodes = nodes

    def port_list(self, node_id: int) -> PortList | None:
        return self.port_lists.get(node_id)

    async def execute_command(
        self, node_id: int, command: int, parameter: str = "", timeout: float = 2.0
    ) -> int:
        self.commands.append((node_id, command))
        return 0

    def readiness(self) -> list[int]:
        return [int(m.value) for name, subject, m in self.node.log if name.startswith("Readiness")]

    def setpoints(self) -> list[tuple[float, float]]:
        return [(m.i_d, m.i_q) for name, _, m in self.node.log if name.startswith("CurrentSetpoint")]


@pytest.fixture(autouse=True)
def _dsdl(dsdl: Path) -> Iterator[None]:
    yield


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    ("vendor_code", "health", "reason"),
    [
        (0x20, Health.CAUTION, "emergency stop"),
        (0x10, Health.ADVISORY, "trip"),
        (0x02, Health.ADVISORY, "fault"),
        (0x03, Health.NOMINAL, "external enable"),
        (0x80, Health.NOMINAL, "local control"),
    ],
)
def test_engage_is_refused_with_the_reason(vendor_code: int, health: Health, reason: str) -> None:
    async def go() -> None:
        session = FakeSession([drive(vendor_code, health)])
        with pytest.raises(ControlError, match=reason):
            await Controller(session).engage(TARGET)  # type: ignore[arg-type]
        assert session.readiness() == []

    run(go())


def test_engage_is_refused_offline_and_beside_another_master() -> None:
    async def go() -> None:
        with pytest.raises(ControlError, match="offline"):
            await Controller(FakeSession([])).engage(TARGET)  # type: ignore[arg-type]
        session = FakeSession([drive()])
        session.port_lists[20] = PortList(frozenset({1034}), frozenset(), frozenset())
        session._nodes.append(replace(drive(), node_id=20))
        with pytest.raises(ControlError, match="node 20 publishes the readiness"):
            await Controller(session).engage(TARGET)  # type: ignore[arg-type]
        assert session.readiness() == []

    run(go())


def test_keepalive_refreshes_engaged_while_fed_and_the_watchdog_puts_the_drive_to_sleep() -> None:
    async def go() -> None:
        session = FakeSession([drive()])
        controller = Controller(session)  # type: ignore[arg-type]
        await controller.engage(TARGET)
        for _ in range(8):  # 0.4 s, fed
            controller.feed()
            await asyncio.sleep(0.05)
        engaged = session.readiness().count(ENGAGED)
        assert engaged >= 5 and SLEEP not in session.readiness()
        await asyncio.sleep(0.6)  # not fed: the watchdog trips after 0.3 s
        assert controller.watchdog_tripped
        assert controller.engaged == set()
        assert session.readiness()[-control.STOP_REPEATS :] == [SLEEP] * control.STOP_REPEATS
        await controller.close()

    run(go())


def test_setpoints_ramp_and_disengage_ramps_to_zero_then_standby() -> None:
    async def go() -> None:
        session = FakeSession([drive()])
        controller = Controller(session)  # type: ignore[arg-type]
        with pytest.raises(ControlError, match="not enabled"):
            controller.set_setpoint(TARGET, SetpointMode.CURRENT, (0.0, 4.0))
        await controller.engage(TARGET)
        with pytest.raises(ValueError, match="2 value"):
            controller.set_setpoint(TARGET, SetpointMode.CURRENT, (4.0,))
        with pytest.raises(ControlError, match="speed"):
            controller.set_setpoint(TARGET, SetpointMode.SPEED, (1.0,))
        controller.set_setpoint(TARGET, SetpointMode.CURRENT, (0.0, 4.0), ramp=0.3)
        for _ in range(10):
            controller.feed()
            await asyncio.sleep(0.05)
        i_q = [q for _, q in session.setpoints()]
        assert 0.0 < i_q[0] < 4.0  # ramping
        assert i_q[-1] == pytest.approx(4.0)
        assert all(b >= a for a, b in itertools.pairwise(i_q))

        feeding = asyncio.get_running_loop().create_task(_feed(controller))
        await controller.disengage(10, ramp=0.2)
        feeding.cancel()
        assert session.setpoints()[-1][1] == pytest.approx(0.0, abs=1e-6)
        assert controller.engaged == set()
        assert session.readiness()[-1] == STANDBY
        await asyncio.sleep(control.STANDBY_HOLD + 0.2)
        count = len(session.readiness())
        await asyncio.sleep(0.2)
        assert len(session.readiness()) == count  # left alone after the STANDBY hold
        await controller.close()

    run(go())


def test_stop_all_sends_sleep_and_zero_setpoints_at_once() -> None:
    async def go() -> None:
        session = FakeSession([drive()])
        controller = Controller(session)  # type: ignore[arg-type]
        await controller.engage(TARGET)
        controller.set_setpoint(TARGET, SetpointMode.CURRENT, (0.0, 4.0))
        other = DriveTarget(11, 1035)
        await controller.stop_all([other])
        sleeps = [(subject, m.value) for name, subject, m in session.node.log if name.startswith("Readiness")]
        assert sleeps.count((1034, SLEEP)) == control.STOP_REPEATS
        assert sleeps.count((1035, SLEEP)) == control.STOP_REPEATS
        assert session.setpoints()[-1] == (0.0, 0.0)
        assert controller.engaged == set()
        await controller.close()

    run(go())


def test_emergency_stop_stops_and_sends_the_command() -> None:
    async def go() -> None:
        session = FakeSession([drive()])
        controller = Controller(session)  # type: ignore[arg-type]
        await controller.engage(TARGET)
        assert await controller.emergency_stop([10, 11]) == {10: 0, 11: 0}
        assert session.commands == [
            (10, control.EMERGENCY_STOP_COMMAND),
            (11, control.EMERGENCY_STOP_COMMAND),
        ]
        assert controller.engaged == set()
        await controller.close()

    run(go())


def test_recovery_of_an_emergency_stop_needs_the_restart() -> None:
    async def go() -> None:
        session = FakeSession([drive(0x20, Health.CAUTION, uptime=100)])
        controller = Controller(session)  # type: ignore[arg-type]
        refused = await control.recover(session, controller, 10)  # type: ignore[arg-type]
        assert not refused.ready and "restart" in refused.steps[-1]
        assert session.commands == []

        async def restarted() -> None:
            await asyncio.sleep(0.3)
            session.set_nodes([drive(0, uptime=1)])

        asyncio.get_running_loop().create_task(restarted())
        result = await control.recover(session, controller, 10, allow_restart=True, timeout=3)  # type: ignore[arg-type]
        assert session.commands == [(10, control.RESTART_COMMAND)]
        assert result.ready and result.steps[-1] == "ready to enable"
        await controller.close()

    run(go())


def test_recovery_of_a_blocked_drive_explains_the_e_stop_loop() -> None:
    async def go() -> None:
        session = FakeSession([drive(0x03)])
        result = await control.recover(session, Controller(session), 10)  # type: ignore[arg-type]
        assert not result.ready and "E-stop loop" in result.steps[-1]

    run(go())


async def _feed(controller: Controller) -> None:
    while True:
        controller.feed()
        await asyncio.sleep(0.05)
