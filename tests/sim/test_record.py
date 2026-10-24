# SPDX-License-Identifier: Apache-2.0
"""cypilot-ctl record against the released UNIMOC simulation: CSV export and replay."""

from __future__ import annotations

from pathlib import Path

from cypilot.core.signals import Recorder
from sim.conftest import Ctl

DRIVE = 10


def test_record_exports_the_telemetry_and_loads_again(ctl: Ctl, tmp_path: Path) -> None:
    result = ctl.json("record", str(DRIVE), "telemetry", "--for", "2", "--out", str(tmp_path))
    assert list(result["samples"]) == [f"{DRIVE}/telemetry"]
    assert result["samples"][f"{DRIVE}/telemetry"] >= 10  # unimoc.telemetry.rate, 20 Hz by default

    loaded = Recorder.load(tmp_path)
    times, cycles = loaded.series(f"{DRIVE}/telemetry", "control_cycles")
    assert times == sorted(times)
    assert cycles == sorted(cycles) and cycles[-1] > cycles[0]  # the controller runs
    assert "engaged" in loaded.fields(f"{DRIVE}/telemetry")


def test_record_refuses_an_unknown_port(ctl: Ctl, tmp_path: Path) -> None:
    code, _ = ctl.run("record", str(DRIVE), "no_such_port", "--for", "0.1", "--out", str(tmp_path))
    assert code == 2
