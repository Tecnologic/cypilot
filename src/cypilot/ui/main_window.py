# SPDX-License-Identifier: Apache-2.0
"""The main window: nodes with their state, registers, control and log; stop all is always reachable."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QItemSelection, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QWidget,
)

from cypilot import __version__
from cypilot.core.control import Controller
from cypilot.core.session import LogRecord, NodeSummary, Session
from cypilot.core.transport import InterfaceSpec
from cypilot.ui.bus import Bus
from cypilot.ui.connect_dialog import ConnectDialog
from cypilot.ui.control_panel import ControlPanel
from cypilot.ui.models import NodeTableModel
from cypilot.ui.panels import LogPanel, RegisterPanel


class MainWindow(QMainWindow):
    def __init__(self, bus: Bus | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"cypilot {__version__}")
        self.resize(1200, 700)
        self.bus = bus or Bus(self)
        self.bus.nodes_changed.connect(self._on_nodes)
        self.bus.record_received.connect(self._on_record)
        self.bus.connected.connect(self._on_connected)
        self.bus.disconnected.connect(self._on_disconnected)

        self.nodes = NodeTableModel(self)
        self.node_table = QTableView()
        self.node_table.setModel(self.nodes)
        self.node_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.node_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.node_table.verticalHeader().setVisible(False)
        self.node_table.horizontalHeader().setStretchLastSection(True)
        self.node_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.node_table.selectionModel().selectionChanged.connect(self._on_selection)

        self.registers = RegisterPanel(self.bus)
        self.registers.message.connect(self._status)
        self.control = ControlPanel(self.bus)
        self.control.message.connect(self._status)
        self.log = LogPanel()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.registers, "Registers")
        self.tabs.addTab(self.control, "Control")
        self.tabs.addTab(self.log, "Log")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.node_table)
        splitter.addWidget(self.tabs)
        splitter.setSizes([480, 720])
        self.setCentralWidget(splitter)

        toolbar = self.addToolBar("Bus")
        toolbar.setMovable(False)
        self.connect_action = QAction("Connect...", self)
        self.connect_action.triggered.connect(self.ask_connect)
        self.disconnect_action = QAction("Disconnect", self)
        self.disconnect_action.triggered.connect(self.bus.disconnect_from)
        self.disconnect_action.setEnabled(False)
        toolbar.addAction(self.connect_action)
        toolbar.addAction(self.disconnect_action)
        toolbar.addSeparator()
        # Stop all and the emergency stop are reachable from anywhere in the window.
        self.stop_action = QAction("STOP ALL (Esc)", self)
        self.stop_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self.stop_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.stop_action.setToolTip("SLEEP to every drive and zero setpoints, at once")
        self.stop_action.triggered.connect(self.stop_all)
        self.estop_action = QAction("Emergency stop (Shift+Esc)", self)
        self.estop_action.setShortcut(QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_Escape))
        self.estop_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.estop_action.setToolTip(
            "ExecuteCommand EMERGENCY_STOP to every UNIMOC node; latched until a restart"
        )
        self.estop_action.triggered.connect(self.emergency_stop)
        toolbar.addAction(self.stop_action)
        toolbar.addAction(self.estop_action)
        stop_button = toolbar.widgetForAction(self.stop_action)
        if stop_button is not None:
            stop_button.setStyleSheet(
                "background: #c62828; color: white; font-weight: bold; padding: 4px 12px"
            )

        # The watchdog of the readiness keep-alive: a stalled UI thread puts the drives to sleep.
        self.feed_timer = QTimer(self)
        self.feed_timer.setInterval(100)
        self.feed_timer.timeout.connect(self.bus.feed)
        self.feed_timer.start()
        self._selected: int | None = None
        self._nodes: list[NodeSummary] = []
        self._status("not connected")

    def stop_all(self) -> None:
        def done(result: list[int] | BaseException) -> None:
            if isinstance(result, BaseException):
                self._status(f"stop all: {result}")
            else:
                self._status("STOP ALL: SLEEP sent to " + (", ".join(map(str, result)) or "no drive"))

        self.bus.stop_all(done)

    def emergency_stop(self) -> None:
        targets = [n.node_id for n in self._nodes if n.is_unimoc]
        if not targets:
            self.stop_all()
            return
        answer = QMessageBox.warning(
            self,
            "Emergency stop",
            f"Send EMERGENCY_STOP to nodes {', '.join(map(str, targets))}? "
            "They stay stopped until a restart.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        async def stop(session: Session, controller: Controller) -> dict[int, int | None]:
            return await controller.emergency_stop(targets)

        def done(result: dict[int, int | None] | BaseException) -> None:
            if isinstance(result, BaseException):
                self._status(f"emergency stop: {result}")
            else:
                answered = [str(n) for n, status in result.items() if status == 0]
                self._status("EMERGENCY STOP sent; confirmed by " + (", ".join(answered) or "no node"))

        self.bus.control(stop, done)

    def ask_connect(self) -> None:
        dialog = ConnectDialog(self)
        if dialog.exec() and dialog.spec is not None:
            self.connect_to(dialog.spec, dialog.local_node_id.value())

    def connect_to(self, spec: InterfaceSpec, local_node_id: int) -> None:
        self.connect_action.setEnabled(False)
        self._status(f"connecting to {spec.text} (the first start compiles the DSDL) ...")
        self.bus.connect_to(spec, local_node_id)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.bus.shutdown()
        super().closeEvent(event)

    # --- Bus events ----------------------------------------------------------

    def _on_connected(self, interface: str) -> None:
        self.disconnect_action.setEnabled(True)
        self.connect_action.setEnabled(False)
        self._status(f"connected to {interface}")

    def _on_disconnected(self, reason: str) -> None:
        self.disconnect_action.setEnabled(False)
        self.connect_action.setEnabled(True)
        self.nodes.set_nodes([])
        self._nodes = []
        self.registers.show_node(None)
        self.control.show_node(None)
        self._status(f"connection failed: {reason}" if reason else "disconnected")

    def _on_nodes(self, nodes: Sequence[NodeSummary]) -> None:
        self._nodes = list(nodes)
        self.nodes.set_nodes(nodes)
        if self._selected is not None:
            row = self.nodes.row_of(self._selected)
            if row is None:
                self._status(f"node {self._selected} went offline")
                self.control.show_node(None)
            else:
                self.node_table.selectRow(row)
                self.control.show_node(self.nodes.node_at(row))

    def _on_record(self, record: LogRecord) -> None:
        self.log.add(record)

    def _on_selection(self, selected: QItemSelection, _deselected: QItemSelection) -> None:
        rows = {index.row() for index in selected.indexes()}
        if not rows:
            return
        node = self.nodes.node_at(min(rows))
        if node is not None:
            self._selected = node.node_id
            self.registers.show_node(node.node_id, node.name)
            self.control.show_node(node)

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)
