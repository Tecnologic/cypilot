# SPDX-License-Identifier: Apache-2.0
"""The simulation controls of a UNIMOC ``native_sim`` drive (registers ``unimoc.sim.*``).

The plant inputs (shaft mode, load torque, DC-link voltage, ...) are ordinary
registers and are edited from the scan like any other. The controls here need
four names: ``paused`` stops the cycle timer, ``advance`` runs a number of
control cycles (at most :data:`MAX_ADVANCE` per write) before it answers,
``reset`` puts the plant to standstill and the virtual time to 0, and
``cycle_count`` with ``cycle_period_ns`` give the virtual time. The drive's
readiness and setpoint timeouts run in real time, also while paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cypilot.core.registers import Register, RegisterClient, RegisterError, RegisterValue

PREFIX = "unimoc.sim."
PAUSED = PREFIX + "paused"
ADVANCE = PREFIX + "advance"
RESET = PREFIX + "reset"
CYCLE_COUNT = PREFIX + "cycle_count"
CYCLE_PERIOD = PREFIX + "cycle_period_ns"
MAX_ADVANCE = 16000
"""Control cycles one write of ``unimoc.sim.advance`` may run (1 s at 16 kHz)."""
ADVANCE_TIMEOUT = 10.0
"""Seconds to wait for the answer of an advance; it comes after the cycles ran."""


class SimulationError(RuntimeError):
    """The node is no simulation, or it refused a control."""


@dataclass(frozen=True)
class SimStatus:
    paused: bool
    cycle_count: int
    cycle_period_ns: int

    @property
    def virtual_time(self) -> float:
        """Seconds of virtual time since the controller started."""
        return self.cycle_count * self.cycle_period_ns * 1e-9


def is_simulation(registers: list[Register]) -> bool:
    return any(r.name == PAUSED for r in registers)


def chunks(cycles: int) -> list[int]:
    """The writes of ``advance`` that run ``cycles`` control cycles."""
    if cycles < 0:
        raise ValueError("cycles must not be negative")
    full, rest = divmod(cycles, MAX_ADVANCE)
    return [MAX_ADVANCE] * full + ([rest] if rest else [])


class SimControl:
    """The controls of one simulated drive, on a running pycyphal node."""

    def __init__(self, node: Any, node_id: int) -> None:
        self._registers = RegisterClient(node, node_id, timeout=1.0)
        self._slow = RegisterClient(node, node_id, timeout=ADVANCE_TIMEOUT, retries=0)
        self.node_id = node_id

    def close(self) -> None:
        self._registers.close()
        self._slow.close()

    async def status(self) -> SimStatus:
        paused, count, period = await self._registers.scan([PAUSED, CYCLE_COUNT, CYCLE_PERIOD])
        if paused.value.kind == "empty":
            raise SimulationError(f"node {self.node_id} is no UNIMOC simulation")
        return SimStatus(bool(_first(paused)), int(_first(count)), int(_first(period)))

    async def set_paused(self, paused: bool) -> None:
        await self._write(self._registers, PAUSED, RegisterValue("bit", [paused]))

    async def advance(self, cycles: int) -> SimStatus:
        """Runs ``cycles`` control cycles of a paused simulation; returns the status afterwards."""
        before = await self.status()
        if not before.paused:
            raise SimulationError("pause the simulation before advancing it")
        for chunk in chunks(cycles):
            # The register reads 0 again once its cycles ran: the cycle count checks the result.
            await self._write(self._slow, ADVANCE, RegisterValue("natural32", [chunk]), expect_echo=False)
        after = await self.status()
        if after.cycle_count - before.cycle_count != cycles:
            raise SimulationError(
                f"node {self.node_id} ran {after.cycle_count - before.cycle_count} of {cycles} cycles"
            )
        return after

    async def reset(self) -> None:
        """Plant to standstill, virtual time to 0, controller restarted (refused while enabled)."""
        await self._write(self._registers, RESET, RegisterValue("natural8", [1]), expect_echo=False)

    async def _write(
        self, client: RegisterClient, name: str, value: RegisterValue, *, expect_echo: bool = True
    ) -> None:
        try:
            result = await client.write(name, value)
        except RegisterError as error:
            raise SimulationError(f"node {self.node_id}: {name}: {error}") from error
        if result.register.value.kind == "empty":
            raise SimulationError(f"node {self.node_id} is no UNIMOC simulation (no {name})")
        if expect_echo and not result.accepted:
            raise SimulationError(f"node {self.node_id} refused {name} = {value.format()}")


def _first(register: Register) -> Any:
    content = register.value.content
    return content[0] if isinstance(content, list) and content else 0
