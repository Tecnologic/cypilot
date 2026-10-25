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
