# SPDX-License-Identifier: Apache-2.0
"""The web API against the hub with demo devices on a virtual bus."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from demo_bus import DEVICE_ID, wait_for

from cypilot.core.hub import BusHub
from cypilot.web.app import create_app

OPERATOR = {"X-Cypilot-Client": "operator"}
VIEWER = {"X-Cypilot-Client": "viewer"}


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


@contextlib.asynccontextmanager
async def web(
    *, connect: bool = True, pin: str | None = None, lease_time: float = 1.0
) -> AsyncIterator[tuple[BusHub, httpx.AsyncClient]]:
    hub = BusHub(lease_time=lease_time)
    transport = httpx.ASGITransport(app=create_app(hub, pin=pin))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
            if connect:
                response = await client.post("/api/connect", json={"demo_devices": 1})
                assert response.status_code == 200, response.text
                await hub.require_session().wait_for_node(DEVICE_ID)
            yield hub, client
    finally:
        await hub.close()


@contextlib.asynccontextmanager
async def renewing(hub: BusHub, client: str) -> AsyncIterator[None]:
    """What the WebSocket of a client does: renew every 100 ms."""

    async def renew() -> None:
        while True:
            hub.renew(client)
            await asyncio.sleep(0.1)

    task = asyncio.create_task(renew())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def engaged(client: httpx.AsyncClient) -> bool:
    response = await client.get(f"/api/nodes/{DEVICE_ID}/registers/demo.engaged")
    return bool(response.json()["value"][0])


def test_status_and_503_without_a_bus(dsdl: Path) -> None:
    async def body() -> None:
        async with web(connect=False) as (_, client):
            status = (await client.get("/api/status")).json()
            assert status["connected"] is False and status["control"]["operator"] is None
            assert (await client.get("/api/nodes")).status_code == 503
            assert (await client.post("/api/stop")).status_code == 503
            assert (await client.get("/")).status_code == 200  # the static UI

    run(body())


def test_nodes_and_registers(dsdl: Path) -> None:
    async def body() -> None:
        async with web() as (_, client):
            nodes = (await client.get("/api/nodes")).json()
            node = next(n for n in nodes if n["node_id"] == DEVICE_ID)
            assert node["unimoc"] is True and node["name"] == "org.tecnologic.cypilot.demo0"

            registers = (await client.get(f"/api/nodes/{DEVICE_ID}/registers")).json()
            names = {register["name"] for register in registers}
            assert {"demo.amplitude", "demo.frequency", "unimoc.settings.pending"} <= names
            assert (await client.get(f"/api/nodes/{DEVICE_ID}/registers/no.such")).status_code == 404
            assert (await client.get("/api/nodes/99/registers")).status_code == 404

            write = {"value": "2.5"}
            url = f"/api/nodes/{DEVICE_ID}/registers/demo.amplitude"
            assert (await client.put(url, json=write, headers=VIEWER)).status_code == 403
            assert (await client.post("/api/control/take", headers=OPERATOR)).status_code == 200
            result = (await client.put(url, json=write, headers=OPERATOR)).json()
            assert result["accepted"] is True and result["register"]["value"] == [2.5]
            read_only = f"/api/nodes/{DEVICE_ID}/registers/demo.store_count"
            assert (await client.put(read_only, json={"value": 3}, headers=OPERATOR)).status_code == 400

    run(body())


def test_one_operator_and_take_over(dsdl: Path) -> None:
    async def body() -> None:
        async with web() as (_, client):
            assert (await client.post("/api/control/take")).status_code == 400  # no client name
            assert (await client.post("/api/control/take", headers=OPERATOR)).json()["operator"] == "operator"
            assert (await client.post("/api/control/take", headers=VIEWER)).status_code == 403
            assert (await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=VIEWER)).status_code == 403
            taken = await client.post("/api/control/take", headers=VIEWER, json={"force": True})
            assert taken.json()["operator"] == "viewer"
            assert (await client.post("/api/control/release", headers=VIEWER)).json()["operator"] is None

    run(body())


def test_enable_lease_lapse_and_stops(dsdl: Path) -> None:
    async def body() -> None:
        async with web(lease_time=0.5) as (hub, client):
            async with renewing(hub, "operator"):
                await client.post("/api/control/take", headers=OPERATOR)
                state = (await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=OPERATOR)).json()
                assert state["engaged"] == [DEVICE_ID] and await engaged(client)
                setpoint = {"mode": "speed", "values": [1.0]}
                response = await client.post(
                    f"/api/nodes/{DEVICE_ID}/setpoint", json=setpoint, headers=OPERATOR
                )
                assert response.status_code == 400  # the demo device has no setpoint subscriptions
            # The renewals stop: the lease lapses and the drive goes to sleep.
            await wait_for(lambda: hub.control_state() == {"operator": None, "engaged": []}, timeout=3.0)
            await wait_for_async(lambda: not_engaged(client))

            async with renewing(hub, "operator"):
                await client.post("/api/control/take", headers=OPERATOR)
                await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=OPERATOR)
                assert (await client.post("/api/stop", headers=VIEWER)).json()["stopped"] == [DEVICE_ID]
                await wait_for_async(lambda: not_engaged(client))

                await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=OPERATOR)
                await hub.client_gone("operator")  # the operator's WebSocket closed
                assert hub.control_state()["operator"] is None
                await wait_for_async(lambda: not_engaged(client))

            estop = (await client.post("/api/estop", headers=VIEWER)).json()
            assert estop["nodes"] == {str(DEVICE_ID): 0}
            await wait_for(lambda: node_vendor(hub) & 0x20 != 0)  # E-stop latched

    run(body())


async def not_engaged(client: httpx.AsyncClient) -> bool:
    return not await engaged(client)


def node_vendor(hub: BusHub) -> int:
    return int(next(n["vendor_code"] for n in hub.nodes() if n["node_id"] == DEVICE_ID))


async def wait_for_async(condition: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


def test_commands_need_the_operator_but_the_emergency_stop(dsdl: Path) -> None:
    async def body() -> None:
        async with web() as (_, client):
            url = f"/api/nodes/{DEVICE_ID}/command"
            assert (await client.post(url, json={"command": "store"}, headers=VIEWER)).status_code == 403
            assert (await client.post(url, json={"command": "nonsense"})).status_code == 400
            result = (await client.post(url, json={"command": "emergency-stop"}, headers=VIEWER)).json()
            assert result["ok"] is True

    run(body())


def test_pin_login(dsdl: Path) -> None:
    async def body() -> None:
        async with web(connect=False, pin="1234") as (_, client):
            assert (await client.get("/api/auth")).json() == {"required": True, "ok": False}
            assert (await client.get("/api/status")).status_code == 401
            assert (await client.post("/api/login", json={"pin": "0000"})).status_code == 403
            token = (await client.post("/api/login", json={"pin": "1234"})).json()["token"]
            assert token and (await client.get("/api/status")).status_code == 200  # the cookie
            client.cookies.clear()
            bearer = {"Authorization": f"Bearer {token}"}
            assert (await client.get("/api/status", headers=bearer)).status_code == 200

    run(body())


def test_scope_capture_and_subjects(dsdl: Path) -> None:
    async def body() -> None:
        async with web() as (hub, client):
            info = (await client.get(f"/api/nodes/{DEVICE_ID}/scope")).json()
            assert [v["name"] for v in info["variables"]][:2] == ["sine", "cosine"]
            capture = {"channels": [0, 1], "sample_period_us": 100, "num_samples": 50}
            result = (await client.post(f"/api/nodes/{DEVICE_ID}/scope/capture", json=capture)).json()
            assert len(result["t"]) == 50 and len(result["channels"]) == 2

            async with hub.events.listen() as queue:
                subscription = {"subject_id": 1000, "type": "uavcan.primitive.array.Real32"}
                assert (await client.post("/api/subjects", json=subscription)).status_code == 200
                while (event := await asyncio.wait_for(queue.get(), 3.0))["kind"] != "subject":
                    pass
                assert len(event["last"]["value"]) == 3

            publication = {
                "subject_id": 2000,
                "type": "uavcan.primitive.scalar.Real32",
                "value": {"value": 1},
            }
            assert (await client.post("/api/publishers", json=publication, headers=VIEWER)).status_code == 403
            await client.post("/api/control/take", headers=OPERATOR)
            started = (await client.post("/api/publishers", json=publication, headers=OPERATOR)).json()
            assert [p["id"] for p in (await client.get("/api/publishers")).json()] == [started["id"]]
            await client.post("/api/stop", headers=VIEWER)  # stop all ends the publications too
            assert (await client.get("/api/publishers")).json() == []

    run(body())


def test_the_websocket_renews_the_lease(dsdl: Path) -> None:
    import uvicorn
    import websockets

    async def body() -> None:
        hub = BusHub(lease_time=0.5)
        server = uvicorn.Server(uvicorn.Config(create_app(hub), port=0, log_level="warning", lifespan="off"))
        serving = asyncio.create_task(server.serve())
        try:
            await wait_for(lambda: server.started)
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                await client.post("/api/connect", json={"demo_devices": 1})
                await hub.require_session().wait_for_node(DEVICE_ID)
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client=operator") as ws:
                    hello = json.loads(await ws.recv())
                    assert hello["kind"] == "hello" and hello["client"] == "operator"

                    async def renew() -> None:
                        while True:
                            await ws.send(json.dumps({"type": "renew"}))
                            await asyncio.sleep(0.1)

                    renewer = asyncio.create_task(renew())
                    await client.post("/api/control/take", headers=OPERATOR)
                    await client.post(f"/api/nodes/{DEVICE_ID}/enable", headers=OPERATOR)
                    await asyncio.sleep(1.5)  # three lease times
                    assert hub.control_state() == {"operator": "operator", "engaged": [DEVICE_ID]}
                    renewer.cancel()
                # The WebSocket closed: the operator is gone and the drive sleeps.
                await wait_for(lambda: hub.control_state() == {"operator": None, "engaged": []}, timeout=3.0)
                await wait_for_async(lambda: not_engaged(client))
        finally:
            server.should_exit = True
            await serving
            await hub.close()

    run(body())


def test_register_descriptions_from_a_file(dsdl: Path, tmp_path: Path, monkeypatch: Any) -> None:
    file = {
        "schema": 1,
        "firmware": {"name": "unimoc", "version": "0.3.0"},
        "registers": [
            {
                "name": "demo.amplitude",
                "type": "real32",
                "size": 1,
                "mutable": True,
                "persistent": False,
                "unit": "A",
                "help": "Amplitude of the sine",
            }
        ],
    }
    (tmp_path / "registers.json").write_text(json.dumps(file))
    monkeypatch.setenv("CYPILOT_REGISTERS", str(tmp_path / "registers.json"))

    async def body() -> None:
        async with web(connect=False) as (_, client):
            result = (await client.get("/api/descriptions")).json()
            assert result["available"] is True and result["firmware"]["version"] == "0.3.0"
            assert result["registers"]["demo.amplitude"]["unit"] == "A"

    run(body())

    monkeypatch.setenv("CYPILOT_REGISTERS", str(tmp_path / "missing.json"))

    async def missing() -> None:
        async with web(connect=False) as (_, client):
            result = (await client.get("/api/descriptions")).json()
            assert result["available"] is False and result["registers"] == {}

    run(missing())


def test_the_drive_target(dsdl: Path) -> None:
    async def body() -> None:
        async with web() as (_, client):
            target = (await client.get(f"/api/nodes/{DEVICE_ID}/target")).json()
            assert target == {"node_id": DEVICE_ID, "readiness": 106, "setpoints": {}}

    run(body())


def test_a_capture_of_two_nodes_is_stored_in_the_run(dsdl: Path, tmp_path: Path) -> None:
    from cypilot.core.runs import RunStore

    async def body() -> None:
        hub = BusHub(lease_time=1.0, runs=RunStore(tmp_path))
        transport = httpx.ASGITransport(app=create_app(hub))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
                await client.post("/api/connect", json={"demo_devices": 2})
                await hub.require_session().wait_for_node(DEVICE_ID + 1)
                state = (await client.get("/api/scope/trigger?nodes=10,11")).json()
                assert state["shared"] is False
                setup = {"nodes": [10, 11]}
                assert (
                    await client.post("/api/scope/trigger", json=setup, headers=VIEWER)
                ).status_code == 403
                await client.post("/api/control/take", headers=OPERATOR)
                hub.renew("operator")
                assert (
                    await client.post("/api/scope/trigger", json=setup, headers=OPERATOR)
                ).status_code == 200
                assert (await client.get("/api/scope/trigger?nodes=10,11")).json()["subject"] == 220

                await client.post("/api/runs/start")
                capture = {
                    "nodes": {"10": [0], "11": [1, 2]},
                    "trigger_node": 10,
                    "sample_period_us": 100,
                    "num_samples": 40,
                    "trigger_mode": "rising",
                    "trigger_level": 0.5,
                }
                result = (await client.post("/api/scope/capture", json=capture)).json()
                assert set(result["captures"]) == {"10", "11"}
                assert len(result["captures"]["11"]["channels"]) == 2
                run_id = (await client.post("/api/runs/stop")).json()["id"]
                files = {f["name"] for f in (await client.get(f"/api/runs/{run_id}")).json()["files"]}
                assert {"capture1_node10.csv", "capture2_node11.csv"} <= files
                header = (
                    await client.get(f"/api/runs/{run_id}/files/capture2_node11.csv")
                ).text.splitlines()[0]
                assert header == "t_s,cosine,ramp"
        finally:
            await hub.close()

    run(body())


async def startup_running(client: httpx.AsyncClient) -> bool:
    return bool((await client.get(f"/api/nodes/{DEVICE_ID}/startup")).json()["running"])


def test_the_startup_aid_needs_the_operator_and_every_stop_aborts_it(dsdl: Path) -> None:
    async def body() -> None:
        async with web(lease_time=0.5) as (hub, client):
            url = f"/api/nodes/{DEVICE_ID}/startup"
            state = (await client.get(url)).json()
            assert state["step_name"] == "IDLE" and state["running"] is False
            assert (await client.post(url, json={"action": "start"}, headers=VIEWER)).status_code == 403

            async with renewing(hub, "operator"):
                await client.post("/api/control/take", headers=OPERATOR)
                state = (await client.post(url, json={"action": "start"}, headers=OPERATOR)).json()
                assert state["running"] is True and state["step_name"] == "PWM_DISABLE"
                assert "scope" in state["prompt"]
                # The generic register editor: a step write answers with the step, and is accepted.
                step = f"/api/nodes/{DEVICE_ID}/registers/unimoc.startup.step"
                result = (await client.put(step, json={"value": 1}, headers=OPERATOR)).json()
                assert result["accepted"] is True and result["register"]["value"] == [1]
                await wait_for_async(lambda: _step_is(client, "ADC_OFFSET_CAL"), timeout=3.0)
                # Readiness SLEEP does not stop it; stop all (any client) aborts it.
                assert DEVICE_ID in (await client.post("/api/stop", headers=VIEWER)).json()["stopped"]
                state = (await client.get(url)).json()
                assert state["running"] is False and state["step_name"] == "FAULT"

                # The abort is open to every client.
                await client.post(url, json={"action": "start"}, headers=OPERATOR)
                assert (await client.post(url, json={"action": "abort"}, headers=VIEWER)).json()[
                    "step_name"
                ] == "FAULT"

                await client.post(url, json={"action": "start"}, headers=OPERATOR)
                assert await startup_running(client)
            # The renewals stop: the lease lapses and the run is aborted.
            await wait_for_async(lambda: _not(startup_running(client)), timeout=3.0)
            assert (await client.get(url)).json()["step_name"] == "FAULT"

            # Released control and a run started from the register editor: aborted too.
            async with renewing(hub, "operator"):
                await client.post("/api/control/take", headers=OPERATOR)
                run_register = f"/api/nodes/{DEVICE_ID}/registers/unimoc.startup.run"
                assert (await client.put(run_register, json={"value": 1}, headers=OPERATOR)).json()[
                    "accepted"
                ]
                await client.post("/api/control/release", headers=OPERATOR)
                assert not await startup_running(client)

    run(body())


async def _step_is(client: httpx.AsyncClient, name: str) -> bool:
    return bool((await client.get(f"/api/nodes/{DEVICE_ID}/startup")).json()["step_name"] == name)


async def _not(awaitable: Any) -> bool:
    return not await awaitable
