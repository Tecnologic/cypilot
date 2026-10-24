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
        wait_until(qapp, lambda: window.saw("unimoc.motor.limits.i_max = 12.5"))
        value.setText("5000")
        wait_until(qapp, lambda: window.saw("rejected"))
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
        wait_until(qapp, lambda: window.saw("current = 0, 1.5"))

        window.stop_action.trigger()
        wait_until(qapp, lambda: window.saw("STOP ALL: SLEEP sent to 10"))
        wait_until(qapp, lambda: state() == "disabled")

        window.control.enable_button.click()
        wait_until(qapp, lambda: state() == "engaged")
        window.feed_timer.stop()  # as if the UI thread hung
        wait_until(qapp, lambda: state() == "disabled", timeout=5)
        window.feed_timer.start()
    finally:
        window.close()


def test_simulation_tab_builds_the_plant_inputs_and_steps_the_clock(qapp: Any, sim: SimInstance) -> None:
    window = MainWindow()
    window.show()
    panel = window.simulation
    try:
        window.connect_to(parse_interface(sim.slcan_interfaces[0]), 127)
        wait_until(qapp, lambda: window.nodes.row_of(DRIVE) is not None, timeout=30)
        window.node_table.selectRow(window.nodes.row_of(DRIVE) or 0)
        window.tabs.setCurrentWidget(panel)
        wait_until(qapp, lambda: panel.inputs_layout.rowCount() >= 5)  # from the unimoc.sim.* registers
        labels = {
            panel.inputs_layout.itemAt(row, panel.inputs_layout.ItemRole.LabelRole).widget().text()
            for row in range(panel.inputs_layout.rowCount())
        }
        assert {"shaft_mode", "load_torque", "dc_link_voltage", "external_enable"} <= labels
        assert "paused" not in labels and "advance" not in labels  # those are the controls

        wait_until(qapp, panel.pause_button.isEnabled)
        panel.pause_button.click()
        wait_until(qapp, lambda: panel.clock.text().startswith("paused"))
        before = int(panel.clock.text().split("cycle ")[1].split(",")[0])
        panel.cycles.setValue(1600)
        panel.advance_button.click()
        wait_until(qapp, lambda: f"cycle {before + 1600}," in panel.clock.text())
        panel.run_button.click()
        wait_until(qapp, lambda: panel.clock.text().startswith("running"))
    finally:
        window.close()


def test_plot_records_the_telemetry_exports_and_replays(qapp: Any, sim: SimInstance, tmp_path: Path) -> None:
    window = MainWindow()
    window.show()
    key = f"{DRIVE}/telemetry"
    try:
        window.connect_to(parse_interface(sim.slcan_interfaces[0]), 127)
        wait_until(qapp, lambda: window.nodes.row_of(DRIVE) is not None, timeout=30)
        window.node_table.selectRow(window.nodes.row_of(DRIVE) or 0)
        window.tabs.setCurrentWidget(window.plot)
        wait_until(qapp, lambda: window.plot.source_item(key) is not None)

        source = window.plot.source_item(key)
        assert source is not None
        source.setCheckState(0, Qt.CheckState.Checked)  # as a click does
        wait_until(qapp, lambda: window.plot.field_item(key, "control_cycles") is not None)
        assert window.plot.subscribed == [key]
        field = window.plot.field_item(key, "control_cycles")
        assert field is not None
        field.setCheckState(0, Qt.CheckState.Checked)
        wait_until(qapp, lambda: len(window.plot.curve_data(key, "control_cycles")[0]) >= 5)
        window.plot.mark("a marker")

        window.plot.export(tmp_path)
        assert (tmp_path / "node10_telemetry.csv").is_file()
        window.plot.load(tmp_path)
        assert window.plot.state.text() == "replay"
        assert "a marker" in [text for _, text in window.plot.recorder.markers]
        replayed = window.plot.field_item(key, "control_cycles")
        assert replayed is not None
        replayed.setCheckState(0, Qt.CheckState.Checked)
        assert len(window.plot.curve_data(key, "control_cycles")[0]) >= 5

        window.plot.show_live()
        wait_until(qapp, lambda: window.plot.field_item(key, "control_cycles") is not None)
        assert window.plot.state.text().startswith("live, 1 source")
    finally:
        window.close()
