# SPDX-License-Identifier: Apache-2.0
"""Runs: the store on disk, and recording a run of the demo devices through the web API."""

from __future__ import annotations

import asyncio
import gc
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from demo_bus import DEVICE_ID

from cypilot.core.hub import BusHub
from cypilot.core.runs import RunError, RunLimits, RunStore
from cypilot.core.signals import Source
from cypilot.web.app import create_app

OPERATOR = {"X-Cypilot-Client": "operator"}


CAPTURE = SimpleNamespace(t=[0.0, 0.001], channels=[[1.0, 2.0], [3.0, 4.0]])


def test_a_run_is_written_closed_packed_and_read_back(tmp_path: Path) -> None:
    clock = iter(float(t) for t in range(1000, 2000))
    store = RunStore(tmp_path, clock=lambda: next(clock))
    run = store.start("bench", "first try", {"interface": "virtual:x"})
    with pytest.raises(RunError, match="records already"):
        store.start()
    source = Source(10, "telemetry", 1000, "uavcan.primitive.array.Real32.1.0")
    run.frame(1000.5, 0, 0x107D550A, bytes([1, 2, 3, 0xE0]), fd=False)
    run.frame(1000.6, 1, 0x107D550A, bytes(range(12)) + bytes(4), fd=True)
    run.sample(source, 1000.5, {"value[0]": 1.5, "value[1]": 2.0})
    run.sample(source, 1000.6, {"value[0]": 2.5})
    run.mark("control taken", 1000.55, kind="control")
    run.add_capture(10, CAPTURE, ["sine", "cosine"])
    run.set_node(10, {"info": {"name": "demo"}})
    run.flush()
    assert store.summaries()[0]["state"] == "recording"
    with pytest.raises(RunError, match="stop"):
        store.delete(run.run_id)

    summary = store.stop()
    assert summary is not None and summary["state"] == "closed" and summary["nodes"] == [10]
    run_id = summary["id"]
    names = {f["name"] for f in store.files(run_id)}
    assert {
        "run.json",
        "frames.log",
        "node10_telemetry.csv",
        "recording.json",
        "capture1_node10.csv",
    } <= names
    frames = store.read_file(run_id, "frames.log").decode().splitlines()
    assert frames[0] == "(1000.500000) can0 107D550A#010203E0"
    assert frames[1].startswith("(1000.600000) can1 107D550A##0000102030405060708090A0B")
    meta = json.loads(store.read_file(run_id, "run.json"))
    assert meta["name"] == "bench" and meta["interface"] == "virtual:x"
    assert [m["text"] for m in meta["markers"]][:2] == ["started", "control taken"]
    assert meta["stop_reason"] == "stopped by the user"

    recorder = store.recorder(run_id)
    times, values = recorder.series("10/telemetry", "value[0]")
    assert times == [1000.5, 1000.6] and values == [1.5, 2.5]

    with zipfile.ZipFile(io.BytesIO(b"".join(store.archive(run_id)))) as archive:
        assert f"{run_id}/frames.log" in archive.namelist() and f"{run_id}/run.json" in archive.namelist()

    assert store.update(run_id, name="renamed", notes="ok")["name"] == "renamed"
    assert RunStore(tmp_path).summaries()[0]["notes"] == "ok"
    store.delete(run_id)
    assert store.summaries() == []
    with pytest.raises(LookupError):
        store.files("../etc")


def test_a_run_left_recording_is_closed_and_kept(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    run = store.start("crash")
    run.sample(Source(10, "telemetry", 1000, "x.Y.1.0"), 5.0, {"a": 1.0})
    run.flush()  # cypilot stops here without stop() ...
    del run, store  # ... and its open files go with the process (Windows keeps open files from deletion)
    gc.collect()
    again = RunStore(tmp_path)
    (summary,) = again.summaries()
    assert summary["state"] == "closed" and summary["stop_reason"] == "cypilot stopped while recording"
    assert again.active is None
    assert "node10_telemetry.csv" in {f["name"] for f in again.files(summary["id"])}


def test_no_run_starts_without_free_space(tmp_path: Path) -> None:
    store = RunStore(tmp_path, RunLimits(warn_free=1 << 62, stop_free=1 << 62))
    assert store.space()["warn"] is True
    with pytest.raises(RunError, match="free space"):
        store.start()


def test_a_run_of_the_demo_devices_over_the_api(dsdl: Path, tmp_path: Path) -> None:
    async def body() -> None:
        hub = BusHub(lease_time=1.0, runs=RunStore(tmp_path))
        transport = httpx.ASGITransport(app=create_app(hub))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
                assert (await client.post("/api/runs/start")).status_code == 503  # no bus
                await client.post("/api/connect", json={"demo_devices": 1})
                await hub.require_session().wait_for_node(DEVICE_ID)
                assert (await client.get("/api/runs")).json()["active"] is None  # never by itself

                started = (await client.post("/api/runs/start", json={"name": "demo"})).json()
                assert started["state"] == "recording"
                assert (await client.post("/api/runs/start")).status_code == 409
                await client.post("/api/control/take", headers=OPERATOR)
                hub.renew("operator")
                await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=OPERATOR)
                await client.post("/api/runs/mark", json={"text": "step"})
                capture = {"channels": [0], "sample_period_us": 100, "num_samples": 20}
                assert (
                    await client.post(f"/api/nodes/{DEVICE_ID}/scope/capture", json=capture)
                ).status_code == 200
                await client.post("/api/stop")
                await asyncio.sleep(1.5)
                active = (await client.get("/api/runs")).json()["active"]
                assert active["frames"] > 0 and active["samples"] > 0 and active["size"] > 0

                stopped = (await client.post("/api/runs/stop")).json()
                run_id = stopped["id"]
                assert (await client.post("/api/runs/stop")).status_code == 409
                detail = (await client.get(f"/api/runs/{run_id}")).json()
                markers = [m["text"] for m in detail["markers"]]
                assert (
                    "control taken" in markers
                    and "step" in markers
                    and any(m.startswith("STOP ALL") for m in markers)
                )
                assert "registers" in detail["nodes"][str(DEVICE_ID)]
                files = {f["name"] for f in detail["files"]}
                assert {"frames.log", "node10_telemetry.csv", "capture1_node10.csv"} <= files

                download = await client.get(f"/api/runs/{run_id}/download")
                assert download.headers["content-type"] == "application/zip"
                with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                    assert f"{run_id}/node10_telemetry.csv" in archive.namelist()
                frames = (await client.get(f"/api/runs/{run_id}/files/frames.log")).text
                assert frames.startswith("(") and " can0 " in frames

                replay: dict[str, Any] = (await client.get(f"/api/runs/{run_id}/replay")).json()
                (telemetry,) = [s for s in replay["sources"] if s["port"] == "telemetry"]
                assert len(telemetry["t"]) > 10 and "value[0]" in telemetry["fields"]
                assert any(m["text"] == "step" for m in replay["markers"])

                assert (await client.patch(f"/api/runs/{run_id}", json={"name": "renamed"})).json()[
                    "name"
                ] == "renamed"
                assert (await client.delete(f"/api/runs/{run_id}")).json() == {"ok": True}
                assert (await client.get("/api/runs")).json()["runs"] == []
                assert (await client.get(f"/api/runs/{run_id}")).status_code == 400
        finally:
            await hub.close()

    asyncio.run(body())
