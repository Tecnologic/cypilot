# SPDX-License-Identifier: Apache-2.0
"""The main window: the nodes of the bus with their state, their registers and the log (G2)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QItemSelection, Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMainWindow,
    QSplitter,
    QTableView,
    QTabWidget,
    QWidget,
)

from cypilot import __version__
from cypilot.core.session import LogRecord, NodeSummary
from cypilot.core.transport import InterfaceSpec
from cypilot.ui.bus import Bus
from cypilot.ui.connect_dialog import ConnectDialog
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
        self.log = LogPanel()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.registers, "Registers")
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
        self._selected: int | None = None
        self._status("not connected")

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
        self.registers.show_node(None)
        self._status(f"connection failed: {reason}" if reason else "disconnected")

    def _on_nodes(self, nodes: Sequence[NodeSummary]) -> None:
        self.nodes.set_nodes(nodes)
        if self._selected is not None:
            row = self.nodes.row_of(self._selected)
            if row is None:
                self._status(f"node {self._selected} went offline")
            else:
                self.node_table.selectRow(row)

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

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)
