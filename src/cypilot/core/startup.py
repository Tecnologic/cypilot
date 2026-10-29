# SPDX-License-Identifier: Apache-2.0
"""The hardware startup aid of a UNIMOC drive: commissioning commands and status message.

UNIMOC's startup aid (``HwStartup``, README "Hardware Bring-Up Guide") is a
step-by-step bring-up of a new board: phase 1 without a motor (offsets,
noise, forced duties, DC link, gate driver), the CONNECT_MOTOR barrier, phase
2 with the motor (ADC trigger sweep, current-sense gain), then DONE, where
the drive applies the calibration (not stored to flash). It never advances on
its own; the operator continues each step and enters the external meter
readings.

The bus contract (UNIMOC ``unimoc.drive.CommissioningStatus.0.1``):

- **Actions** are ``uavcan.node.ExecuteCommand`` codes: START_STARTUP_AID,
  CONTINUE (parameter ``motor-connected`` at CONNECT_MOTOR), SET_READING
  (``vdc=48.1``, ``current=0.85``) and ABORT. The answer says at once whether
  the drive took the action; a refusal carries the reason in its output text.
- **Progress and completion** are the status message on the subject of
  ``uavcan.pub.commissioning.id``, published on every change, at 2 Hz in a
  run and at 0.5 Hz otherwise: the step, its state (measuring, waiting,
  needs a reading, done, failed, aborted), what it needs, the progress, one
  bit per passed and per failed step and the reason of a fault.
- **Values** stay registers: the calibration ``unimoc.startup.adc_offset_*``
  and ``gain_*``.

While it runs the aid owns the outputs: the power stage is released by the
aid, not by readiness, so readiness SLEEP does not stop it. :func:`abort_running`
is what a stop does instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cypilot.core.registers import Register, RegisterClient, RegisterError, RegisterValue

_logger = logging.getLogger(__name__)

PREFIX = "unimoc.startup."
CALIBRATION = tuple(
    PREFIX + name
    for name in (
        "adc_offset_a",
        "adc_offset_b",
        "adc_offset_c",
        "gain_a",
        "gain_b",
        "gain_c",
        "gain_vdc",
    )
)
"""The persistent calibration registers the drive writes at DONE."""

STATUS_ID = "uavcan.pub.commissioning.id"
"""The subject of the status message; a node with this register has the startup aid."""
STATUS_TYPE = "unimoc.drive.CommissioningStatus.0.1"

COMMAND_START = 0x0200
COMMAND_CONTINUE = 0x0202
COMMAND_SET_READING = 0x0203
COMMAND_ABORT = 0x0204
"""The commissioning commands (``COMMAND_*`` of ``unimoc.drive.CommissioningStatus.0.1``)."""
MOTOR_CONNECTED = "motor-connected"

LEGACY_RUN = PREFIX + "run"
"""The run register of UNIMOC up to v0.3.0: a stop still aborts such a drive's aid with it."""

STATUS_SUCCESS = 0
STATUS_BAD_COMMAND = 3

STEPS = (
    "IDLE",
    "PWM_DISABLE",
    "ADC_OFFSET_CAL",
    "ADC_NOISE_FLOOR",
    "DUTY_FORCE_LOW",
    "DUTY_FORCE_MID",
    "DUTY_FORCE_HIGH",
    "DC_LINK_VOLTAGE_CHECK",
    "GATE_DRIVER_ENABLE_CHECK",
    "CONNECT_MOTOR",
    "PHASE_ADC_ALIGNMENT",
    "CURRENT_SENSE_CALIBRATION",
    "DONE",
    "FAULT",
)
"""The steps by number, as UNIMOC's ``FsmState`` (``src/startup/startup_results.hpp``)."""
IDLE, DC_LINK_VOLTAGE_CHECK, CONNECT_MOTOR, CURRENT_SENSE_CALIBRATION, DONE, FAULT = (
    STEPS.index(name)
    for name in (
        "IDLE",
        "DC_LINK_VOLTAGE_CHECK",
        "CONNECT_MOTOR",
        "CURRENT_SENSE_CALIBRATION",
        "DONE",
        "FAULT",
    )
)

STATES = ("idle", "measuring", "waiting", "needs reading", "done", "failed", "aborted")
"""``state`` of the status message by number."""
MEASURING, WAITING, NEEDS_READING = 1, 2, 3
NEEDS = (None, "vdc", "current", MOTOR_CONNECTED, "shaft-free", "one-turn")
"""``needs`` of the status message by number: a reading (SET_READING) or a confirmation (CONTINUE)."""
FAULTS = ("", "aborted", "emergency stop", "overcurrent", "trip", "enable lost", "apply failed")
"""``fault`` of the status message by number."""
PROCEDURE_STARTUP_AID = 1

