# SPDX-License-Identifier: Apache-2.0
"""The web server for the browser tests."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


class WebServer:
    """``cypilot.web`` on a free port, served by uvicorn on its own thread (as ``cypilot-ctl serve``)."""

    def __init__(self, *, pin: str | None = None, lease_time: float = 0.5, runs: Path | None = None) -> None:
        import uvicorn

        from cypilot.core.hub import BusHub
        from cypilot.core.runs import RunStore
        from cypilot.web.app import create_app

        self.hub = BusHub(lease_time=lease_time, runs=RunStore(runs) if runs is not None else None)
        app = create_app(self.hub, pin=pin)
        hub = self.hub

        @contextlib.asynccontextmanager
        async def lifespan(_: Any) -> AsyncIterator[None]:
            try:
                yield
            finally:
                await hub.close()

        app.router.lifespan_context = lifespan
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> WebServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the web server did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *_: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def post(self, path: str, body: Any = None) -> Any:
        import httpx

        response = httpx.post(self.url + path, json=body, timeout=30)
        response.raise_for_status()
        return response.json()
