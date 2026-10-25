# SPDX-License-Identifier: Apache-2.0
"""Enabling drives, setpoints, stop all and recovery (``docs/plan.md``, section 7).

cypilot is no safety device. The rules this module keeps:

- It never engages a drive by itself: a drive is engaged only by
  :meth:`Controller.engage`, called on an explicit request.
- Readiness ``ENGAGED`` is only valid while it is refreshed: the drive
  disengages without it after ``unimoc.control.command_timeout`` (0.5 s). The
  keep-alive here refreshes it every 50 ms for the drives the user engaged.
- **Watchdog:** the owner (the web app's control lease, the CLI's loop) must
  call :meth:`Controller.feed` at least every ``watchdog_timeout``. If it
  stops, the keep-alive sends ``SLEEP`` to every engaged drive and stops; a
  lost browser cannot keep a drive engaged.
- :meth:`Controller.stop_all` sends ``SLEEP`` on every readiness subject it
  knows and zero setpoints at once, without a ramp.
- It does not publish on a readiness subject another node publishes on (the
  VDC in the cart): no two masters.

Readiness is per drive (UNIMOC v0.2.0: subject 1024 + node-ID by default), so
one drive is enabled alone. The setpoint subjects are shared by default: a
setpoint reaches every engaged drive that subscribes its subject.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cypilot.core.capabilities import SETPOINTS, ports
from cypilot.core.registers import RegisterClient
from cypilot.core.session import Session
from cypilot.core.status import Chip, VendorStatus

KEEPALIVE_PERIOD = 0.05
"""Seconds between two readiness and setpoint publications (20 Hz)."""
WATCHDOG_TIMEOUT = 0.3
"""Seconds without :meth:`Controller.feed` after which the engaged drives are put to sleep."""
STANDBY_HOLD = 1.0
"""Seconds of STANDBY after a disable or a trip acknowledgement, then the drive is left alone."""
STOP_REPEATS = 3
"""SLEEP is sent this many times, one keep-alive period apart, on a stop."""
ENGAGE_TIMEOUT = 2.5
"""Seconds to wait for the engaged bit of the heartbeat (1 Hz)."""

SLEEP = 0
STANDBY = 2
ENGAGED = 3

RESTART_COMMAND = 65535
EMERGENCY_STOP_COMMAND = 65531


class ControlError(RuntimeError):
    """A drive cannot be enabled or recovered; the text says why."""


class SetpointMode(enum.Enum):
    """Setpoint kinds by their UNIMOC port name (``uavcan.sub.<name>.id``)."""

    CURRENT = "current_setpoint"  # i_d, i_q [A]
    TORQUE = "torque_setpoint"  # [N m]
    SPEED = "speed_setpoint"  # mechanical [rad/s]
    POSITION = "position_setpoint"  # mechanical [rad] from home

    @property
    def components(self) -> int:
        return 2 if self is SetpointMode.CURRENT else 1


@dataclass(frozen=True)
class DriveTarget:
    """Where one drive listens: its readiness subject and its setpoint subjects."""

    node_id: int
    readiness: int
    setpoints: dict[SetpointMode, int] = field(default_factory=dict)


async def find_target(session: Session, node_id: int) -> DriveTarget:
    """Reads the port registers of a drive (not the whole register list)."""
    names = ["uavcan.sub.readiness.id", *(f"uavcan.sub.{name}.id" for name in SETPOINTS)]
    client = RegisterClient(session.node, node_id, timeout=1.0)
    try:
        registers = await client.scan(names)
    finally:
        client.close()
    found = {p.name: p.port_id for p in ports(registers, node_id) if p.kind == "sub" and p.enabled}
    readiness = found.get("readiness")
    if readiness is None:
        raise ControlError(f"node {node_id} has no readiness subscription; it cannot be enabled from the bus")
    setpoints = {mode: int(port) for mode in SetpointMode if (port := found.get(mode.value)) is not None}
    return DriveTarget(node_id, int(readiness), setpoints)


@dataclass
class _Setpoint:
    mode: SetpointMode
    subject: int
    target: tuple[float, ...]
    value: tuple[float, ...]
    ramp: float  # seconds from the present value to the target; 0: at once


class Controller:
    """The readiness keep-alive and the setpoints of one session; runs on the session's loop."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], float] = time.monotonic,
        watchdog_timeout: float = WATCHDOG_TIMEOUT,
        on_watchdog: Callable[[], None] | None = None,
    ) -> None:
        """``watchdog_timeout``: seconds without :meth:`feed` until the drives are put to sleep (the web
        app uses its control lease time); ``on_watchdog`` is called when that happened."""
        self._session = session
        self._clock = clock
        self.watchdog_timeout = watchdog_timeout
        self._on_watchdog = on_watchdog
        self._publishers: dict[tuple[str, int], Any] = {}
        self._engaged: dict[int, DriveTarget] = {}
        self._standby: dict[int, tuple[DriveTarget, float]] = {}
        self._known: dict[int, DriveTarget] = {}
        self._setpoints: dict[tuple[SetpointMode, int], _Setpoint] = {}
        self._last_feed = clock()
        self._task: asyncio.Task[None] | None = None
        self.watchdog_tripped = False
        """True after the watchdog put the drives to sleep; cleared by the next engage."""

    # --- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._keepalive())

    async def close(self) -> None:
        """Puts every engaged drive to sleep and stops the keep-alive."""
        await self.stop_all()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for publisher in self._publishers.values():
            publisher.close()
        self._publishers.clear()

    def feed(self) -> None:
        """The owner is alive; call at least every :attr:`watchdog_timeout` (thread-safe)."""
        self._last_feed = self._clock()

    @property
    def known(self) -> dict[int, DriveTarget]:
        """The drives this controller has addressed, by node-ID."""
        return dict(self._known)

    @property
    def engaged(self) -> set[int]:
        """Node-IDs this controller keeps engaged."""
        return set(self._engaged)

    # --- Drives ----------------------------------------------------------------

    async def engage(self, target: DriveTarget) -> None:
        """Starts the ENGAGED keep-alive for a drive after checking that nothing speaks against it."""
        node = next((n for n in self._session.nodes() if n.node_id == target.node_id), None)
        if node is None:
            raise ControlError(f"node {target.node_id} is offline")
        status = VendorStatus.decode(node.vendor_code) if node.is_unimoc else None
        refusals = {
            Chip.E_STOP: "the emergency stop is latched; recover it (restart) first",
            Chip.TRIPPED: "a trip is latched; recover it (STANDBY) first",
            Chip.FAULT: "the power stage reports a fault; recover it first",
            Chip.BLOCKED: "the external enable is missing (E-stop loop open)",
            Chip.LOCAL_CONTROL: "the drive is under local control (e-bike); it engages by itself",
        }
        for chip in node.chips:
            if chip in refusals:
                raise ControlError(f"node {target.node_id}: {refusals[chip]}")
        if status is None:
            raise ControlError(f"node {target.node_id} is no UNIMOC drive")
        other = self.foreign_publisher(target.readiness)
        if other is not None:
            raise ControlError(
                f"node {other} publishes the readiness of node {target.node_id} "
                f"(subject {target.readiness}); cypilot does not take over from another master"
            )
        self._known[target.node_id] = target
        self._standby.pop(target.node_id, None)
        self.watchdog_tripped = False
        self.feed()
        self._engaged[target.node_id] = target
        self.start()
        await self._publish_readiness(target.readiness, ENGAGED)

    async def wait_engaged(self, node_id: int, timeout: float = ENGAGE_TIMEOUT) -> None:
        """Waits for the engaged bit; on a timeout the error names the likely reason."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            node = next((n for n in self._session.nodes() if n.node_id == node_id), None)
            if node is not None and VendorStatus.decode(node.vendor_code).engaged:
                return
            await asyncio.sleep(0.05)
        node = next((n for n in self._session.nodes() if n.node_id == node_id), None)
        state = ", ".join(c.value for c in node.chips) if node else "offline"
        raise ControlError(f"node {node_id} did not engage within {timeout} s (state: {state})")

    async def disengage(self, node_id: int, ramp: float = 0.5) -> None:
        """Ramps the setpoints of a drive to zero, then holds STANDBY for :data:`STANDBY_HOLD`."""
        target = self._engaged.get(node_id) or self._known.get(node_id)
        if target is None:
            return
        if node_id in self._engaged:
            mine = [
                s for (mode, subject), s in self._setpoints.items() if target.setpoints.get(mode) == subject
            ]
            for setpoint in mine:
                self._retarget(setpoint, tuple(0.0 for _ in setpoint.target), ramp)
            await asyncio.sleep(ramp)
            # The keep-alive may not have reached zero within the ramp time (timer
            # granularity); the last setpoint the drive sees before STANDBY is zero.
            for setpoint in mine:
                self._retarget(setpoint, setpoint.target, 0.0)
            await self._publish_setpoints()
        self._engaged.pop(node_id, None)
        self._standby[node_id] = (target, self._clock() + STANDBY_HOLD)
        self._drop_unused_setpoints()
        await self._publish_readiness(target.readiness, STANDBY)

    async def acknowledge(self, target: DriveTarget) -> None:
        """STANDBY for :data:`STANDBY_HOLD`: acknowledges a latched trip without releasing the outputs."""
        self._known[target.node_id] = target
        self._engaged.pop(target.node_id, None)
        self._drop_unused_setpoints()
        self._standby[target.node_id] = (target, self._clock() + STANDBY_HOLD)
        self.start()
        await self._publish_readiness(target.readiness, STANDBY)

    async def stop_all(self, extra: Iterable[DriveTarget] = ()) -> None:
        """SLEEP on every known readiness subject and zero setpoints, at once; nothing stays engaged."""
        targets = {t.node_id: t for t in (*self._known.values(), *extra)}
        self._engaged.clear()
        self._standby.clear()
        for setpoint in self._setpoints.values():
            setpoint.value = setpoint.target = tuple(0.0 for _ in setpoint.target)
        for _ in range(STOP_REPEATS):
            for target in targets.values():
                await self._publish_readiness(target.readiness, SLEEP)
            await self._publish_setpoints()
            await asyncio.sleep(KEEPALIVE_PERIOD)
        self._setpoints.clear()

    async def emergency_stop(self, node_ids: Sequence[int]) -> dict[int, int | None]:
        """ExecuteCommand EMERGENCY_STOP (latched until a restart) to the nodes; the status per node."""
        await self.stop_all()
        results: dict[int, int | None] = {}
        for node_id in node_ids:
            try:
                results[node_id] = await self._session.execute_command(
                    node_id, EMERGENCY_STOP_COMMAND, timeout=1.0
                )
            except TimeoutError:
                results[node_id] = None
        return results

    def foreign_publisher(self, subject: int) -> int | None:
        """A node other than cypilot that publishes on ``subject``, from the port lists."""
        for node in self._session.nodes():
            port_list = self._session.port_list(node.node_id)
            if port_list is not None and subject in port_list.publishers:
                return node.node_id
        return None

    # --- Setpoints -------------------------------------------------------------

    def set_setpoint(
        self, target: DriveTarget, mode: SetpointMode, values: Sequence[float], ramp: float = 0.0
    ) -> None:
        """Sends a setpoint while the drive is engaged; ``ramp`` seconds from the present value."""
        if len(values) != mode.components:
            raise ValueError(f"{mode.name.lower()} takes {mode.components} value(s)")
        subject = target.setpoints.get(mode)
        if subject is None:
            raise ControlError(f"node {target.node_id} does not subscribe a {mode.name.lower()} setpoint")
        if target.node_id not in self._engaged:
            raise ControlError(f"node {target.node_id} is not enabled")
        key = (mode, subject)
        present = self._setpoints.get(key)
        if present is None:
            present = _Setpoint(mode, subject, tuple(0.0 for _ in values), tuple(0.0 for _ in values), 0.0)
            self._setpoints[key] = present
        self._retarget(present, tuple(float(v) for v in values), ramp)

    def setpoint_of(self, mode: SetpointMode, subject: int) -> tuple[float, ...] | None:
        present = self._setpoints.get((mode, subject))
        return present.value if present else None

    def _retarget(self, setpoint: _Setpoint, target: tuple[float, ...], ramp: float) -> None:
        setpoint.target = target
        setpoint.ramp = max(0.0, ramp)
        if setpoint.ramp == 0.0:
            setpoint.value = target

    def _drop_unused_setpoints(self) -> None:
        used = {(mode, subject) for t in self._engaged.values() for mode, subject in t.setpoints.items()}
        for key in [key for key in self._setpoints if key not in used]:
            del self._setpoints[key]

    # --- Keep-alive ------------------------------------------------------------

    async def _keepalive(self) -> None:
        last = self._clock()
        while True:
            await asyncio.sleep(KEEPALIVE_PERIOD)
            now = self._clock()
            step, last = now - last, now
            if self._engaged and now - self._last_feed > self.watchdog_timeout:
                self.watchdog_tripped = True
                await self.stop_all()
                if self._on_watchdog is not None:
                    self._on_watchdog()
                continue
            for setpoint in self._setpoints.values():
                _ramp(setpoint, step)
            for target in self._engaged.values():
                await self._publish_readiness(target.readiness, ENGAGED)
            for node_id, (target, until) in list(self._standby.items()):
                if now > until:
                    del self._standby[node_id]
                else:
                    await self._publish_readiness(target.readiness, STANDBY)
            if self._engaged:
                await self._publish_setpoints()

    async def _publish_readiness(self, subject: int, value: int) -> None:
        import reg.udral.service.common

        publisher = self._publisher(reg.udral.service.common.Readiness_0_1, subject)
        await publisher.publish(reg.udral.service.common.Readiness_0_1(value))

    async def _publish_setpoints(self) -> None:
        for setpoint in self._setpoints.values():
            dtype, message = _setpoint_message(setpoint.mode, setpoint.value)
            await self._publisher(dtype, setpoint.subject).publish(message)

    def _publisher(self, dtype: Any, subject: int) -> Any:
        import pycyphal.transport

        key = (dtype.__name__, subject)
        publisher = self._publishers.get(key)
        if publisher is None:
            publisher = self._session.node.make_publisher(dtype, subject)
            publisher.priority = pycyphal.transport.Priority.FAST
            publisher.send_timeout = KEEPALIVE_PERIOD
            self._publishers[key] = publisher
        return publisher


def _ramp(setpoint: _Setpoint, step: float) -> None:
    if setpoint.value == setpoint.target:
        return
    if setpoint.ramp <= 0.0 or step >= setpoint.ramp:
        setpoint.value = setpoint.target
        setpoint.ramp = 0.0
        return
    fraction = step / setpoint.ramp
    setpoint.value = tuple(
        v + (t - v) * fraction for v, t in zip(setpoint.value, setpoint.target, strict=True)
    )
    setpoint.ramp -= step


def _setpoint_message(mode: SetpointMode, value: tuple[float, ...]) -> tuple[Any, Any]:
    if mode is SetpointMode.CURRENT:
        import unimoc.drive

        return unimoc.drive.CurrentSetpoint_0_1, unimoc.drive.CurrentSetpoint_0_1(i_d=value[0], i_q=value[1])
    if mode is SetpointMode.TORQUE:
        import uavcan.si.unit.torque

        return uavcan.si.unit.torque.Scalar_1_0, uavcan.si.unit.torque.Scalar_1_0(value[0])
    if mode is SetpointMode.SPEED:
        import uavcan.si.unit.angular_velocity

        return uavcan.si.unit.angular_velocity.Scalar_1_0, uavcan.si.unit.angular_velocity.Scalar_1_0(
            value[0]
        )
    import uavcan.si.unit.angle

    return uavcan.si.unit.angle.Scalar_1_0, uavcan.si.unit.angle.Scalar_1_0(value[0])


# --- Recovery -------------------------------------------------------------------


@dataclass
class Recovery:
    """The steps a recovery took and whether the node is ready to enable afterwards."""

    steps: list[str] = field(default_factory=list)
    ready: bool = False


async def recover(
    session: Session,
    controller: Controller,
    node_id: int,
    *,
    allow_restart: bool = False,
    timeout: float = 15.0,
) -> Recovery:
    """Runs the procedure that fits the node's state (``docs/plan.md``, section 7.5)."""
    result = Recovery()
    node = _node(session, node_id)
    if node is None:
        result.steps.append("offline: wait for the node to come back")
        return result
    if node.node_id in controller.engaged:
        await controller.disengage(node_id, ramp=0.0)
        result.steps.append("disabled")
    chips = set(node.chips)
    if Chip.E_STOP in chips:
        if not allow_restart:
            result.steps.append("the emergency stop is latched until a restart; confirm the restart")
            return result
        uptime = node.uptime
        await session.execute_command(node_id, RESTART_COMMAND)
        result.steps.append("restart sent")
        if not await _wait(session, node_id, lambda n: n.uptime < uptime and n.name is not None, timeout):
            result.steps.append(f"the node did not come back within {timeout} s")
            return result
        result.steps.append("the node restarted")
    elif chips & {Chip.TRIPPED, Chip.FAULT}:
        target = await find_target(session, node_id)
        await controller.acknowledge(target)
        result.steps.append("STANDBY sent (acknowledges the trip)")
        cleared = await _wait(
            session, node_id, lambda n: not set(n.chips) & {Chip.TRIPPED, Chip.FAULT}, timeout
        )
        if not cleared:
            result.steps.append("the trip or fault did not clear")
            return result
        result.steps.append("trip cleared")
    node = _node(session, node_id)
    if node is None:
        return result
    if Chip.BLOCKED in node.chips:
        result.steps.append(
            "the external enable is missing: check the E-stop loop (not possible from the bus)"
        )
        return result
    blocking = set(node.chips) & {Chip.E_STOP, Chip.TRIPPED, Chip.FAULT}
    result.ready = not blocking
    result.steps.append(
        "ready to enable" if result.ready else "still: " + ", ".join(c.value for c in blocking)
    )
    return result


async def _wait(session: Session, node_id: int, condition: Callable[[Any], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = _node(session, node_id)
        if node is not None and condition(node):
            return True
        await asyncio.sleep(0.1)
    return False


def _node(session: Session, node_id: int) -> Any:
    return next((n for n in session.nodes() if n.node_id == node_id), None)