PROMPTS = {
    "PWM_DISABLE": "50 % duty on all phases, no motor connected. Check the gate-driver outputs with a scope.",
    "ADC_OFFSET_CAL": "Measuring the zero offsets of the current sense (1024 samples).",
    "ADC_NOISE_FLOOR": "Measuring the noise of the current sense.",
    "DUTY_FORCE_LOW": "5 % duty on all phases: check the phase voltages with a scope.",
    "DUTY_FORCE_MID": "50 % duty on all phases: check the phase voltages with a scope.",
    "DUTY_FORCE_HIGH": "95 % duty on all phases: check the phase voltages with a scope.",
    "DC_LINK_VOLTAGE_CHECK": "Measure the DC-link voltage with a multimeter and enter it, then continue.",
    "GATE_DRIVER_ENABLE_CHECK": "Checking the response of the current sense to a duty imbalance.",
    "CONNECT_MOTOR": "Phase 1 done, the outputs are off. Connect the motor (U, V, W and PE) without a power "
    "cycle of the logic (it would end the run), then continue: phase 2 puts voltage on the motor. The shaft "
    "must turn freely.",
    "PHASE_ADC_ALIGNMENT": "Sweeping the ADC trigger with current in phase A.",
    "CURRENT_SENSE_CALIBRATION": "Measure the phase-A current with a clamp meter, enter it, then continue.",
    "DONE": "The calibration is applied. Store it to keep it after a restart.",
    "FAULT": "Ended: by the user, an emergency stop, an overcurrent, a trip or a lost enable. "
    "The outputs are off.",
}
"""What the operator does in each step (the drive's log has the measured values)."""

STATUS_TIMEOUT = 2.5
"""Seconds to wait for a status message: the idle period of the drive is 2 s."""


class StartupError(RuntimeError):
    """The node has no startup aid, or it refused a request (with the drive's reason)."""


@dataclass(frozen=True)
class StartupState:
    """One status message of the startup aid."""

    step: int
    state: int
    needs_code: int = 0
    progress: int = 0
    passed_bits: int = 0
    failed_bits: int = 0
    fault: int = 0
    procedure: int = PROCEDURE_STARTUP_AID

    @property
    def running(self) -> bool:
        return self.state in (MEASURING, WAITING, NEEDS_READING)

    @property
    def measuring(self) -> bool:
        return self.state == MEASURING

    @property
    def step_name(self) -> str:
        return STEPS[self.step] if 0 <= self.step < len(STEPS) else f"step {self.step}"

    @property
    def state_name(self) -> str:
        return STATES[self.state] if 0 <= self.state < len(STATES) else f"state {self.state}"

    @property
    def needs(self) -> str | None:
        """What the step waits for: ``"vdc"``, ``"current"``, ``"motor-connected"`` or None."""
        return NEEDS[self.needs_code] if 0 <= self.needs_code < len(NEEDS) else None

    @property
    def reading(self) -> str | None:
        """The external reading the step waits for or will wait for: ``"vdc"``, ``"current"`` or None.

        The current reading may come before the step has measured, so a tool
        asks for it in the whole step.
        """
        if not self.running:
            return None
        return {DC_LINK_VOLTAGE_CHECK: "vdc", CURRENT_SENSE_CALIBRATION: "current"}.get(self.step)

    @property
    def fault_name(self) -> str:
        return FAULTS[self.fault] if 0 <= self.fault < len(FAULTS) else f"fault {self.fault}"

    @property
    def prompt(self) -> str:
        if self.step == FAULT and self.fault_name:
            return f"Ended: {self.fault_name}. The outputs are off."
        return PROMPTS.get(self.step_name, "")

    def passed(self, step: int) -> bool:
        return bool(self.passed_bits >> step & 1)

    def failed(self, step: int) -> bool:
        return bool(self.failed_bits >> step & 1)

    @classmethod
    def from_message(cls, message: Any) -> StartupState:
        return cls(
            step=int(message.step),
            state=int(message.state),
            needs_code=int(message.needs),
            progress=int(message.progress),
            passed_bits=int(message.passed),
            failed_bits=int(message.failed),
            fault=int(message.fault),
            procedure=int(message.procedure),
        )

    def json(self) -> dict[str, Any]:
        steps = [i for i, name in enumerate(STEPS) if name in PROMPTS and i not in (DONE, FAULT)]
        return {
            "running": self.running,
            "measuring": self.measuring,
            "step": self.step,
            "step_name": self.step_name,
            "state": self.state_name,
            "prompt": self.prompt,
            "needs": self.needs,
            "reading": self.reading,
            "progress": self.progress,
            "phase_2_next": self.needs == MOTOR_CONNECTED,
            "passed": {STEPS[i]: self.passed(i) for i in steps},
            "failed": {STEPS[i]: self.failed(i) for i in steps if self.failed(i)},
            "fault": self.fault_name,
        }


