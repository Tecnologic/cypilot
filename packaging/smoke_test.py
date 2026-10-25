# SPDX-License-Identifier: Apache-2.0
"""Smoke test of a built executable or an installed wheel: python packaging/smoke_test.py dist/cypilot

Runs ``--version`` and ``dsdl`` (the download on the first start), then
``serve`` with a demo device: the page, the API, the registers of the node, a
recorded run. Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def call(method: str, path: str, body: object = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def wait_for(what: str, check: object, timeout: float = 120.0) -> object:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()  # type: ignore[operator]
            if result:
                return result
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(0.5)


def main() -> None:
    exe = sys.argv[1:]  # the executable, or an interpreter and "-m cypilot"
    print(subprocess.run([*exe, "--version"], check=True, capture_output=True, text=True).stdout.strip())
    print(subprocess.run([*exe, "dsdl"], check=True, capture_output=True, text=True).stdout.strip())
    with tempfile.TemporaryDirectory() as runs:
        server = subprocess.Popen([*exe, "serve", "--port", str(PORT), "--demo", "1", "--runs", runs])
        try:
            status = wait_for("web app", lambda: call("GET", "/api/status"))
            assert status["connected"] and status["demo_devices"] == 1, status
            assert b"cypilot" in urllib.request.urlopen(BASE + "/", timeout=10).read()
            wait_for("demo node", lambda: any(n["node_id"] == 10 for n in call("GET", "/api/nodes")))
            names = [r["name"] for r in call("GET", "/api/nodes/10/registers")]
            assert "demo.frequency" in names, names
            call("POST", "/api/runs/start", {"name": "smoke"})
            time.sleep(2)
            run = call("POST", "/api/runs/stop")
            assert run["state"] == "closed", run
            assert not call("POST", "/api/disconnect")["connected"]
            print("smoke test passed")
        finally:
            server.terminate()
            server.wait(10)


if __name__ == "__main__":
    main()
