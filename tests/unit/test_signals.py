# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math
from pathlib import Path

import pytest

from cypilot.core.registers import Register, RegisterValue
from cypilot.core.signals import Recorder, SignalError, Source, flatten, load_type, sources

TELEMETRY = Source(10, "telemetry", 212, "unimoc.drive.Telemetry.0.1")
STATUS = Source(11, "drive_status", 213, "unimoc.drive.Status.0.1")


def port(kind: str, name: str, port_id: int, type_name: str) -> list[Register]:
    return [
        Register(f"uavcan.{kind}.{name}.id", RegisterValue("natural16", [port_id]), True, True),
        Register(f"uavcan.{kind}.{name}.type", RegisterValue("string", type_name), False, False),
    ]


def test_sources_are_the_published_typed_ports() -> None:
    registers = [
        *port("pub", "telemetry", 212, "unimoc.drive.Telemetry.0.1"),
        *port("pub", "off", 65535, "uavcan.primitive.scalar.Real32.1.0"),
        *port("sub", "torque_setpoint", 100, "uavcan.si.unit.torque.Scalar.1.0"),
        Register("uavcan.pub.untyped.id", RegisterValue("natural16", [300]), True, True),
    ]
    assert sources(registers, 10) == [TELEMETRY]


def test_load_type_and_flatten(dsdl: Path) -> None:
    telemetry = load_type("unimoc.drive.Telemetry.0.1")
    row = flatten(telemetry(control_cycles=7, i_q=1.5, engaged=True))
    assert row["control_cycles"] == 7.0
    assert row["i_q"] == 1.5
    assert row["engaged"] == 1.0
    assert row["overcurrent_tripped"] == 0.0
    vector = load_type("uavcan.si.unit.velocity.Vector3.1.0")
    assert flatten(vector(meter_per_second=[1.0, 2.0, 3.0])) == {
        "meter_per_second[0]": 1.0,
        "meter_per_second[1]": 2.0,
        "meter_per_second[2]": 3.0,
    }
    with pytest.raises(SignalError):
        load_type("unimoc.drive.NoSuchType.0.1")
    with pytest.raises(SignalError):
        load_type("telemetry")


def test_recorder_keeps_its_history_and_series() -> None:
    recorder = Recorder(history=10.0, clock=lambda: 0.0)
    for second in range(30):
        recorder.add(TELEMETRY, {"i_q": float(second)}, stamp=float(second))
    recorder.add(TELEMETRY, {"i_q": 30.0, "u_q": 1.0}, stamp=30.0)
    assert recorder.source_keys() == ["10/telemetry"]
    assert recorder.count("10/telemetry") == 11  # 20 .. 30
    assert recorder.fields("10/telemetry") == ["i_q", "u_q"]
    assert (recorder.first_time("10/telemetry"), recorder.last_time("10/telemetry")) == (20.0, 30.0)
    assert recorder.last_time("nothing") is None
    times, values = recorder.series("10/telemetry", "u_q", since=29.0)
    assert times == [29.0, 30.0]
    assert math.isnan(values[0]) and values[1] == 1.0
    assert recorder.series("nothing", "i_q") == ([], [])
    recorder.mark("old", stamp=5.0)
    recorder.mark("new", stamp=30.0)
    assert recorder.markers == [(30.0, "new")]  # the history holds for markers too
    recorder.clear()
    assert recorder.source_keys() == []


def test_export_and_load_round_trip(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.add(TELEMETRY, {"i_q": 0.25, "engaged": 0.0}, stamp=100.0)
    recorder.add(TELEMETRY, {"i_q": 0.5, "engaged": 1.0}, stamp=100.05)
    recorder.add(STATUS, {"speed": -3.0}, stamp=100.01)
    recorder.mark("node 10: enabled", stamp=100.02)
    written = recorder.export(tmp_path)
    assert sorted(p.name for p in written) == [
        "node10_telemetry.csv",
        "node11_drive_status.csv",
        "recording.json",
    ]
    assert (tmp_path / "node10_telemetry.csv").read_text().splitlines()[0] == "time,i_q,engaged"

    loaded = Recorder.load(tmp_path)
    assert loaded.source_keys() == ["10/telemetry", "11/drive_status"]
    assert loaded.source("10/telemetry") == TELEMETRY
    assert loaded.series("10/telemetry", "i_q") == ([100.0, 100.05], [0.25, 0.5])
    assert loaded.series("11/drive_status", "speed") == ([100.01], [-3.0])
    assert loaded.markers == [(100.02, "node 10: enabled")]


def test_load_refuses_a_directory_without_a_recording(tmp_path: Path) -> None:
    with pytest.raises(SignalError):
        Recorder.load(tmp_path)
    (tmp_path / "recording.json").write_text('{"format": 99, "sources": []}')
    with pytest.raises(SignalError):
        Recorder.load(tmp_path)