def has_startup(registers: Iterable[Register]) -> bool:
    return any(r.name == STATUS_ID for r in registers)


async def _call(node: Any, node_id: int, command: int, parameter: str, timeout: float) -> tuple[int, str]:
    # Imported here: the session imports the capabilities, and they import this module.
    from cypilot.core.session import call_command

    return await call_command(node, node_id, command, parameter, timeout)


class StatusWatch:
    """The latest status message of the startup aid of each node, by subscription.

    One subscriber per subject (the drives publish on the same subject by
    default); ``on_change`` is called with the node and its state on every
    change.
    """

    def __init__(self, node: Any, on_change: Callable[[int, StartupState], None] | None = None) -> None:
        self._node = node
        self._on_change = on_change
        self._subscribers: dict[int, Any] = {}
        self._states: dict[int, tuple[StartupState, float]] = {}
        self._changed = asyncio.Condition()

    def watch(self, subject_id: int) -> None:
        if subject_id in self._subscribers:
            return
        from cypilot.core.signals import load_type

        subscriber = self._node.make_subscriber(load_type(STATUS_TYPE), subject_id)

        async def handler(message: Any, transfer: Any) -> None:
            node_id = transfer.source_node_id
            if node_id is None:
                return
            state = StartupState.from_message(message)
            old = self._states.get(node_id)
            self._states[node_id] = state, time.monotonic()
            if self._on_change is not None and (old is None or old[0] != state):
                self._on_change(node_id, state)
            async with self._changed:
                self._changed.notify_all()

        subscriber.receive_in_background(handler)
        self._subscribers[subject_id] = subscriber

    def latest(self, node_id: int) -> StartupState | None:
        found = self._states.get(node_id)
        return found[0] if found is not None else None

    def running(self) -> list[int]:
        """The nodes whose last status was a run."""
        return sorted(node_id for node_id, (state, _) in self._states.items() if state.running)

    async def wait(
        self,
        node_id: int,
        predicate: Callable[[StartupState], bool] = lambda _: True,
        timeout: float = STATUS_TIMEOUT,
        since: float | None = None,
    ) -> StartupState:
        """The first status of ``node_id`` that holds ``predicate`` (received after ``since``)."""

        def found() -> bool:
            entry = self._states.get(node_id)
            return entry is not None and (since is None or entry[1] >= since) and predicate(entry[0])

        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(found), timeout)
            except TimeoutError:
                raise StartupError(f"no status of the startup aid of node {node_id}") from None
        return self._states[node_id][0]

    def close(self) -> None:
        for subscriber in self._subscribers.values():
            subscriber.close()
        self._subscribers.clear()


