# SPDX-License-Identifier: Apache-2.0
"""The released UNIMOC native_sim drive for the tests (Linux only).

The binary is ``CYPILOT_SIM_EXE`` or the native_sim asset of the UNIMOC release
cypilot pins (cypilot.dsdl.DEFAULT_UNIMOC_REF), downloaded into the cache. The
tests talk to it over its first slcan pseudo terminal (classic CAN).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cypilot import cli
from cypilot.simulation import SimInstance, SimulationError, fetch_release

pytestmark = pytest.mark.sim


@pytest.fixture(scope="session")
def sim_executable() -> Path:
    if sys.platform != "linux":
        pytest.skip("native_sim runs on Linux only")
    configured = os.environ.get("CYPILOT_SIM_EXE")
    if configured:
        return Path(configured)
    try:
        return fetch_release()
    except SimulationError as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"no simulation: {error}")


@pytest.fixture(scope="module")
def sim(sim_executable: Path, tmp_path_factory: pytest.TempPathFactory, dsdl: Path) -> Iterator[SimInstance]:
    flash = tmp_path_factory.mktemp("sim") / "flash.bin"
    with SimInstance(sim_executable, flash) as instance:
        yield instance


class Ctl:
    """Runs ``cypilot-ctl`` in this process against the simulation."""

    def __init__(self, sim: SimInstance, capsys: pytest.CaptureFixture[str]) -> None:
        self.sim = sim
        self.capsys = capsys

    def run(self, *args: str) -> tuple[int, str]:
        self.capsys.readouterr()
        code = cli.main(["-i", self.sim.slcan_interfaces[0], *args])
        return code, self.capsys.readouterr().out

    def json(self, *args: str) -> Any:
        code, out = self.run("--json", *args)
        assert code == 0, out
        return json.loads(out)

    def json_rc(self, expected: int, *args: str) -> Any:
        """``--json`` output of a command that exits with ``expected`` (1: differences found)."""
        code, out = self.run("--json", *args)
        assert code == expected, out
        return json.loads(out)

    def wait_for(self, predicate: Any, timeout: float = 20.0) -> Any:
        """Lists the nodes until ``predicate(nodes)`` holds."""
        deadline = time.monotonic() + timeout
        nodes = None
        while time.monotonic() < deadline:
            code, out = self.run("--json", "nodes", "--wait", "1.5")
            if code != 0:  # the pseudo terminal is gone while the simulation restarts
                time.sleep(0.5)
                continue
            nodes = json.loads(out)
            if predicate(nodes):
                return nodes
        raise AssertionError(f"no match within {timeout} s, last: {nodes}")


@pytest.fixture
def ctl(sim: SimInstance, capsys: pytest.CaptureFixture[str]) -> Ctl:
    return Ctl(sim, capsys)
