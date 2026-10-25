# SPDX-License-Identifier: Apache-2.0
"""The Wi-Fi of the Pi over nmcli (a fake nmcli), and its web API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cypilot.core.hub import BusHub
from cypilot.web.app import create_app
from cypilot.wifi import Wifi, WifiError, split_terse

OPERATOR = {"X-Cypilot-Client": "operator"}


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_terse_fields_with_escapes() -> None:
    assert split_terse(r"*:Cafe\:Gast:40:") == ["*", "Cafe:Gast", "40", ""]
    assert split_terse(r"a\\b:c") == ["a\\b", "c"]


def test_status_scan_known_and_the_access_point(fake_nmcli: tuple[Path, Path]) -> None:
    _, nmcli = fake_nmcli
    wifi = Wifi(nmcli=str(nmcli))

    async def body() -> None:
        status = await wifi.status()
        assert status == {
            "device": "wlan0",
            "state": "connected",
            "connection": "cypilot-ap",
            "access_point": True,
            "addresses": ["10.42.0.1"],
        }
        assert [n.ssid for n in await wifi.scan()] == ["office", "workshop", "Cafe:Gast"]
        assert await wifi.known() == [{"name": "workshop", "autoconnect": True}]
        assert (await wifi.access_point())["ssid"] == "cypilot"

    run(body())


def test_connect_and_the_fallback_to_the_access_point(fake_nmcli: tuple[Path, Path]) -> None:
    state, nmcli = fake_nmcli
    wifi = Wifi(nmcli=str(nmcli))

    async def body() -> None:
        with pytest.raises(WifiError, match="access point is open again"):
            await wifi.connect("office", "wrong-key")
        assert (await wifi.status())["access_point"] is True  # the cart is not left without a network
        status = await wifi.connect("office", "secret-office")
        assert status["connection"] == "office" and status["addresses"] == ["192.168.1.50"]
        assert {k["name"] for k in await wifi.known()} == {"workshop", "office"}
        with pytest.raises(WifiError, match="No network"):
            await wifi.connect("nowhere", "some-key-1")  # not on the access point now: no fallback
        await wifi.forget("workshop")
        with pytest.raises(WifiError, match="not a known network"):
            await wifi.forget("cypilot-ap")
        await wifi.set_access_point("cart", "new-key-123")
        assert json.loads(state.read_text())["ap_psk"] == "new-key-123"
        assert (await wifi.start_access_point())["access_point"] is True
        with pytest.raises(WifiError, match="8 to 63"):
            await wifi.set_access_point(None, "short")

    run(body())


def test_the_wifi_api_needs_control_for_changes(fake_nmcli: tuple[Path, Path], dsdl: Path) -> None:
    _, nmcli = fake_nmcli

    async def body() -> None:
        hub = BusHub(lease_time=1.0)
        transport = httpx.ASGITransport(app=create_app(hub, wifi=Wifi(nmcli=str(nmcli))))
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
                state = (await client.get("/api/wifi")).json()
                assert state["available"] and state["status"]["access_point"]
                assert state["access_point"]["ssid"] == "cypilot"
                assert len((await client.get("/api/wifi/scan")).json()) == 3
                join = {"ssid": "workshop", "password": "secret-workshop"}
                assert (await client.post("/api/wifi/connect", json=join)).status_code == 503  # no bus
                await client.post("/api/connect", json={"demo_devices": 1})
                assert (await client.post("/api/wifi/connect", json=join)).status_code == 403
                await client.post("/api/control/take", headers=OPERATOR)
                joined = await client.post("/api/wifi/connect", json=join, headers=OPERATOR)
                assert joined.json()["connection"] == "workshop"
                bad = await client.put("/api/wifi/access-point", json={"key": "short"}, headers=OPERATOR)
                assert bad.status_code == 422
                assert (
                    await client.post("/api/wifi/access-point/start", headers=OPERATOR)
                ).status_code == 200
        finally:
            await hub.close()

    run(body())


def test_without_wifi_management(dsdl: Path) -> None:
    async def body() -> None:
        hub = BusHub()
        transport = httpx.ASGITransport(app=create_app(hub))
        async with httpx.AsyncClient(transport=transport, base_url="http://cypilot") as client:
            assert (await client.get("/api/wifi")).json() == {"available": False}
            assert (await client.get("/api/wifi/scan")).status_code == 404

    run(body())