class StartupAid:
    """The startup aid of one drive, on a running pycyphal node.

    ``watch``: a shared :class:`StatusWatch` (the web hub has one); else the
    aid subscribes by itself.
    """

    def __init__(
        self, node: Any, node_id: int, timeout: float = 1.0, watch: StatusWatch | None = None
    ) -> None:
        self._node = node
        self._registers = RegisterClient(node, node_id, timeout=timeout)
        self._timeout = timeout
        self._own_watch = watch is None
        self._watch = watch if watch is not None else StatusWatch(node)
        self._subject: int | None = None
        self.node_id = node_id

    def close(self) -> None:
        self._registers.close()
        if self._own_watch:
            self._watch.close()

    async def subscribe(self) -> None:
        """Subscribes to the status subject of the node (read from its register)."""
        if self._subject is not None:
            return
        try:
            register = await self._registers.read(STATUS_ID)
        except RegisterError as error:
            raise StartupError(f"node {self.node_id}: {STATUS_ID}: {error}") from error
        content = register.value.content
        if register.value.kind == "empty" or not isinstance(content, list) or not content:
            raise StartupError(f"node {self.node_id} has no startup aid (no {STATUS_ID})")
        subject = int(content[0])
        if subject > 8191:
            raise StartupError(f"node {self.node_id} does not publish its commissioning status ({STATUS_ID})")
        self._watch.watch(subject)
        self._subject = subject

    async def state(self, timeout: float = STATUS_TIMEOUT) -> StartupState:
        """The latest status; waits for the first one."""
        await self.subscribe()
        latest = self._watch.latest(self.node_id)
        return latest if latest is not None else await self._watch.wait(self.node_id, timeout=timeout)

    async def wait(
        self, predicate: Callable[[StartupState], bool], timeout: float = STATUS_TIMEOUT
    ) -> StartupState:
        """The first status that holds ``predicate``."""
        await self.subscribe()
        return await self._watch.wait(self.node_id, predicate, timeout)

    async def calibration(self) -> dict[str, float]:
        """The calibration registers (offsets [A], gain corrections), as the drive applied them at DONE."""
        found = await self._registers.scan(list(CALIBRATION))
        return {r.name.removeprefix(PREFIX): float(_first(r)) for r in found if r.value.kind != "empty"}

    async def start(self) -> StartupState:
        """Starts a run from IDLE, DONE or FAULT; the drive releases its outputs at neutral duty."""
        await self.subscribe()
        await self._command(COMMAND_START, refused="refused to start the startup aid")
        return await self._watch.wait(self.node_id, lambda state: state.running)

    async def next_step(self, *, motor_connected: bool = False) -> StartupState:
        """Continues with the next step once the present one has measured; the state before.

        Out of CONNECT_MOTOR into phase 2 only with ``motor_connected``: the
        drive refuses a continue without it there, and refuses one while a step
        measures, so a second click meant for the step before cannot cross
        the barrier.
        """
        state = await self.state()
        if not state.running:
            raise StartupError(f"the startup aid of node {self.node_id} is not running")
        if state.needs == MOTOR_CONNECTED and not motor_connected:
            raise StartupError("phase 2 puts voltage on the motor: confirm that the motor is connected")
        parameter = MOTOR_CONNECTED if state.needs == MOTOR_CONNECTED else ""
        since = time.monotonic()
        await self._command(COMMAND_CONTINUE, parameter, refused="did not continue")
        with contextlib.suppress(StartupError):  # the status follows within a spin of the drive
            await self._watch.wait(self.node_id, lambda new: new != state, timeout=1.0, since=since)
        return state

    async def abort(self) -> None:
        """Aborts a run: FAULT, outputs off. A no-op unless a run is active."""
        with contextlib.suppress(StartupError):  # the abort goes out without the status, too
            await self.subscribe()
        since = time.monotonic()
        await self._command(COMMAND_ABORT, refused="refused the abort")
        if self._subject is not None:  # the status follows within a spin of the drive
            with contextlib.suppress(StartupError):
                await self._watch.wait(
                    self.node_id, lambda state: not state.running, timeout=1.0, since=since
                )

    async def set_reading(self, *, current: float | None = None, vdc: float | None = None) -> StartupState:
        """Enters the readings of the clamp meter [A] and of the multimeter [V]."""
        await self.subscribe()
        since = time.monotonic()
        for name, value in (("current", current), ("vdc", vdc)):
            if value is not None:
                await self._command(
                    COMMAND_SET_READING, f"{name}={float(value):g}", refused="refused the reading"
                )
        with contextlib.suppress(StartupError):
            return await self._watch.wait(self.node_id, timeout=0.6, since=since)  # 2 Hz in a run
        return await self.state()

    async def _command(self, command: int, parameter: str = "", *, refused: str) -> str:
        try:
            status, output = await _call(self._node, self.node_id, command, parameter, self._timeout)
        except TimeoutError as error:
            raise StartupError(str(error)) from error
        if status != STATUS_SUCCESS:
            reason = output or f"status {status}"
            raise StartupError(f"node {self.node_id} {refused}: {reason}")
        return output


async def abort_running(node: Any, node_ids: Iterable[int], timeout: float = 0.5) -> list[int]:
    """Sends ABORT to the startup aid of every given node at once; the nodes that took it.

    For the stops: readiness SLEEP does not stop the aid. ABORT is a no-op on
    a drive whose aid does not run. A node that does not answer or has no aid
    is skipped; it must not delay the others. A drive with the run register of
    UNIMOC up to v0.3.0 gets ``run = 0`` instead.
    """

    async def one(node_id: int) -> int | None:
        try:
            status, _ = await _call(node, node_id, COMMAND_ABORT, "", timeout)
            if status == STATUS_SUCCESS:
                return node_id
            if status == STATUS_BAD_COMMAND:
                return await _abort_legacy(node, node_id, timeout)
        except (TimeoutError, RegisterError) as error:
            _logger.debug("node %d: startup abort: %s", node_id, error)
        return None

    stopped = await asyncio.gather(*(one(node_id) for node_id in node_ids))
    return sorted(node_id for node_id in stopped if node_id is not None)


async def _abort_legacy(node: Any, node_id: int, timeout: float) -> int | None:
    registers = RegisterClient(node, node_id, timeout=timeout)
    try:
        run = await registers.read(LEGACY_RUN)
        if run.value.kind == "empty" or not _first(run):
            return None
        await registers.write(LEGACY_RUN, RegisterValue("natural8", [0]))
        return node_id
    finally:
        registers.close()


def _first(register: Register) -> Any:
    content = register.value.content
    return content[0] if isinstance(content, list) and content else 0
