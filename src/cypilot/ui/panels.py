# SPDX-License-Identifier: Apache-2.0
"""The panels of the main window: registers of the selected node and the log of all nodes."""

from __future__ import annotations

import datetime
from typing import ClassVar

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QStandardItem
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from cypilot.core.registers import Register, WriteResult, parse_like
from cypilot.core.session import SEVERITY_NAMES, LogRecord, Session
from cypilot.ui.bus import Bus
from cypilot.ui.models import REGISTER_ROLE, RegisterTreeModel

STORE_COMMAND = 65530
MAX_LOG_ROWS = 5000


class RegisterPanel(QWidget):
    """Scan, filter, edit and store the registers of one node."""

    message = Signal(str)
    """A line for the status bar."""

    def __init__(self, bus: Bus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._node_id: int | None = None
        self._updating = False
        self.model = RegisterTreeModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self.model)
        self._proxy.setRecursiveFilteringEnabled(True)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(0)
        self._proxy.setFilterRole(Qt.ItemDataRole.ToolTipRole)  # leaves carry the full name

        self.title = QLabel("No node selected")
        self.filter = QLineEdit(placeholderText="Filter, e.g. motor.limits")
        self.filter.textChanged.connect(self._on_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.store_button = QPushButton("Store")
        self.store_button.setToolTip("ExecuteCommand STORE_PERSISTENT_STATES: save the settings in flash")
        self.store_button.clicked.connect(self._store)
        self.tree = QTreeView()
        self.tree.setModel(self._proxy)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.model.itemChanged.connect(self._on_item_changed)

        tools = QHBoxLayout()
        tools.addWidget(self.title, 1)
        tools.addWidget(self.filter, 2)
        tools.addWidget(self.refresh_button)
        tools.addWidget(self.store_button)
        layout = QVBoxLayout(self)
        layout.addLayout(tools)
        layout.addWidget(self.tree)
        self._set_enabled(False)

    def _on_filter(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        if text:
            self.tree.expandAll()  # the matches sit deep in the tree

    @property
    def node_id(self) -> int | None:
        return self._node_id

    def show_node(self, node_id: int | None, name: str | None = None) -> None:
        if node_id == self._node_id:
            return
        self._node_id = node_id
        self._updating = True
        self.model.set_registers([])
        self._updating = False
        self.title.setText("No node selected" if node_id is None else f"Node {node_id}: {name or '?'}")
        self._set_enabled(node_id is not None)
        if node_id is not None:
            self.refresh()

    def refresh(self) -> None:
        node_id = self._node_id
        if node_id is None:
            return

        async def scan(session: Session) -> list[Register]:
            client = session.registers(node_id)
            try:
                return await client.scan()
            finally:
                client.close()

        self.message.emit(f"scanning the registers of node {node_id} ...")
        self.refresh_button.setEnabled(False)
        self._bus.call(scan, lambda result: self._on_scanned(node_id, result))

    def _on_scanned(self, node_id: int, result: list[Register] | BaseException) -> None:
        self.refresh_button.setEnabled(self._node_id is not None)
        if node_id != self._node_id:
            return
        if isinstance(result, BaseException):
            self.message.emit(f"register scan of node {node_id} failed: {result}")
            return
        self._updating = True
        self.model.set_registers(result)
        self._updating = False
        self.tree.expandToDepth(0)
        self.message.emit(f"{len(result)} registers of node {node_id}")

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._updating or self._node_id is None:
            return
        register = item.data(REGISTER_ROLE)
        if not isinstance(register, Register) or item.column() != 1:
            return
        try:
            value = parse_like(item.text(), register.value)
        except ValueError as error:
            self.message.emit(f"{register.name}: {error}")
            self._show(register, rejected=False)
            return
        node_id, name = self._node_id, register.name

        async def write(session: Session) -> WriteResult:
            client = session.registers(node_id)
            try:
                return await client.write(name, value)
            finally:
                client.close()

        self._bus.call(write, lambda result: self._on_written(node_id, register, result))

    def _on_written(self, node_id: int, before: Register, result: WriteResult | BaseException) -> None:
        if node_id != self._node_id:
            return
        if isinstance(result, BaseException):
            self.message.emit(f"{before.name}: write failed: {result}")
            self._show(before, rejected=False)
            return
        self._show(result.register, rejected=not result.accepted)
        if result.accepted:
            self.message.emit(f"{before.name} = {result.register.value.format()}")
        else:
            self.message.emit(f"{before.name}: rejected, the node keeps {result.register.value.format()}")

    def _show(self, register: Register, *, rejected: bool) -> None:
        self._updating = True
        self.model.update_register(register, rejected=rejected)
        self._updating = False

    def _store(self) -> None:
        node_id = self._node_id
        if node_id is None:
            return

        async def store(session: Session) -> int:
            return await session.execute_command(node_id, STORE_COMMAND)

        def done(result: int | BaseException) -> None:
            if isinstance(result, BaseException):
                self.message.emit(f"store on node {node_id} failed: {result}")
            elif result == 0:
                self.message.emit(f"node {node_id}: settings stored")
            elif result == 5:
                self.message.emit(f"node {node_id}: store refused while the power stage is enabled")
            else:
                self.message.emit(f"node {node_id}: store answered status {result}")

        self._bus.call(store, done)

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.filter, self.refresh_button, self.store_button, self.tree):
            widget.setEnabled(enabled)


class LogPanel(QWidget):
    """The ``uavcan.diagnostic.Record`` messages of all nodes, newest at the bottom."""

    SEVERITY_COLORS: ClassVar[dict[int, str]] = {4: "#ef6c00", 5: "#c62828", 6: "#b71c1c", 7: "#b71c1c"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Time", "Node", "Severity", "Text"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self.table.setRowCount(0))
        tools = QHBoxLayout()
        tools.addStretch(1)
        tools.addWidget(clear)
        layout = QVBoxLayout(self)
        layout.addLayout(tools)
        layout.addWidget(self.table)

    def add(self, record: LogRecord) -> None:
        if self.table.rowCount() >= MAX_LOG_ROWS:
            self.table.removeRow(0)
        row = self.table.rowCount()
        self.table.insertRow(row)
        severity = SEVERITY_NAMES[record.severity] if 0 <= record.severity < len(SEVERITY_NAMES) else "?"
        cells = (
            datetime.datetime.fromtimestamp(record.received).strftime("%H:%M:%S.%f")[:-3],
            "?" if record.node_id is None else str(record.node_id),
            severity,
            record.text,
        )
        color = self.SEVERITY_COLORS.get(record.severity)
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if color is not None:
                item.setForeground(QBrush(QColor(color)))
            self.table.setItem(row, column, item)
        self.table.scrollToBottom()
