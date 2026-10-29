# SPDX-License-Identifier: Apache-2.0
"""cypilot-ctl adapters, monitor and scope against the demo device on a virtual bus (another thread)."""

from __future__ import annotations

import asyncio
import csv
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from demo_bus import DEVICE_ID, new_channel

from cypilot import cli


@pytest.fixture
def channel(dsdl: Path) -> Iterator[str]:
    """A demo device on its own virtual bus, run by an event loop in a thread."""
    from cypilot.core.transport import make_transport, parse_interface
    from cypilot.demo import DemoDevice

    name = new_channel()
    loop = asyncio.new_event_loop()
    started = threading.Event()
    devices: list[DemoDevice] = []

    def serve() -> None:
        asyncio.set_event_loop(loop)
        device = DemoDevice(make_transport(parse_interface(name), DEVICE_ID))
        loop.call_soon(device.start)
        loop.call_soon(started.set)
        devices.append(device)
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    started.wait(5)
    yield name

    async def stop() -> None:  # close the device and end its tasks before the loop stops
        devices[0].close()
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def ctl(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(list(args))
    return code, capsys.readouterr().out


def test_monitor_receives_the_telemetry(channel: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = ctl(
        capsys, "-i", channel, "--json", "monitor", "1000", "uavcan.primitive.array.Real32", "--for", "1"
    )
    result = json.loads(out)
    assert code == 0 and result["type"] == "uavcan.primitive.array.Real32.1.0"
    assert result["messages"] >= 10 and result["sources"] == [DEVICE_ID]  # 20 Hz
    assert len(result["last"]["value"]) == 3


def test_scope_lists_the_variables_and_captures_to_csv(
    channel: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out = ctl(capsys, "-i", channel, "scope", str(DEVICE_ID))
    assert code == 0 and "sine [A]" in out and "tick 100 us" in out

    target = tmp_path / "capture.csv"
    code, out = ctl(
        capsys,
        "-i",
        channel,
        "--json",
        "scope",
        str(DEVICE_ID),
        "0",
        "1",
        "--samples",
        "50",
        "--out",
        str(target),
    )
    assert code == 0 and json.loads(out)["samples"] == 50
    rows = list(csv.reader(target.open()))
    assert rows[0] == ["time", "var0", "var1"] and len(rows) == 51


def test_scope_of_a_missing_node_fails(channel: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = ctl(capsys, "-i", channel, "scope", "99")
    assert code == 2


def scripted(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Answers ``cypilot-ctl startup run`` in order; the end of the list is the end of the input."""
    asked: list[str] = []

    async def ask(prompt: str) -> str:
        asked.append(prompt)
        if not answers:
            raise EOFError("end of the input")
        return answers.pop(0)

    monkeypatch.setattr(cli, "_ask", ask)
    return asked


def test_startup_run_walks_the_steps_to_done(
    channel: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Disconnected; six steps; the DC link and its step; the gate step; phase 2; the clamp meter.
    asked = scripted(monkeypatch, ["yes", *[""] * 6, "48", "", "", "yes", "", "0.85", ""])
    code, out = ctl(capsys, "-i", channel, "--json", "startup", str(DEVICE_ID), "run")
    result = json.loads(out)
    assert code == 0, out
    assert result["step_name"] == "DONE" and result["calibration"]["gain_a"] == pytest.approx(1.02)
    assert any("multimeter" in prompt for prompt in asked) and any(
        "clamp meter" in prompt for prompt in asked
    )
    assert any("Phase 2 puts voltage" in prompt for prompt in asked)


def test_startup_run_aborts_at_the_end_of_the_input_and_without_the_motor_confirmation(
    channel: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ["yes", ""])  # one step, then the input ends (as Ctrl+C)
    code, _ = ctl(capsys, "-i", channel, "startup", str(DEVICE_ID), "run")
    assert code == 2
    code, out = ctl(capsys, "-i", channel, "--json", "startup", str(DEVICE_ID))
    assert code == 0 and json.loads(out)["step_name"] == "FAULT"

    scripted(monkeypatch, ["no"])  # the motor is still connected: not started
    code, _ = ctl(capsys, "-i", channel, "startup", str(DEVICE_ID), "run")
    assert code == 2

    scripted(monkeypatch, ["yes", *[""] * 6, "48", "", "", "no"])  # phase 2 not confirmed
    code, _ = ctl(capsys, "-i", channel, "startup", str(DEVICE_ID), "run")
    assert code == 2
    code, out = ctl(capsys, "-i", channel, "--json", "startup", str(DEVICE_ID))
    assert json.loads(out)["step_name"] == "FAULT"


def test_stop_aborts_a_running_startup_aid(channel: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = ctl(capsys, "-i", channel, "set", str(DEVICE_ID), "unimoc.startup.run", "1")
    assert code == 0
    code, out = ctl(capsys, "-i", channel, "--json", "stop")
    assert code == 0 and json.loads(out)["startup_aborted"] == [DEVICE_ID]
