# SPDX-License-Identifier: Apache-2.0
"""The web API against the released UNIMOC simulation: enable, setpoint, the lease and stop all."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx

from cypilot.core.hub import BusHub
from cypilot.simulation import SimInstance
from cypilot.web.app import create_app

DRIVE = 10
OPERATOR = {"X-Cypilot-Client": "operator"}


async def _engaged(hub: BusHub) -> bool:
    return any(n["node_id"] == DRIVE and "engaged" in n["chips"] for n in hub.nodes())


async def _wait(hub: BusHub, engaged: bool, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _engaged(hub) == engaged:
            return True
        await asyncio.sleep(0.05)
    return False


def test_operator_enables_sets_a_setpoint_and_the_lease_lapse_stops_the_drive(
    sim: SimInstance, dsdl: Path
) -> None:
    async def go() -> None:
        hub = BusHub(lease_time=0.5)
        transport = httpx.ASGITransport(app=create_app(hub))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
                connect = await client.post("/api/connect", json={"interfaces": [sim.slcan_interfaces[0]]})
                assert connect.status_code == 200, connect.text
                await hub.require_session().wait_for_node(DRIVE)

                async def renew() -> None:
                    while True:
                        hub.renew("operator")
                        await asyncio.sleep(0.1)

                renewer = asyncio.create_task(renew())
                await client.post("/api/control/take", headers=OPERATOR)
                enable = await client.post(f"/api/nodes/{DRIVE}/enable", headers=OPERATOR)
                assert enable.json()["engaged"] == [DRIVE], enable.text
                setpoint = {"mode": "current", "values": [0.0, 1.0], "ramp": 0.2}
                response = await client.post(f"/api/nodes/{DRIVE}/setpoint", json=setpoint, headers=OPERATOR)
                assert response.status_code == 200, response.text
                assert await _wait(hub, engaged=True)

                assert (await client.post("/api/stop")).json()["stopped"] == [DRIVE]
                assert await _wait(hub, engaged=False)

                await client.post(f"/api/nodes/{DRIVE}/enable", headers=OPERATOR)
                assert await _wait(hub, engaged=True)
                renewer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewer
                assert await _wait(hub, engaged=False)  # the lease lapsed
                assert hub.control_state()["operator"] is None
        finally:
            await hub.close()

    asyncio.run(go())
