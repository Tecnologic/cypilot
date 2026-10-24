# SPDX-License-Identifier: Apache-2.0
"""cypilot-ctl sim against the released UNIMOC simulation: pause, advance, reset, run."""

from __future__ import annotations

from sim.conftest import Ctl

DRIVE = 10


def test_pause_advance_reset_and_run(ctl: Ctl) -> None:
    paused = ctl.json("sim", str(DRIVE), "pause")
    assert paused["paused"] and paused["cycle_period_ns"] == 62500
    again = ctl.json("sim", str(DRIVE), "status")
    assert again["cycle_count"] == paused["cycle_count"]  # the clock stands

    advanced = ctl.json("sim", str(DRIVE), "advance", "20000")  # two writes of 16000 and 4000
    assert advanced["cycle_count"] == paused["cycle_count"] + 20000

    reset = ctl.json("sim", str(DRIVE), "reset")
    assert reset["cycle_count"] == 0 and reset["paused"]  # a pause survives the reset

    running = ctl.json("sim", str(DRIVE), "run")
    assert not running["paused"]
    assert ctl.json("sim", str(DRIVE), "status")["cycle_count"] > running["cycle_count"]


def test_advance_needs_a_pause(ctl: Ctl) -> None:
    ctl.json("sim", str(DRIVE), "run")
    code, _ = ctl.run("sim", str(DRIVE), "advance", "100")
    assert code == 2
