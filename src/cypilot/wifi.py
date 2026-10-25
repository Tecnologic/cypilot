# SPDX-License-Identifier: Apache-2.0
"""Wi-Fi of the Raspberry Pi on the cart, over NetworkManager (``nmcli``), for the web app.

The Pi joins a known network when one is in range, else it opens its own
access point, the NetworkManager connection ``cypilot-ap`` that
``scripts/pi-setup.sh`` creates (``docs/pi.md``). This module lets the web
app show the state, scan, join and forget networks and change the access
point. ``nmcli`` runs with an argument list, never through a shell. The
service user needs the polkit rule of the setup script.

Joining a network can cut off the browsers that use the Pi's access point.
When the join fails and the access point was up, it is opened again, so the
cart never ends up without a network.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import Any

AP_CONNECTION = "cypilot-ap"
AP_ADDRESS = "10.42.0.1"
"""The Pi's address on its own access point (NetworkManager's shared mode)."""
CONNECT_WAIT = 30
"""Seconds nmcli waits for a network to connect."""


class WifiError(RuntimeError):
    """nmcli is missing, refused or failed."""


@dataclass(frozen=True)
class Network:
    ssid: str
    signal: int
    security: str
    in_use: bool


def split_terse(line: str) -> list[str]:
    """The fields of one line of ``nmcli -t`` (``:`` separates, ``\\`` escapes ``:`` and ``\\``)."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def check_ssid(ssid: str) -> str:
    if not 1 <= len(ssid.encode()) <= 32:
        raise WifiError("an SSID has 1 to 32 bytes")
    return ssid


def check_key(key: str) -> str:
    if not 8 <= len(key) <= 63 or not key.isascii():
        raise WifiError("a WPA2 key has 8 to 63 ASCII characters")
    return key


class Wifi:
    """The Wi-Fi of one device (``wlan0``)."""

    def __init__(self, nmcli: str = "nmcli", device: str = "wlan0") -> None:
        self.nmcli = nmcli
        self.device = device
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return shutil.which(self.nmcli) is not None

    async def _run(self, *args: str, timeout: float = 15.0) -> str:
        if not self.available:
            raise WifiError("NetworkManager (nmcli) is not installed")
        process = await asyncio.create_subprocess_exec(
            self.nmcli, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise WifiError(f"nmcli {args[0]} did not finish within {timeout:g} s") from None
        if process.returncode != 0:
            message = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
            raise WifiError(message.removeprefix("Error: ") or f"nmcli failed ({process.returncode})")
        return out.decode(errors="replace")

    async def status(self) -> dict[str, Any]:
        """The device's state, its connection, whether that is the access point, and its addresses."""
        state, connection = "unavailable", ""
        for line in (await self._run("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")).splitlines():
            fields = split_terse(line)
            if len(fields) >= 4 and fields[0] == self.device:
                state, connection = fields[2], fields[3]
        addresses: list[str] = []
        if state == "connected":
            show = await self._run("-t", "-f", "IP4.ADDRESS", "device", "show", self.device)
            addresses = [split_terse(line)[-1].split("/")[0] for line in show.splitlines() if line]
        return {
            "device": self.device,
            "state": state,
            "connection": connection,
            "access_point": connection == AP_CONNECTION,
            "addresses": addresses,
        }

    async def known(self) -> list[dict[str, Any]]:
        """The saved networks the Pi joins when in range (the access point not among them)."""
        out = await self._run("-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show")
        known = []
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 3 and fields[1] == "802-11-wireless" and fields[0] != AP_CONNECTION:
                known.append({"name": fields[0], "autoconnect": fields[2] == "yes"})
        return known

    async def access_point(self) -> dict[str, Any]:
        """The SSID of the access point (its key stays in NetworkManager)."""
        try:
            out = await self._run("-t", "-g", "802-11-wireless.ssid", "connection", "show", AP_CONNECTION)
        except WifiError:
            return {"configured": False, "ssid": None, "address": AP_ADDRESS}
        return {"configured": True, "ssid": out.strip(), "address": AP_ADDRESS}

    async def scan(self) -> list[Network]:
        out = await self._run(
            "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", self.device,
            "--rescan", "yes", timeout=30.0,
        )  # fmt: skip
        best: dict[str, Network] = {}
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) < 4 or not fields[1]:
                continue  # a hidden network
            network = Network(fields[1], int(fields[2] or 0), fields[3] or "open", fields[0] == "*")
            old = best.get(network.ssid)
            if old is None or network.signal > old.signal or network.in_use:
                best[network.ssid] = network
        return sorted(best.values(), key=lambda n: (not n.in_use, -n.signal))

    async def connect(self, ssid: str, password: str | None) -> dict[str, Any]:
        """Joins a network and saves it; reopens the access point when that fails and it was up."""
        check_ssid(ssid)
        if password:
            check_key(password)
        async with self._lock:
            was_ap = (await self.status())["access_point"]
            args = ["--wait", str(CONNECT_WAIT), "device", "wifi", "connect", ssid, "ifname", self.device]
            if password:
                args += ["password", password]
            try:
                await self._run(*args, timeout=CONNECT_WAIT + 10)
            except WifiError as error:
                if was_ap:
                    try:
                        await self._run("connection", "up", AP_CONNECTION, timeout=CONNECT_WAIT + 10)
                    except WifiError as again:
                        raise WifiError(f"{error}; the access point did not come back: {again}") from None
                    raise WifiError(f"{error}; the access point is open again") from None
                raise
        return await self.status()

    async def forget(self, name: str) -> None:
        if name == AP_CONNECTION:
            raise WifiError("the access point is not a known network; change it instead")
        if name not in {k["name"] for k in await self.known()}:
            raise WifiError(f"no known network {name!r}")
        await self._run("connection", "delete", name)

    async def set_access_point(self, ssid: str | None, key: str | None) -> dict[str, Any]:
        """Changes the SSID and the key; they apply the next time the access point opens."""
        settings: list[str] = []
        if ssid is not None:
            settings += ["802-11-wireless.ssid", check_ssid(ssid)]
        if key is not None:
            settings += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", check_key(key)]
        if settings:
            await self._run("connection", "modify", AP_CONNECTION, *settings)
        return await self.access_point()

    async def start_access_point(self) -> dict[str, Any]:
        async with self._lock:
            await self._run("connection", "up", AP_CONNECTION, timeout=CONNECT_WAIT + 10)
        return await self.status()
