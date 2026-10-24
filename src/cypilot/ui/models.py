# SPDX-License-Identifier: Apache-2.0
"""Qt models of the node table and the register tree, filled from core snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QStandardItem, QStandardItemModel

from cypilot.core.registers import Register
from cypilot.core.session import NodeSummary
from cypilot.core.status import Chip, Health

REGISTER_ROLE = Qt.ItemDataRole.UserRole + 1
"""The full :class:`Register` of a leaf of the register tree."""

CHIP_COLORS = {
    Chip.OFFLINE: "#9e9e9e",
    Chip.E_STOP: "#c62828",
    Chip.TRIPPED: "#e65100",
    Chip.FAULT: "#e65100",
    Chip.BLOCKED: "#ef6c00",
    Chip.ENGAGED: "#2e7d32",
    Chip.ENABLED: "#558b2f",
    Chip.DISABLED: "#546e7a",
    Chip.SETTINGS_PENDING: "#1565c0",
    Chip.LOCAL_CONTROL: "#6a1b9a",
}
HEALTH_COLORS = {Health.ADVISORY: "#ef6c00", Health.CAUTION: "#c62828", Health.WARNING: "#b71c1c"}

Index = QModelIndex | QPersistentModelIndex


class NodeTableModel(QAbstractTableModel):
    """One row per online node: ID, name, version, health, mode, uptime, state chips."""

    HEADERS = ("ID", "Name", "SW", "Health", "Mode", "Uptime", "State")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._nodes: list[NodeSummary] = []

    def set_nodes(self, nodes: Sequence[NodeSummary]) -> None:
        self.beginResetModel()
        self._nodes = list(nodes)
        self.endResetModel()

    def node_at(self, row: int) -> NodeSummary | None:
        return self._nodes[row] if 0 <= row < len(self._nodes) else None

    def row_of(self, node_id: int) -> int | None:
        return next((row for row, node in enumerate(self._nodes) if node.node_id == node_id), None)

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008 (Qt API)
        return 0 if parent.isValid() else len(self._nodes)

    def columnCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008 (Qt API)
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        node = self.node_at(index.row())
        if node is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return _node_cell(node, column)
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == 3 and node.health in HEALTH_COLORS:
                return QBrush(QColor(HEALTH_COLORS[node.health]))
            if column == 6 and node.chips:
                return QBrush(QColor(CHIP_COLORS[node.chips[0]]))
        if role == Qt.ItemDataRole.ToolTipRole and column == 6 and node.unimoc is None:
            return "probing whether this is a UNIMOC node"
        return None


def _node_cell(node: NodeSummary, column: int) -> str:
    if column == 0:
        return str(node.node_id)
    if column == 1:
        return node.name or "?"
    if column == 2:
        return f"{node.software_version[0]}.{node.software_version[1]}" if node.software_version else "?"
    if column == 3:
        return node.health.name.lower()
    if column == 4:
        return node.mode.name.lower()
    if column == 5:
        return _uptime(node.uptime)
    return ", ".join(chip.value for chip in node.chips) or "-"


def _uptime(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


class RegisterTreeModel(QStandardItemModel):
    """The registers of one node as a tree on the dots of their names.

    Columns: name part, value (editable for mutable registers), type, flags.
    A leaf keeps its :class:`Register` under :data:`REGISTER_ROLE`.
    """

    HEADERS = ("Register", "Value", "Type", "Flags")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setHorizontalHeaderLabels(list(self.HEADERS))
        self._leaves: dict[str, QStandardItem] = {}

    def set_registers(self, registers: Sequence[Register]) -> None:
        self.removeRows(0, self.rowCount())
        self._leaves.clear()
        branches: dict[str, QStandardItem] = {}
        for register in registers:
            parent = self.invisibleRootItem()
            parts = register.name.split(".")
            for depth in range(1, len(parts)):
                prefix = ".".join(parts[:depth])
                if prefix not in branches:
                    branch = _item(parts[depth - 1])
                    parent.appendRow([branch, _item(""), _item(""), _item("")])
                    branches[prefix] = branch
                parent = branches[prefix]
            leaf = _item(parts[-1])
            leaf.setData(register, REGISTER_ROLE)
            leaf.setToolTip(register.name)
            value = _item(register.value.format(), editable=register.mutable)
            value.setData(register, REGISTER_ROLE)
            parent.appendRow([leaf, value, _item(_type(register)), _item(_flags(register))])
            self._leaves[register.name] = value

    def value_item(self, name: str) -> QStandardItem | None:
        return self._leaves.get(name)

    def update_register(self, register: Register, *, rejected: bool = False) -> None:
        """Shows a register's new value; a rejected write is marked red until the next change."""
        item = self._leaves.get(register.name)
        if item is None:
            return
        item.setData(register, REGISTER_ROLE)
        item.setText(register.value.format())
        item.setForeground(QBrush(QColor("#c62828")) if rejected else QBrush())
        item.setToolTip("rejected by the node; it kept this value" if rejected else "")


def _item(text: str, *, editable: bool = False) -> QStandardItem:
    item = QStandardItem(text)
    item.setEditable(editable)
    return item


def _type(register: Register) -> str:
    content = register.value.content
    return f"{register.value.kind}[{len(content)}]" if isinstance(content, list) else register.value.kind


def _flags(register: Register) -> str:
    return ("mutable " if register.mutable else "") + ("persistent" if register.persistent else "")
