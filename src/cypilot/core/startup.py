# SPDX-License-Identifier: Apache-2.0
"""The hardware startup aid of a UNIMOC drive (registers ``unimoc.startup.*``).

UNIMOC's startup aid (``HwStartup``, README "Hardware Bring-Up Guide") is a
step-by-step bring-up of a new board: phase 1 without a motor (offsets,
noise, forced duties, DC link, gate driver), the CONNECT_MOTOR barrier, phase
2 with the motor (ADC trigger sweep, current-sense gain), then DONE, where
the drive applies the calibration (not stored to flash). It never advances on
its own; the operator continues each step and enters the external meter
readings. The registers:

- ``run`` (natural8): 1 while it runs; write 1 to start (refused while the
  drive is engaged or without the external enable), 0 to abort.
- ``step`` (natural8): the step (``FsmState``); write 1 to continue. The
  answer is the step, not the written 1: the step changes at the drive's next
  run of the aid, and only once the step has measured.
- ``ext_current_A``, ``ext_vdc_V`` (real32): the clamp-meter and multimeter
  readings of CURRENT_SENSE_CALIBRATION and DC_LINK_VOLTAGE_CHECK.
- ``results`` (natural8[14]): 1 per passed step, indexed by the step.

While it runs the aid owns the outputs: the power stage is released by the
aid, not by readiness, so readiness SLEEP does not stop it. :func:`abort_running`
is what a stop does instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from cypilot.core.registers import Register, RegisterClient, RegisterError, RegisterValue

PREFIX = "unimoc.startup."
RUN = PREFIX + "run"
STEP = PREFIX + "step"
EXT_CURRENT = PREFIX + "ext_current_A"
EXT_VDC = PREFIX + "ext_vdc_V"
RESULTS = PREFIX + "results"
CALIBRATION = tuple(
    PREFIX + name for name in ("adc_offset_a", "adc_offset_b", "gain_a", "gain_b", "gain_vdc")
)
"""The persistent calibration registers the drive writes at DONE."""

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
    "FAULT": "Aborted: by the user, an overcurrent, a trip or a lost enable. The outputs are off.",
}
"""What the operator does in each step (the drive's log has the measured values)."""


class StartupError(RuntimeError):
    """The node has no startup aid, or it refused a request."""


@dataclass(frozen=True)
class StartupState:
    running: bool
    step: int
    passed: tuple[bool, ...]
    ext_current: float
    ext_vdc: float

    @property
    def step_name(self) -> str:
        return STEPS[self.step] if 0 <= self.step < len(STEPS) else f"step {self.step}"

    @property
    def prompt(self) -> str:
        return PROMPTS.get(self.step_name, "")

    @property
    def needs(self) -> str | None:
        """The external reading the step waits for: ``"vdc"``, ``"current"`` or None."""
        return {DC_LINK_VOLTAGE_CHECK: "vdc", CURRENT_SENSE_CALIBRATION: "current"}.get(self.step)

    def json(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "step": self.step,
            "step_name": self.step_name,
            "prompt": self.prompt,
            "needs": self.needs,
            "phase_2_next": self.step == CONNECT_MOTOR,
            "passed": {
                STEPS[i]: p for i, p in enumerate(self.passed) if i < len(STEPS) and STEPS[i] in PROMPTS
            },
            "ext_current": self.ext_current,
            "ext_vdc": self.ext_vdc,
        }


def has_startup(registers: Iterable[Register]) -> bool:
    return any(r.name == RUN for r in registers)


class StartupAid:
    """The startup aid of one drive, on a running pycyphal node."""

    def __init__(self, node: Any, node_id: int, timeout: float = 1.0) -> None:
        self._registers = RegisterClient(node, node_id, timeout=timeout)
        self.node_id = node_id

    def close(self) -> None:
        self._registers.close()

    async def state(self) -> StartupState:
        run, step, current, vdc, results = await self._registers.scan(
            [RUN, STEP, EXT_CURRENT, EXT_VDC, RESULTS]
        )
        if run.value.kind == "empty":
            raise StartupError(f"node {self.node_id} has no startup aid (no {RUN})")
        passed = results.value.content if isinstance(results.value.content, list) else []
        return StartupState(
            running=bool(_first(run)),
            step=int(_first(step)),
            passed=tuple(bool(p) for p in passed),
            ext_current=float(_first(current)),
            ext_vdc=float(_first(vdc)),
        )

    async def calibration(self) -> dict[str, float]:
        """The calibration registers (offsets [A], gain corrections), as the drive applied them at DONE."""
        found = await self._registers.scan(list(CALIBRATION))
        return {r.name.removeprefix(PREFIX): float(_first(r)) for r in found if r.value.kind != "empty"}

    async def start(self) -> StartupState:
        """Starts a run from IDLE, DONE or FAULT; the drive releases its outputs at neutral duty."""
        result = await self._write(RUN, RegisterValue("natural8", [1]))
        if not _first(result):
            raise StartupError(
                f"node {self.node_id} refused to start the startup aid: the drive is engaged, its external "
                "enable is missing or a fault is latched (see its log)"
            )
        return await self.state()

    async def next_step(self, *, motor_connected: bool = False) -> StartupState:
        """Continues with the next step once the present one has measured (the drive keeps the request).

        Out of CONNECT_MOTOR into phase 2 only with ``motor_connected``: the
        barrier passes at once, so a second click meant for the step before
        must not cross it.
        """
        state = await self.state()
        if not state.running:
            raise StartupError(f"the startup aid of node {self.node_id} is not running")
        if state.step == CONNECT_MOTOR and not motor_connected:
            raise StartupError("phase 2 puts voltage on the motor: confirm that the motor is connected")
        if state.needs == "vdc" and not state.ext_vdc > 0.0:
            raise StartupError("enter the DC-link voltage of the multimeter first")
        if state.needs == "current" and not state.ext_current > 0.0:
            raise StartupError("enter the phase-A current of the clamp meter first")
        await self._write(STEP, RegisterValue("natural8", [1]))  # answers with the step
        return state

    async def abort(self) -> None:
        """Aborts a run: FAULT, outputs off. A no-op unless a run is active."""
        await self._write(RUN, RegisterValue("natural8", [0]))

    async def set_external(self, *, current: float | None = None, vdc: float | None = None) -> StartupState:
        """Enters the readings of the clamp meter [A] and of the multimeter [V]."""
        if current is not None:
            await self._write(EXT_CURRENT, RegisterValue("real32", [float(current)]))
        if vdc is not None:
            await self._write(EXT_VDC, RegisterValue("real32", [float(vdc)]))
        return await self.state()

    async def _write(self, name: str, value: RegisterValue) -> Any:
        try:
            result = await self._registers.write(name, value)
        except RegisterError as error:
            raise StartupError(f"node {self.node_id}: {name}: {error}") from error
        if result.register.value.kind == "empty":
            raise StartupError(f"node {self.node_id} has no startup aid (no {name})")
        return result.register


async def abort_running(node: Any, node_ids: Iterable[int]) -> list[int]:
    """Aborts the startup aid of every given node where it runs, at once; the nodes it stopped.

    For the stops: readiness SLEEP does not stop the aid. A node that does not
    answer or has no aid is skipped; it must not delay the others.
    """

    async def one(node_id: int) -> int | None:
        aid = StartupAid(node, node_id, timeout=0.5)
        try:
            run = await aid._registers.read(RUN)
            if run.value.kind == "empty" or not _first(run):
                return None
            await aid.abort()
            return node_id
        except (RegisterError, StartupError):
            return None
        finally:
            aid.close()

    stopped = await asyncio.gather(*(one(node_id) for node_id in node_ids))
    return sorted(node_id for node_id in stopped if node_id is not None)


def _first(register: Register) -> Any:
    content = register.value.content
    return content[0] if isinstance(content, list) and content else 0
