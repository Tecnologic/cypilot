# SPDX-License-Identifier: Apache-2.0
"""The main window against the released UNIMOC simulation (offscreen)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from cypilot.core.transport import parse_interface
from cypilot.simulation import SimInstance
from qt_support import wait_until

pytest.importorskip("PySide6.QtWidgets")

from cypilot.ui.main_window import MainWindow

DRIVE = 10


def test_main_window_shows_the_drive_and_edits_its_registers(qapp: Any, sim: SimInstance) -> None:
    window = MainWindow()
    window.show()
    try:
        window.connect_to(parse_interface(sim.slcan_interfaces[0]), 127)
        wait_until(qapp, lambda: window.nodes.row_of(DRIVE) is not None, timeout=30)
        row = window.nodes.row_of(DRIVE)
        assert row is not None
        wait_until(qapp, lambda: window.nodes.data(window.nodes.index(row, 6)) == "disabled")

        window.node_table.selectRow(row)
        wait_until(qapp, lambda: window.registers.model.value_item("unimoc.motor.limits.i_max") is not None)
        value = window.registers.model.value_item("unimoc.motor.limits.i_max")
        assert value is not None

        value.setText("12.5")  # as the editor does
        wait_until(qapp, lambda: "unimoc.motor.limits.i_max = 12.5" in window.statusBar().currentMessage())
        value.setText("5000")
        wait_until(qapp, lambda: "rejected" in window.statusBar().currentMessage())
        assert value.text() == "12.5"
        assert value.foreground().color().name() == "#c62828"

        screenshot = os.environ.get("CYPILOT_SCREENSHOT")
        if screenshot:
            window.registers.filter.setText("motor.limits")
            qapp.processEvents()
            window.grab().save(str(Path(screenshot)))
    finally:
        window.close()
