# SPDX-License-Identifier: Apache-2.0
"""scripts/pi-setup.sh in its dry run: the files it writes and the commands it would run."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "pi-setup.sh"

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="a script for the Raspberry Pi (bash)")


def setup(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_files_of_the_pi(tmp_path: Path) -> None:
    out = setup(tmp_path, "--hostname", "cart", "--runs", "/mnt/ssd/runs", "--oscillator", "20000000")
    config = (tmp_path / "boot/firmware/config.txt").read_text()
    assert "dtparam=spi=on" in config
    assert "dtoverlay=mcp251xfd,spi0-0,oscillator=20000000,interrupt=25" in config
    network = (tmp_path / "etc/systemd/network/80-cypilot-can0.network").read_text()
    assert "Name=can0" in network and "BitRate=1000000" in network and "FDMode=yes" in network
    service = (tmp_path / "etc/systemd/system/cypilot.service").read_text()
    assert "--interface socketcan:can0 serve --host 0.0.0.0 --port 80" in service
    assert "Restart=always" in service and "CAP_NET_BIND_SERVICE" in service
    env = dict(
        line.split("=", 1)
        for line in (tmp_path / "etc/cypilot/cypilot.env").read_text().splitlines()
        if "=" in line
    )
    assert env["CYPILOT_RUNS"] == "/mnt/ssd/runs" and len(env["CYPILOT_PIN"]) == 6
    assert (tmp_path / "usr/local/sbin/cypilot-wifi-fallback").exists()
    assert "+ hostnamectl set-hostname cart" in out
    assert "+ nmcli connection add type wifi" in out and "802-11-wireless.mode ap" in out
    assert "cypilot-ctl dsdl" in out  # fetched while online
    assert f"http://cart.local/   PIN {env['CYPILOT_PIN']}" in out


def test_a_second_run_keeps_the_pin_and_replaces_its_lines(tmp_path: Path) -> None:
    setup(tmp_path, "--pin", "424242", "--psk", "a-long-key")
    setup(tmp_path, "--no-ap")
    env = (tmp_path / "etc/cypilot/cypilot.env").read_text()
    assert "CYPILOT_PIN=424242" in env and "CYPILOT_AP_PSK=a-long-key" in env
    assert (tmp_path / "boot/firmware/config.txt").read_text().count("dtoverlay=mcp251xfd") == 1


def test_bad_options_are_refused(tmp_path: Path) -> None:
    for args in (["--psk", "short"], ["--nonsense"]):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run", "--root", str(tmp_path), *args], capture_output=True, text=True
        )
        assert result.returncode == 2, result.stderr
