# SPDX-License-Identifier: Apache-2.0
"""The Qt models and panels without a bus (offscreen)."""

from __future__ import annotations

from typing import Any

import pytest

from cypilot.core.registers import Register, RegisterValue
from cypilot.core.session import LogRecord, NodeSummary
from cypilot.core.status import Health, Mode

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt

from cypilot.ui.connect_dialog import ConnectDialog, discover_interfaces
from cypilot.ui.models import CHIP_COLORS, REGISTER_ROLE, NodeTableModel, RegisterTreeModel
from cypilot.ui.panels import LogPanel


def summary(node_id: int, vendor_code: int, health: Health = Health.NOMINAL) -> NodeSummary:
    return NodeSummary(
        node_id=node_id,
        health=health,
        mode=Mode.OPERATIONAL,
        vendor_code=vendor_code,
        uptime=3725,
        name="FL drive",
        software_version=(0, 2),
        hardware_version=(1, 0),
        unique_id=b"\x01",
        vcs_revision=0x1234,
        unimoc=True,
    )


def test_node_table_shows_the_state_chips(qapp: Any) -> None:
    model = NodeTableModel()
    model.set_nodes([summary(10, 0x09), summary(11, 0x20, Health.CAUTION)])
    assert model.rowCount() == 2 and model.columnCount() == 7
    cell = lambda row, column: model.data(model.index(row, column))  # noqa: E731
    assert [cell(0, c) for c in range(7)] == [
        "10",
        "FL drive",
        "0.2",
        "nominal",
        "operational",
        "1:02:05",
        "engaged",
    ]
    assert cell(1, 6) == "E-stop latched"
    color = model.data(model.index(1, 6), Qt.ItemDataRole.ForegroundRole).color().name()
    assert color == next(c for chip, c in CHIP_COLORS.items() if chip.value == "E-stop latched")
    assert model.row_of(11) == 1 and model.row_of(99) is None


def test_register_tree_groups_by_dots_and_marks_rejected_values(qapp: Any) -> None:
    i_max = Register("unimoc.motor.limits.i_max", RegisterValue("real32", [15.0]), True, True)
    kind = Register(
        "uavcan.sub.readiness.type",
        RegisterValue("string", "reg.udral.service.common.Readiness.0.1"),
        False,
        False,
    )
    model = RegisterTreeModel()
    model.set_registers([i_max, kind])
    assert [model.item(row).text() for row in range(model.rowCount())] == ["unimoc", "uavcan"]
    value = model.value_item("unimoc.motor.limits.i_max")
    assert value is not None and value.text() == "15" and value.isEditable()
    readonly = model.value_item("uavcan.sub.readiness.type")
    assert readonly is not None and not readonly.isEditable()
    assert value.data(REGISTER_ROLE) == i_max

    model.update_register(i_max, rejected=True)
    assert value.foreground().color().name() == "#c62828"
    model.update_register(Register(i_max.name, RegisterValue("real32", [12.5]), True, True))
    assert value.text() == "12.5"


def test_log_panel_keeps_the_records(qapp: Any) -> None:
    panel = LogPanel()
    panel.add(LogRecord(received=0.0, node_id=10, severity=4, text="settings wait for the disengage"))
    assert panel.table.rowCount() == 1
    assert [panel.table.item(0, c).text() for c in (1, 2, 3)] == [
        "10",
        "warning",
        "settings wait for the disengage",
    ]


def test_connect_dialog_refuses_an_invalid_interface(qapp: Any) -> None:
    dialog = ConnectDialog()
    dialog.interface.setCurrentText("vcan0")  # the kind is missing
    dialog._accept()
    assert dialog.spec is None and "interface" in dialog.error.text()
    dialog.interface.setCurrentText("slcan:/dev/pts/9")
    dialog._accept()
    assert dialog.spec is not None and dialog.spec.mtu == 8
    assert isinstance(discover_interfaces(), list)


class FakeBus:
    """Records the jobs a panel submits instead of running them."""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def call(self, job: Any, done: Any = None) -> None:
        self.jobs.append((job, done))


def test_control_panel_loads_the_target_once_the_node_is_known_as_unimoc(qapp: Any) -> None:
    from dataclasses import replace

    from cypilot.ui.control_panel import ControlPanel

    bus = FakeBus()
    panel = ControlPanel(bus)  # type: ignore[arg-type]
    probing = replace(summary(10, 0x00), unimoc=None)  # selected before the UNIMOC probe answered
    panel.show_node(probing)
    assert bus.jobs == []
    panel.show_node(replace(probing, unimoc=True))
    assert len(bus.jobs) == 1  # the readiness subject is read now
    panel.show_node(replace(probing, unimoc=True, uptime=3726))
    assert len(bus.jobs) == 1  # not again while the first read runs
