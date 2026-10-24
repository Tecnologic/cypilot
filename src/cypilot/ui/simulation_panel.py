# SPDX-License-Identifier: Apache-2.0
"""The Simulation tab: virtual clock, pause, run, advance and reset of a simulated drive (G3).

The plant inputs are built from the node's ``unimoc.sim.*`` registers found by
the scan (a checkbox for a bit, a number field otherwise); only the controls
name the registers they need (``cypilot.core.sim_control``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cypilot.core.registers import Register, RegisterClient, RegisterValue, WriteResult
from cypilot.core.session import NodeSummary, Session
from cypilot.core.sim_control import (
    ADVANCE,
    CYCLE_COUNT,
    CYCLE_PERIOD,
    PAUSED,
    PREFIX,
    RESET,
    SimControl,
    SimStatus,
)
from cypilot.ui.bus import Bus

CONTROL_REGISTERS = {PAUSED, ADVANCE, RESET, CYCLE_COUNT, CYCLE_PERIOD}


class SimulationPanel(QWidget):
    message = Signal(str)

    def __init__(self, bus: Bus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._node_id: int | None = None
        self._inputs_for: int | None = None

        self.title = QLabel("No simulated drive selected")
        self.clock = QLabel("-")
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(lambda: self._set_paused(True))
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(lambda: self._set_paused(False))
        self.cycles = QSpinBox(suffix=" cycles")
        self.cycles.setRange(1, 1_000_000)
        self.cycles.setValue(1600)
        self.advance_button = QPushButton("Advance")
        self.advance_button.setToolTip("Runs the cycles of a paused simulation as fast as it can")
        self.advance_button.clicked.connect(self._advance)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip("Plant to standstill, virtual time to 0, controller restarted")
        self.reset_button.clicked.connect(self._reset)
        controls = QHBoxLayout()
        for widget in (
            self.pause_button,
            self.run_button,
            self.cycles,
            self.advance_button,
            self.reset_button,
        ):
            controls.addWidget(widget)
        controls.addStretch(1)

        self.inputs = QGroupBox("Plant inputs (unimoc.sim.* registers)")
        self.inputs_layout = QFormLayout(self.inputs)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.clock)
        layout.addLayout(controls)
        layout.addWidget(self.inputs)
        layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self._set_enabled(False)

    def show_node(self, node: NodeSummary | None) -> None:
        node_id = node.node_id if node is not None else None
        if node_id == self._node_id:
            return
        self._node_id = node_id
        self._clear_inputs()
        self._set_enabled(False)
        self.title.setText("No simulated drive selected" if node is None else f"Node {node_id}")
        self.clock.setText("-")
        if node_id is None:
            self.timer.stop()
            return
        self.refresh()
        self.timer.start()

    def refresh(self) -> None:
        node_id = self._node_id
        if node_id is None or not self.isVisible():
            return

        async def status(session: Session) -> SimStatus:
            control = SimControl(session.node, node_id)
            try:
                return await control.status()
            finally:
                control.close()

        self._bus.call(status, lambda result: self._on_status(node_id, result))

    def _on_status(self, node_id: int, result: SimStatus | BaseException) -> None:
        if node_id != self._node_id:
            return
        if isinstance(result, BaseException):
            self.title.setText(f"Node {node_id}: not a simulation")
            self._set_enabled(False)
            self.timer.stop()
            return
        self.title.setText(f"Node {node_id}: simulated drive")
        state = "paused" if result.paused else "running"
        self.clock.setText(f"{state}, cycle {result.cycle_count}, virtual time {result.virtual_time:.4f} s")
        self._set_enabled(True)
        self.pause_button.setEnabled(not result.paused)
        self.run_button.setEnabled(result.paused)
        self.advance_button.setEnabled(result.paused)
        if self._inputs_for != node_id:
            self._load_inputs(node_id)

    # --- Controls ------------------------------------------------------------

    def _set_paused(self, paused: bool) -> None:
        self._control(lambda c: c.set_paused(paused), "paused" if paused else "running")

    def _advance(self) -> None:
        cycles = self.cycles.value()

        async def advance(control: SimControl) -> None:
            await control.advance(cycles)

        self._control(advance, f"advanced {cycles} cycles")

    def _reset(self) -> None:
        self._control(lambda c: c.reset(), "reset: plant at standstill, virtual time 0")

    def _control(self, action: Callable[[SimControl], Awaitable[None]], done_text: str) -> None:
        node_id = self._node_id
        if node_id is None:
            return

        async def job(session: Session) -> None:
            control = SimControl(session.node, node_id)
            try:
                await action(control)
            finally:
                control.close()

        def done(result: object) -> None:
            if isinstance(result, BaseException):
                self.message.emit(f"node {node_id}: {result}")
            else:
                self.message.emit(f"node {node_id}: {done_text}")
            self.refresh()

        self._bus.call(job, done)

    # --- Plant inputs --------------------------------------------------------

    def _load_inputs(self, node_id: int) -> None:
        self._inputs_for = node_id

        async def scan(session: Session) -> list[Register]:
            client = RegisterClient(session.node, node_id)
            try:
                names = [
                    n for n in await client.names() if n.startswith(PREFIX) and n not in CONTROL_REGISTERS
                ]
                return [r for r in await client.scan(names) if r.mutable]
            finally:
                client.close()

        def done(result: list[Register] | BaseException) -> None:
            if node_id != self._node_id or isinstance(result, BaseException):
                return
            self._clear_inputs()
            for register in result:
                editor = self._editor(node_id, register)
                if editor is not None:
                    self.inputs_layout.addRow(register.name.removeprefix(PREFIX), editor)

        self._bus.call(scan, done)

    def _editor(self, node_id: int, register: Register) -> QWidget | None:
        value = register.value
        if not isinstance(value.content, list) or len(value.content) != 1:
            return None
        if value.kind == "bit":
            box = QCheckBox()
            box.setChecked(bool(value.content[0]))
            box.toggled.connect(
                lambda checked: self._write(node_id, register.name, RegisterValue("bit", [checked]))
            )
            return box
        spin = QDoubleSpinBox()
        spin.setRange(-1e9, 1e9)
        spin.setDecimals(3 if value.kind.startswith("real") else 0)
        spin.setValue(float(value.content[0]))
        kind = value.kind

        def commit() -> None:
            number = spin.value()
            content: float | int = number if kind.startswith("real") else round(number)
            self._write(node_id, register.name, RegisterValue(kind, [content]))

        spin.editingFinished.connect(commit)
        return spin

    def _write(self, node_id: int, name: str, value: RegisterValue) -> None:
        async def write(session: Session) -> WriteResult:
            client = RegisterClient(session.node, node_id)
            try:
                return await client.write(name, value)
            finally:
                client.close()

        def done(result: WriteResult | BaseException) -> None:
            if isinstance(result, BaseException):
                self.message.emit(f"{name}: {result}")
            elif result.accepted:
                self.message.emit(f"{name} = {result.register.value.format()}")
            else:
                self.message.emit(f"{name}: rejected, the node keeps {result.register.value.format()}")

        self._bus.call(write, done)

    def _clear_inputs(self) -> None:
        self._inputs_for = None
        while self.inputs_layout.rowCount():
            self.inputs_layout.removeRow(0)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (
            self.pause_button,
            self.run_button,
            self.cycles,
            self.advance_button,
            self.reset_button,
        ):
            widget.setEnabled(enabled)
