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

from PySide6.QtCore import Qt

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


def test_control_enables_stops_all_and_the_watchdog_catches_a_frozen_ui(qapp: Any, sim: SimInstance) -> None:
    window = MainWindow()
    window.show()
    state = lambda: window.nodes.data(window.nodes.index(window.nodes.row_of(DRIVE) or 0, 6))  # noqa: E731
    try:
        window.connect_to(parse_interface(sim.slcan_interfaces[0]), 127)
        wait_until(qapp, lambda: window.nodes.row_of(DRIVE) is not None, timeout=30)
        window.node_table.selectRow(window.nodes.row_of(DRIVE) or 0)
        window.tabs.setCurrentWidget(window.control)
        wait_until(qapp, window.control.enable_button.isEnabled)  # the readiness subject is known
        assert "readiness subject 1034" in window.control.state.text()

        window.control.enable_button.click()
        wait_until(qapp, lambda: state() == "engaged")
        wait_until(qapp, window.control.send_button.isEnabled)
        window.control.mode.setCurrentIndex(
            window.control.mode.findText("current", Qt.MatchFlag.MatchStartsWith)
        )
        window.control.values[1].setValue(1.5)
        window.control.send_button.click()
        wait_until(qapp, lambda: "current = 0, 1.5" in window.statusBar().currentMessage())

        window.stop_action.trigger()
        wait_until(qapp, lambda: "STOP ALL: SLEEP sent to 10" in window.statusBar().currentMessage())
        wait_until(qapp, lambda: state() == "disabled")

        window.control.enable_button.click()
        wait_until(qapp, lambda: state() == "engaged")
        window.feed_timer.stop()  # as if the UI thread hung
        wait_until(qapp, lambda: state() == "disabled", timeout=5)
        window.feed_timer.start()
    finally:
        window.close()
