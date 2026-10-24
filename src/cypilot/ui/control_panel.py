# SPDX-License-Identifier: Apache-2.0
"""The Control tab: enable, disable and recover the selected drive, and its setpoints (G3).

Nothing here engages a drive without a click on **Enable**. Stop all (Esc) and
the emergency stop live in the main window, so they are always reachable.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cypilot.core.control import (
    ControlError,
    Controller,
    DriveTarget,
    Recovery,
    SetpointMode,
    find_target,
    recover,
)
from cypilot.core.session import NodeSummary, Session
from cypilot.core.status import Chip
from cypilot.ui.bus import Bus

UNITS = {
    SetpointMode.CURRENT: ("i_d [A]", "i_q [A]"),
    SetpointMode.TORQUE: ("torque [N m]",),
    SetpointMode.SPEED: ("speed [rad/s]",),
    SetpointMode.POSITION: ("position [rad]",),
}


class ControlPanel(QWidget):
    message = Signal(str)

    def __init__(self, bus: Bus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._node: NodeSummary | None = None
        self._target: DriveTarget | None = None
        self._target_requested: int | None = None
        """Node-ID whose ports were requested (once per selection, also after a failure)."""

        self.title = QLabel("No node selected")
        self.state = QLabel("-")
        self.enable_button = QPushButton("Enable")
        self.enable_button.setToolTip(
            "Publish ENGAGED on the drive's readiness subject while this window lives"
        )
        self.enable_button.clicked.connect(self.enable)
        self.disable_button = QPushButton("Disable")
        self.disable_button.setToolTip("Ramp the setpoint to zero, then STANDBY")
        self.disable_button.clicked.connect(self.disable)
        self.recover_button = QPushButton("Recover")
        self.recover_button.setToolTip("Clear a trip (STANDBY) or a latched emergency stop (restart)")
        self.recover_button.clicked.connect(self.recover)
        buttons = QHBoxLayout()
        for button in (self.enable_button, self.disable_button, self.recover_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        self.mode = QComboBox()
        self.mode.currentIndexChanged.connect(self._on_mode)
        self.values = [QDoubleSpinBox(), QDoubleSpinBox()]
        for spin in self.values:
            spin.setRange(-1e6, 1e6)
            spin.setDecimals(3)
        self.value_labels = [QLabel(), QLabel()]
        self.ramp = QDoubleSpinBox(suffix=" s")
        self.ramp.setRange(0.0, 60.0)
        self.ramp.setValue(0.5)
        self.send_button = QPushButton("Send setpoint")
        self.send_button.clicked.connect(lambda: self.send_setpoint())  # clicked passes a bool
        self.zero_button = QPushButton("Zero")
        self.zero_button.clicked.connect(self.zero_setpoint)
        setpoints = QGroupBox("Setpoint (shared: every engaged drive on the same subject follows it)")
        form = QFormLayout(setpoints)
        form.addRow("Mode", self.mode)
        for label, spin in zip(self.value_labels, self.values, strict=True):
            form.addRow(label, spin)
        form.addRow("Ramp", self.ramp)
        row = QHBoxLayout()
        row.addWidget(self.send_button)
        row.addWidget(self.zero_button)
        row.addStretch(1)
        form.addRow(row)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.state)
        layout.addLayout(buttons)
        layout.addWidget(setpoints)
        layout.addStretch(1)
        self._update()

    # --- Selection and state -------------------------------------------------

    def show_node(self, node: NodeSummary | None) -> None:
        changed = (node is None) != (self._node is None) or (
            node is not None and self._node is not None and node.node_id != self._node.node_id
        )
        self._node = node
        if changed:
            self._target = None
            self._target_requested = None
            self.mode.clear()
            self.title.setText(
                "No node selected" if node is None else f"Node {node.node_id}: {node.name or '?'}"
            )
        # The UNIMOC probe may answer after the selection: read the ports once it did.
        if node is not None and node.is_unimoc and self._target_requested != node.node_id:
            self._target_requested = node.node_id
            self._load_target(node.node_id)
        self._update()

    def _load_target(self, node_id: int) -> None:
        async def load(session: Session) -> DriveTarget:
            return await find_target(session, node_id)

        def done(result: DriveTarget | BaseException) -> None:
            if self._node is None or self._node.node_id != node_id:
                return
            if isinstance(result, BaseException):
                self.message.emit(f"node {node_id}: {result}")
                return
            self._target = result
            self.mode.clear()
            for mode in result.setpoints:
                self.mode.addItem(f"{mode.name.lower()} (subject {result.setpoints[mode]})", mode)
            self._update()

        self._bus.call(load, done)

    def _update(self) -> None:
        node, target = self._node, self._target
        chips = set(node.chips) if node else set()
        engaged = Chip.ENGAGED in chips
        if node is None:
            self.state.setText("-")
        elif not node.is_unimoc:
            self.state.setText("not a UNIMOC drive: no control")
        else:
            readiness = f"readiness subject {target.readiness}" if target else "reading the ports ..."
            self.state.setText(f"{', '.join(c.value for c in node.chips) or '-'}  ({readiness})")
        controllable = target is not None and Chip.LOCAL_CONTROL not in chips
        self.enable_button.setEnabled(controllable and not engaged)
        self.disable_button.setEnabled(controllable and engaged)
        self.recover_button.setEnabled(bool(chips & {Chip.E_STOP, Chip.TRIPPED, Chip.FAULT, Chip.BLOCKED}))
        self.send_button.setEnabled(controllable and engaged and self.mode.count() > 0)
        self.zero_button.setEnabled(self.send_button.isEnabled())
        self._on_mode()

    def _on_mode(self) -> None:
        mode = self.mode.currentData()
        labels = UNITS.get(mode, ()) if isinstance(mode, SetpointMode) else ()
        for index, (label, spin) in enumerate(zip(self.value_labels, self.values, strict=True)):
            visible = index < len(labels)
            label.setText(labels[index] if visible else "")
            label.setVisible(visible)
            spin.setVisible(visible)

    # --- Actions -------------------------------------------------------------

    def enable(self) -> None:
        target = self._target
        if target is None:
            return

        async def engage(session: Session, controller: Controller) -> None:
            await controller.engage(target)
            await controller.wait_engaged(target.node_id)

        self.message.emit(f"node {target.node_id}: enabling ...")
        self._bus.control(engage, lambda result: self._report(target.node_id, "enabled", result))

    def disable(self) -> None:
        target = self._target
        if target is None:
            return
        ramp = self.ramp.value()

        async def disengage(session: Session, controller: Controller) -> None:
            await controller.disengage(target.node_id, ramp=ramp)

        self._bus.control(disengage, lambda result: self._report(target.node_id, "disabled", result))

    def send_setpoint(self, values: tuple[float, ...] | None = None) -> None:
        target, mode = self._target, self.mode.currentData()
        if target is None or not isinstance(mode, SetpointMode):
            return
        chosen = values if values is not None else tuple(s.value() for s in self.values[: mode.components])
        ramp = self.ramp.value()

        async def send(session: Session, controller: Controller) -> None:
            controller.set_setpoint(target, mode, chosen, ramp=ramp)

        text = ", ".join(f"{v:g}" for v in chosen)
        self._bus.control(
            send, lambda result: self._report(target.node_id, f"{mode.name.lower()} = {text}", result)
        )

    def zero_setpoint(self) -> None:
        mode = self.mode.currentData()
        if isinstance(mode, SetpointMode):
            self.send_setpoint(tuple(0.0 for _ in range(mode.components)))

    def recover(self) -> None:
        node = self._node
        if node is None:
            return
        allow_restart = False
        if Chip.E_STOP in node.chips:
            answer = QMessageBox.question(
                self,
                "Restart the drive?",
                f"The emergency stop of node {node.node_id} is latched until a restart. "
                "Restart the drive now?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            allow_restart = True
        node_id = node.node_id

        async def run(session: Session, controller: Controller) -> Recovery:
            return await recover(session, controller, node_id, allow_restart=allow_restart)

        def done(result: Recovery | BaseException) -> None:
            if isinstance(result, BaseException):
                self.message.emit(f"node {node_id}: recovery failed: {result}")
            else:
                self.message.emit(f"node {node_id}: " + "; ".join(result.steps))

        self.message.emit(f"node {node_id}: recovering ...")
        self._bus.control(run, done)

    def _report(self, node_id: int, what: str, result: object) -> None:
        if isinstance(result, ControlError):
            self.message.emit(str(result))
        elif isinstance(result, BaseException):
            self.message.emit(f"node {node_id}: {type(result).__name__}: {result}")
        else:
            self.message.emit(f"node {node_id}: {what}")
