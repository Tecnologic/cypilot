# SPDX-License-Identifier: Apache-2.0
"""The UNIMOC ``native_sim`` drive: download a release and run instances of it (Linux only).

Each instance gets its own flash file, so its settings survive a restart but no
two instances share them. Its CAN bus is reachable over the slcan pseudo
terminals it prints at boot (classic CAN, one tool per terminal) and, when the
host has the interface, on SocketCAN ``vcan0`` (``-can-if``).
"""

from __future__ import annotations

import collections
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import TracebackType

from cypilot.dsdl import DEFAULT_UNIMOC_REF, cache_root

_RELEASES = "https://github.com/Tecnologic/unimoc/releases/download"
_PTY = re.compile(r"^(uart(?:_\d)?) connected to pseudotty: (\S+)")
_NODE = re.compile(r"Cyphal node (\d+) on")
_TAG = re.compile(r"^v(\d+\.\d+\.\d+[A-Za-z0-9.-]*)$")


class SimulationError(RuntimeError):
    """The simulation could not be fetched or did not start."""


def fetch_release(ref: str = DEFAULT_UNIMOC_REF) -> Path:
    """Downloads the ``native_sim`` binary of a UNIMOC release tag (checked against SHA256SUMS)."""
    version = _version(ref)
    directory = cache_root() / "sim" / ref
    executable = directory / "unimoc-native_sim"
    if executable.exists():
        return executable
    asset = f"unimoc-native_sim-{version}.tar.gz"
    archive = _release_asset(ref, asset)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        path = Path(temporary) / asset
        path.write_bytes(archive)
        with tarfile.open(path) as tar:
            member = tar.getmember("unimoc-native_sim")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise SimulationError(f"{asset} holds no unimoc-native_sim")
            target = Path(temporary) / "unimoc-native_sim"
            target.write_bytes(extracted.read())
            target.chmod(0o755)
            target.replace(executable)
    return executable


def fetch_register_descriptions(ref: str = DEFAULT_UNIMOC_REF) -> Path:
    """Downloads the register description file of a UNIMOC release tag (from v0.3.0 on)."""
    version = _version(ref)
    path = cache_root() / "sim" / ref / "registers.json"
    if not path.exists():
        data = _release_asset(ref, f"unimoc-registers-{version}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


def _version(ref: str) -> str:
    match = _TAG.match(ref)
    if not match:
        raise SimulationError(f"not a release tag: {ref!r} (expected v<major>.<minor>.<patch>)")
    return match.group(1)


def _release_asset(ref: str, asset: str) -> bytes:
    data = _get(f"{_RELEASES}/{ref}/{asset}")
    sums = _get(f"{_RELEASES}/{ref}/SHA256SUMS").decode()
    expected = next((line.split()[0] for line in sums.splitlines() if line.endswith(asset)), None)
    if expected != hashlib.sha256(data).hexdigest():
        raise SimulationError(f"{asset} does not match SHA256SUMS of {ref}")
    return data


def _get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "cypilot"}), timeout=120
        ) as response:
            data: bytes = response.read()
            return data
    except OSError as error:
        raise SimulationError(f"download failed: {url}: {error}") from error


class SimInstance:
    """One running simulated drive; use as a context manager."""

    def __init__(
        self, executable: Path, flash: Path, node_id: int | None = None, can_interface: str | None = None
    ) -> None:
        if sys.platform != "linux":
            raise SimulationError("native_sim runs on Linux only (on Windows: run cypilot in WSL2)")
        self.executable = executable
        self.flash = flash
        self.node_id = node_id
        self.can_interface = can_interface
        self.ptys: dict[str, str] = {}
        self.output: collections.deque[str] = collections.deque(maxlen=2000)
        self._process: subprocess.Popen[str] | None = None
        self._started = threading.Event()
        self._reader: threading.Thread | None = None

    def __enter__(self) -> SimInstance:
        self.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.stop()

    @property
    def slcan_interfaces(self) -> list[str]:
        """``slcan:<pty>`` of the CAN terminals (uart_1 to uart_4; uart is the console)."""
        return [f"slcan:{path}" for name, path in sorted(self.ptys.items()) if name != "uart"]

    def start(self, timeout: float = 15.0) -> None:
        """Starts the simulation (again after :meth:`stop`: new pseudo terminals, the same flash)."""
        self.ptys.clear()
        self._started.clear()
        command = [str(self.executable), f"-flash={self.flash}", "-no-color"]
        if self.node_id is not None:
            command.append(f"-node-id={self.node_id}")
        if self.can_interface is not None:
            command.append(f"-can-if={self.can_interface}")
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        self._reader = threading.Thread(target=self._read, name="unimoc-native_sim output", daemon=True)
        self._reader.start()
        if not self._started.wait(timeout):
            self.stop()
            raise SimulationError("the simulation did not report its Cyphal node:\n" + "\n".join(self.output))

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.rstrip()
            self.output.append(line)
            if match := _PTY.match(line):
                self.ptys[match.group(1)] = match.group(2)
            elif match := _NODE.search(line):
                self.node_id = int(match.group(1))
                time.sleep(0.2)  # the slcan terminals open just after this line
                self._started.set()
