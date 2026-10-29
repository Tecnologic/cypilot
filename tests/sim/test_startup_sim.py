# SPDX-License-Identifier: Apache-2.0
"""The startup aid of the UNIMOC simulation over the web API: phase 1, stop all and the lease abort it.

Phase 2 is not run here: it forces duties on the simulated motor, which trips
the overcurrent limit of the startup aid (UNIMOC covers it with ztests).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import httpx

from cypilot.core.hub import BusHub
from cypilot.simulation import SimInstance
from cypilot.web.app import create_app

DRIVE = 10
OPERATOR = {"X-Cypilot-Client": "operator"}
URL = f"/api/nodes/{DRIVE}/startup"


async def _step(client: httpx.AsyncClient, name: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        state: dict[str, Any] = (await client.get(URL)).json()
        if state["step_name"] == name:
            return state
        assert asyncio.get_running_loop().time() < deadline, f"not {name}: {state}"
        await asyncio.sleep(0.1)


def test_phase_1_of_the_startup_aid_and_its_aborts(sim: SimInstance, dsdl: Path) -> None:
    async def go() -> None:
        hub = BusHub(lease_time=1.0)
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
                started = await client.post(URL, json={"action": "start"}, headers=OPERATOR)
                assert started.status_code == 200, started.text
                assert started.json()["step_name"] == "PWM_DISABLE"

                # One sample per millisecond: about a second per measuring step.
                for name in ("ADC_OFFSET_CAL", "ADC_NOISE_FLOOR", "DUTY_FORCE_LOW", "DUTY_FORCE_MID"):
                    await client.post(URL, json={"action": "next"}, headers=OPERATOR)
                    await _step(client, name)
                state = (await client.get(URL)).json()
                assert state["running"] and state["passed"]["ADC_OFFSET_CAL"]

                # Stop all aborts the run (readiness SLEEP alone would not).
                assert DRIVE in (await client.post("/api/stop")).json()["stopped"]
                state = await _step(client, "FAULT")
                assert not state["running"]

                # A run of the operator ends with its lease.
                await client.post(URL, json={"action": "start"}, headers=OPERATOR)
                await _step(client, "PWM_DISABLE")
                renewer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewer
                await _step(client, "FAULT", timeout=3.0)
        finally:
            await hub.close()

    asyncio.run(go())
