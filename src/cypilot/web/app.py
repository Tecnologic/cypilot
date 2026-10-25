# SPDX-License-Identifier: Apache-2.0
"""REST and WebSocket API of cypilot, and the static web UI.

The API works without a bus connection as far as it can (status, adapters,
connect); everything that needs the bus answers 503 until connected. The UI
uses this API only, so scripts can use it too; OpenAPI is at ``/docs``.

**Clients:** every request names its client in the ``X-Cypilot-Client``
header, the WebSocket in its ``client`` query parameter; the WebSocket's
``{"type": "renew"}`` messages keep that client's publications and control
lease alive. **Operator:** one client holds the control lease
(``/api/control/take``); enabling, setpoints, register writes, commands that
change a node, publications and the simulation controls need it. Reading,
plotting, captures, **stop all** and **E-stop** do not: stopping is never
locked. **Login:** with a PIN, every ``/api`` request and the WebSocket need
the token of ``/api/login`` (cookie or ``Authorization: Bearer``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from cypilot import __version__
from cypilot.core.control import ControlError, SetpointMode, find_target, recover
from cypilot.core.hub import BusConfig, BusHub, NotConnected, NotOperator
from cypilot.core.registers import Register, RegisterError, RegisterValue, parse_like
from cypilot.core.runs import RunError, replay_json
from cypilot.core.scope import CaptureRequest, ScopeError
from cypilot.core.signals import SignalError, load_type, sources, type_name
from cypilot.core.sim_control import SimControl, SimulationError
from cypilot.core.subjects import jsonable
from cypilot.core.transport import InterfaceError, find_adapters

_logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
CLIENT_HEADER = "X-Cypilot-Client"
TOKEN_COOKIE = "cypilot_token"

COMMANDS = {
    "restart": 65535,
    "factory-reset": 65532,
    "emergency-stop": 65531,
    "store": 65530,
}
"""Standard ``uavcan.node.ExecuteCommand`` codes by name (as ``cypilot-ctl command``)."""
COMMAND_STATUS = {
    0: "success",
    1: "failure",
    2: "not authorized",
    3: "bad command",
    4: "bad parameter",
    5: "bad state",
    6: "internal error",
}


class LoginRequest(BaseModel):
    pin: str


class ConnectRequest(BaseModel):
    interfaces: list[str] = []
    local_node_id: int = Field(127, ge=0, le=127)
    mtu: int | None = None
    bitrate: tuple[int, int] | None = None
    pnp: str | None = None
    demo_devices: int = Field(0, ge=0, le=16)


class TakeRequest(BaseModel):
    force: bool = False


class RegisterWrite(BaseModel):
    value: Any
    """Text in the register's type (as typed in an editor), or a JSON number, list or string."""


class CommandRequest(BaseModel):
    command: int | str
    parameter: str = ""


class DisableRequest(BaseModel):
    ramp: float = Field(0.5, ge=0.0, le=60.0)


class SetpointRequest(BaseModel):
    mode: str
    """``current``, ``torque``, ``speed`` or ``position``."""
    values: list[float]
    ramp: float = Field(0.0, ge=0.0, le=60.0)


class RecoverRequest(BaseModel):
    restart: bool = False
    """Allow the restart that a latched emergency stop needs."""


class SimRequest(BaseModel):
    action: str
    """``pause``, ``run``, ``advance`` or ``reset``."""
    cycles: int = Field(16000, ge=1, le=10_000_000)


class SubjectRequest(BaseModel):
    subject_id: int = Field(ge=0, le=8191)
    type: str


class PublishRequest(BaseModel):
    subject_id: int = Field(ge=0, le=8191)
    type: str
    value: dict[str, Any] = {}
    rate_hz: float = Field(10.0, ge=0.1, le=1000.0)
    lease_s: float = Field(1.0, ge=0.1, le=60.0)
    priority: str = "nominal"


class MultiCaptureBody(BaseModel):
    nodes: dict[int, list[int]] = Field(min_length=1, max_length=16)
    """Variable IDs per node."""
    trigger_node: int
    sample_period_us: float = Field(gt=0)
    num_samples: int = Field(ge=1, le=65535)
    pretrigger_samples: int = Field(0, ge=0, le=65534)
    trigger_mode: str = "immediate"
    trigger_source: int = Field(0, ge=0, le=255)
    trigger_level: float = 0.0
    hysteresis_band: float = 0.0
    timeout_s: float = Field(30.0, gt=0, le=3600)


class TriggerSetup(BaseModel):
    nodes: list[int] = Field(min_length=1, max_length=16)
    subject: int = Field(220, ge=0, le=8191)


class RunStart(BaseModel):
    name: str = Field("", max_length=200)
    notes: str = Field("", max_length=20000)


class RunUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=20000)


class RunMark(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class CaptureBody(BaseModel):
    channels: list[int] = Field(min_length=1, max_length=16)
    sample_period_us: float = Field(gt=0)
    num_samples: int = Field(ge=1, le=65535)
    pretrigger_samples: int = Field(0, ge=0, le=65534)
    trigger_mode: str = "immediate"
    trigger_source: int = Field(0, ge=0, le=255)
    trigger_level: float = 0.0
    hysteresis_band: float = 0.0
    timeout_s: float = Field(30.0, gt=0, le=3600)


def register_json(register: Register) -> dict[str, Any]:
    content = register.value.content
    return {
        "name": register.name,
        "kind": register.value.kind,
        "value": content.hex() if isinstance(content, bytes) else content,
        "text": register.value.format(),
        "mutable": register.mutable,
        "persistent": register.persistent,
    }


def value_like(data: Any, current: RegisterValue) -> RegisterValue:
    """A JSON value or editor text in the kind and length of the register's present value."""
    if current.kind in ("string", "unstructured") or isinstance(data, str):
        return parse_like(str(data), current)
    items = data if isinstance(data, list) else [data]
    return parse_like(", ".join(str(item) for item in items), current)


class Auth:
    """Tokens of logged-in clients; without a PIN every request is allowed."""

    def __init__(self, pin: str | None) -> None:
        self.pin = pin
        self._tokens: set[str] = set()

    @property
    def required(self) -> bool:
        return self.pin is not None

    def login(self, pin: str) -> str:
        if self.pin is None:
            return ""
        if not secrets.compare_digest(pin, self.pin):
            raise PermissionError("wrong PIN")
        token = secrets.token_urlsafe(24)
        self._tokens.add(token)
        return token

    def valid(self, token: str | None) -> bool:
        return not self.required or (token is not None and token in self._tokens)


def _token(headers: Any, cookies: Any, query: Any) -> str | None:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return str(authorization[7:].strip())
    return cookies.get(TOKEN_COOKIE) or query.get("token")


def create_app(hub: BusHub, *, pin: str | None = None) -> FastAPI:
    app = FastAPI(title="cypilot", version=__version__)
    app.state.hub = hub
    auth = Auth(pin)

    def authorized(request: Request) -> None:
        if not auth.valid(_token(request.headers, request.cookies, request.query_params)):
            raise HTTPException(401, "log in first (POST /api/login)")

    def client_of(request: Request) -> str:
        return request.headers.get(CLIENT_HEADER, "")

    @app.exception_handler(NotConnected)
    async def not_connected(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(NotOperator)
    @app.exception_handler(PermissionError)
    async def forbidden(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=403)

    @app.exception_handler(RunError)
    async def run_conflict(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ControlError)
    @app.exception_handler(RegisterError)
    @app.exception_handler(ScopeError)
    @app.exception_handler(SignalError)
    @app.exception_handler(SimulationError)
    @app.exception_handler(InterfaceError)
    @app.exception_handler(ValueError)
    @app.exception_handler(LookupError)
    async def bad_request(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(TimeoutError)
    async def timeout(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error) or "the node did not answer"}, status_code=504)

    # --- Login (open) ------------------------------------------------------------

    @app.get("/api/auth")
    async def auth_state(request: Request) -> dict[str, Any]:
        token = _token(request.headers, request.cookies, request.query_params)
        return {"required": auth.required, "ok": auth.valid(token)}

    @app.post("/api/login")
    async def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        token = auth.login(body.pin)
        if token:
            response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict")
        return {"token": token}

    api = APIRouter(prefix="/api", dependencies=[Depends(authorized)])

    # --- Bus -----------------------------------------------------------------------

    @api.get("/status")
    async def status() -> dict[str, Any]:
        return hub.status()

    @api.get("/adapters")
    async def adapters() -> list[dict[str, Any]]:
        return [vars(adapter) for adapter in await asyncio.to_thread(find_adapters)]

    @api.post("/connect")
    async def connect(body: ConnectRequest) -> dict[str, Any]:
        try:
            return await hub.connect(BusConfig(**body.model_dump()))
        except (InterfaceError, ValueError):
            raise
        except Exception as error:  # the adapter did not open: serial, USB, SocketCAN errors
            _logger.warning("connect failed: %s", error)
            raise HTTPException(
                400, f"cannot open {', '.join(body.interfaces) or 'the bus'}: {error}"
            ) from error

    @api.post("/disconnect")
    async def disconnect() -> dict[str, Any]:
        return await hub.disconnect()

    # --- Nodes and registers -------------------------------------------------------

    @api.get("/nodes")
    async def nodes() -> list[dict[str, Any]]:
        return hub.nodes()

    def known(node_id: int) -> None:
        if all(node["node_id"] != node_id for node in hub.nodes()):
            raise HTTPException(404, f"node {node_id} is not on the bus")

    @api.get("/nodes/{node_id}/registers")
    async def registers(node_id: int) -> list[dict[str, Any]]:
        known(node_id)
        client = hub.require_session().registers(node_id)
        try:
            return [register_json(register) for register in await client.scan()]
        finally:
            client.close()

    @api.get("/nodes/{node_id}/registers/{name}")
    async def read_register(node_id: int, name: str) -> dict[str, Any]:
        known(node_id)
        client = hub.require_session().registers(node_id)
        try:
            register = await client.read(name)
        finally:
            client.close()
        if register.value.kind == "empty":
            raise HTTPException(404, f"node {node_id} has no register {name}")
        return register_json(register)

    @api.put("/nodes/{node_id}/registers/{name}")
    async def write_register(
        node_id: int, name: str, body: RegisterWrite, request: Request
    ) -> dict[str, Any]:
        hub.require_operator(client_of(request))
        known(node_id)
        client = hub.require_session().registers(node_id)
        try:
            current = await client.read(name)
            if current.value.kind == "empty":
                raise HTTPException(404, f"node {node_id} has no register {name}")
            if not current.mutable:
                raise RegisterError(f"{name} is read-only")
            result = await client.write(name, value_like(body.value, current.value))
        finally:
            client.close()
        hub.events.emit("register", node_id=node_id, name=name, accepted=result.accepted)
        return {"accepted": result.accepted, "register": register_json(result.register)}

    @api.get("/nodes/{node_id}/ports")
    async def node_ports(node_id: int) -> list[dict[str, Any]]:
        from cypilot.core.capabilities import ports

        known(node_id)
        client = hub.require_session().registers(node_id)
        try:
            names = [name for name in await client.names() if name.startswith("uavcan.")]
            found = ports(await client.scan(names), node_id)
        finally:
            client.close()
        return [vars(port) for port in found]

    @api.get("/nodes/{node_id}/sources")
    async def node_sources(node_id: int) -> list[dict[str, Any]]:
        """The published subjects with a type, for plots and recordings."""
        known(node_id)
        client = hub.require_session().registers(node_id)
        try:
            names = [name for name in await client.names() if name.startswith("uavcan.pub.")]
            found = sources(await client.scan(names), node_id)
        finally:
            client.close()
        return [vars(source) | {"key": source.key} for source in found]

    @api.get("/commands")
    async def commands() -> dict[str, int]:
        return COMMANDS

    @api.post("/nodes/{node_id}/command")
    async def command(node_id: int, body: CommandRequest, request: Request) -> dict[str, Any]:
        code = COMMANDS.get(body.command) if isinstance(body.command, str) else body.command
        if code is None:
            raise HTTPException(400, f"unknown command {body.command!r}")
        if code != COMMANDS["emergency-stop"]:  # stopping is never locked
            hub.require_operator(client_of(request))
        known(node_id)
        result = await hub.require_session().execute_command(node_id, code, body.parameter)
        status_text = COMMAND_STATUS.get(result, str(result))
        hub.events.emit("command", node_id=node_id, command=code, status=status_text)
        return {"command": code, "status": status_text, "ok": result == 0}

    descriptions: dict[str, Any] = {}

    @api.get("/descriptions")
    async def register_descriptions() -> dict[str, Any]:
        """The UNIMOC register description file: ``$CYPILOT_REGISTERS`` or that of the pinned release."""
        if not descriptions:
            descriptions.update(await asyncio.to_thread(_load_descriptions))
        return descriptions

    @api.get("/log")
    async def log() -> list[dict[str, Any]]:
        return list(hub.log)

    # --- Control -------------------------------------------------------------------

    @api.get("/control")
    async def control() -> dict[str, Any]:
        return hub.control_state()

    @api.post("/control/take")
    async def take_control(request: Request, body: TakeRequest | None = None) -> dict[str, Any]:
        client = client_of(request)
        if not client:
            raise HTTPException(400, f"name the client in the {CLIENT_HEADER} header")
        return hub.take_control(client, force=bool(body and body.force))

    @api.post("/control/release")
    async def release_control(request: Request) -> dict[str, Any]:
        return await hub.release_control(client_of(request))

    @api.get("/nodes/{node_id}/target")
    async def drive_target(node_id: int) -> dict[str, Any]:
        """Where a drive listens: readiness and setpoint subjects, with the components per setpoint."""
        known(node_id)
        target = await find_target(hub.require_session(), node_id)
        return {
            "node_id": node_id,
            "readiness": target.readiness,
            "setpoints": {
                mode.name.lower(): {"subject": subject, "components": mode.components}
                for mode, subject in target.setpoints.items()
            },
        }

    @api.post("/nodes/{node_id}/enable")
    async def enable(node_id: int, request: Request) -> dict[str, Any]:
        controller = hub.require_operator(client_of(request))
        target = await find_target(hub.require_session(), node_id)
        await controller.engage(target)
        await controller.wait_engaged(node_id)
        return hub.control_state()

    @api.post("/nodes/{node_id}/disable")
    async def disable(node_id: int, request: Request, body: DisableRequest | None = None) -> dict[str, Any]:
        controller = hub.require_operator(client_of(request))
        await controller.disengage(node_id, ramp=body.ramp if body else 0.5)
        return hub.control_state()

    @api.post("/nodes/{node_id}/setpoint")
    async def setpoint(node_id: int, body: SetpointRequest, request: Request) -> dict[str, Any]:
        controller = hub.require_operator(client_of(request))
        try:
            mode = SetpointMode[body.mode.upper()]
        except KeyError:
            raise HTTPException(400, f"unknown setpoint mode {body.mode!r}") from None
        target = controller.known.get(node_id) or await find_target(hub.require_session(), node_id)
        controller.set_setpoint(target, mode, body.values, ramp=body.ramp)
        return {"node_id": node_id, "mode": mode.name.lower(), "values": body.values}

    @api.post("/nodes/{node_id}/recover")
    async def recover_node(
        node_id: int, request: Request, body: RecoverRequest | None = None
    ) -> dict[str, Any]:
        controller = hub.require_operator(client_of(request))
        result = await recover(
            hub.require_session(), controller, node_id, allow_restart=bool(body and body.restart)
        )
        return {"steps": result.steps, "ready": result.ready}

    @api.post("/stop")
    async def stop_all() -> dict[str, Any]:
        return {"stopped": await hub.stop_all()}

    @api.post("/estop")
    async def estop() -> dict[str, Any]:
        result = await hub.emergency_stop()
        return {"nodes": {str(node_id): status for node_id, status in result.items()}}

    # --- Simulation ----------------------------------------------------------------

    @api.get("/nodes/{node_id}/sim")
    async def sim_status(node_id: int) -> dict[str, Any]:
        control = SimControl(hub.require_session().node, node_id)
        try:
            status = await control.status()
        finally:
            control.close()
        return vars(status) | {"virtual_time": status.virtual_time}

    @api.post("/nodes/{node_id}/sim")
    async def sim_action(node_id: int, body: SimRequest, request: Request) -> dict[str, Any]:
        hub.require_operator(client_of(request))
        control = SimControl(hub.require_session().node, node_id)
        try:
            if body.action in ("pause", "run"):
                await control.set_paused(body.action == "pause")
            elif body.action == "advance":
                await control.advance(body.cycles)
            elif body.action == "reset":
                await control.reset()
            else:
                raise HTTPException(400, f"unknown simulation action {body.action!r}")
            status = await control.status()
        finally:
            control.close()
        return vars(status) | {"virtual_time": status.virtual_time}

    # --- Types, subjects, publications ------------------------------------------------

    @api.get("/types/{name}")
    async def type_template(name: str) -> dict[str, Any]:
        import pycyphal.dsdl

        compiled = load_type(name)
        return {"type": type_name(compiled), "default": jsonable(pycyphal.dsdl.to_builtin(compiled()))}

    def monitor() -> Any:
        hub.require_session()
        assert hub.subjects is not None
        return hub.subjects

    def publishers() -> Any:
        hub.require_session()
        assert hub.publishers is not None
        return hub.publishers

    @api.get("/subjects")
    async def subjects() -> list[dict[str, Any]]:
        return list(monitor().list())

    @api.post("/subjects")
    async def subscribe(body: SubjectRequest) -> dict[str, Any]:
        return dict(monitor().subscribe(body.subject_id, body.type))

    @api.delete("/subjects/{sub_id}")
    async def unsubscribe(sub_id: int) -> dict[str, Any]:
        monitor().unsubscribe(sub_id)
        return {"ok": True}

    @api.get("/publishers")
    async def publications() -> list[dict[str, Any]]:
        return list(publishers().list())

    @api.post("/publishers")
    async def publish(body: PublishRequest, request: Request) -> dict[str, Any]:
        client = client_of(request)
        hub.require_operator(client)
        return dict(
            publishers().start(
                body.subject_id,
                body.type,
                body.value,
                body.rate_hz,
                body.lease_s,
                body.priority,
                owner=client,
            )
        )

    @api.put("/publishers/{pub_id}")
    async def update_publication(
        pub_id: int, request: Request, value: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        hub.require_operator(client_of(request))
        try:
            return dict(publishers().update(pub_id, value))
        except KeyError:
            raise HTTPException(404, f"no publication {pub_id}") from None

    @api.delete("/publishers/{pub_id}")
    async def stop_publication(pub_id: int) -> dict[str, Any]:
        publishers().stop(pub_id)
        return {"ok": True}

    # --- Scope -----------------------------------------------------------------------

    def scope() -> Any:
        hub.require_session()
        assert hub.scope is not None
        return hub.scope

    @api.get("/nodes/{node_id}/scope")
    async def scope_info(node_id: int, refresh: bool = False) -> dict[str, Any]:
        known(node_id)
        await scope().discover(node_id, refresh=refresh)
        return dict(await scope().info(node_id))

    @api.get("/nodes/{node_id}/scope/status")
    async def scope_status(node_id: int) -> dict[str, Any]:
        return dict(await scope().status(node_id))

    @api.post("/nodes/{node_id}/scope/capture")
    async def scope_capture(node_id: int, body: CaptureBody) -> dict[str, Any]:
        known(node_id)
        capture = await scope().capture(node_id, CaptureRequest(**body.model_dump()))
        if hub.recording is not None:  # captures during a run are stored in it
            names = {v["id"]: v["name"] for v in (await scope().info(node_id))["variables"]}
            hub.recording.run.add_capture(node_id, capture, [names.get(c, f"ch{c}") for c in body.channels])
        return vars(capture)

    @api.get("/scope/trigger")
    async def scope_trigger(nodes: str) -> dict[str, Any]:
        """Whether the nodes (``nodes=10,11``) share the trigger subject."""
        ids = [int(n) for n in nodes.split(",") if n.strip()]
        return dict(await scope().trigger_ports(ids))

    @api.post("/scope/trigger")
    async def setup_scope_trigger(body: TriggerSetup, request: Request) -> dict[str, Any]:
        """Writes the shared trigger subject, stores it and restarts the nodes (ports apply at start)."""
        controller = hub.require_operator(client_of(request))
        engaged = sorted(set(body.nodes) & controller.engaged)
        if engaged:
            raise HTTPException(409, f"disable node {engaged[0]} first: it restarts")
        for node_id in body.nodes:
            known(node_id)
        await scope().set_trigger_ports(body.nodes, body.subject)
        session = hub.require_session()
        for node_id in body.nodes:
            stored = await session.execute_command(node_id, COMMANDS["store"])
            if stored != 0:
                raise HTTPException(
                    409, f"node {node_id} did not store: {COMMAND_STATUS.get(stored, stored)}"
                )
            hub.events.emit("command", node_id=node_id, command=COMMANDS["store"], status="success")
        for node_id in body.nodes:
            await session.execute_command(node_id, COMMANDS["restart"])
            hub.events.emit("command", node_id=node_id, command=COMMANDS["restart"], status="success")
            scope().forget(node_id)
        return {"subject": body.subject, "nodes": body.nodes}

    @api.post("/scope/capture")
    async def scope_multi_capture(body: MultiCaptureBody) -> dict[str, Any]:
        """One capture on several nodes on the trigger of one of them."""
        for node_id in body.nodes:
            known(node_id)
        fields = body.model_dump(exclude={"nodes", "trigger_node"})
        request = CaptureRequest(channels=[], **fields)
        captures = await scope().multi_capture(body.nodes, body.trigger_node, request)
        if hub.recording is not None:
            for node_id, capture in captures.items():
                names = {v["id"]: v["name"] for v in (await scope().info(node_id))["variables"]}
                labels = [names.get(c, f"ch{c}") for c in body.nodes[node_id]]
                hub.recording.run.add_capture(node_id, capture, labels)
        return {"trigger_node": body.trigger_node, "captures": {str(n): vars(c) for n, c in captures.items()}}

    @api.post("/nodes/{node_id}/scope/read")
    async def scope_read(node_id: int) -> dict[str, Any]:
        return vars(await scope().read(node_id))

    @api.post("/nodes/{node_id}/scope/disarm")
    async def scope_disarm(node_id: int) -> dict[str, Any]:
        await scope().disarm(node_id)
        return {"ok": True}

    # --- Runs: started and stopped only by the user; any client may record ---------

    @api.get("/runs")
    async def runs() -> dict[str, Any]:
        state = hub.run_state()
        state["runs"] = hub.runs.summaries() if hub.runs is not None else []
        return state

    @api.post("/runs/start")
    async def start_run(body: RunStart | None = None) -> dict[str, Any]:
        return await hub.start_run(body.name if body else "", body.notes if body else "")

    @api.post("/runs/stop")
    async def stop_run() -> dict[str, Any]:
        summary = await hub.stop_run()
        if summary is None:
            raise HTTPException(409, "no run records")
        return summary

    @api.post("/runs/mark")
    async def mark_run(body: RunMark) -> dict[str, Any]:
        hub.mark_run(body.text)
        return {"ok": True}

    @api.get("/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        store = hub.require_runs()
        detail = store.get(run_id)
        detail["files"] = (
            await asyncio.to_thread(store.files, run_id) if detail.get("state") == "closed" else []
        )
        return detail

    @api.patch("/runs/{run_id}")
    async def update_run(run_id: str, body: RunUpdate) -> dict[str, Any]:
        return hub.require_runs().update(run_id, name=body.name, notes=body.notes)

    @api.delete("/runs/{run_id}")
    async def delete_run(run_id: str) -> dict[str, Any]:
        await asyncio.to_thread(hub.require_runs().delete, run_id)
        hub.events.emit("run", action="deleted", run={"id": run_id})
        return {"ok": True}

    @api.get("/runs/{run_id}/download")
    async def download_run(run_id: str) -> StreamingResponse:
        store = hub.require_runs()
        data = await asyncio.to_thread(lambda: b"".join(store.archive(run_id)))
        headers = {"Content-Disposition": f'attachment; filename="cypilot-run-{run_id}.zip"'}
        return StreamingResponse(iter([data]), media_type="application/zip", headers=headers)

    @api.get("/runs/{run_id}/files/{name}")
    async def run_file(run_id: str, name: str) -> Response:
        data = await asyncio.to_thread(hub.require_runs().read_file, run_id, name)
        media = (
            "application/json"
            if name.endswith(".json")
            else "text/csv"
            if name.endswith(".csv")
            else "text/plain"
        )
        headers = {"Content-Disposition": f'attachment; filename="{run_id}-{name}"'}
        return Response(data, media_type=media, headers=headers)

    @api.get("/runs/{run_id}/replay")
    async def replay_run(run_id: str, max_points: int = 20000) -> dict[str, Any]:
        store = hub.require_runs()
        recorder = await asyncio.to_thread(store.recorder, run_id)
        return replay_json(recorder, max(100, min(max_points, 200000)))

    app.include_router(api)

    # --- Live events -------------------------------------------------------------------

    @app.websocket("/ws")
    async def events(ws: WebSocket) -> None:
        """Every event as JSON. The client sends ``{"type": "renew"}`` every 100 ms to stay alive."""
        if not auth.valid(_token(ws.headers, ws.cookies, ws.query_params)):
            await ws.close(code=4401)
            return
        await ws.accept()
        client = ws.query_params.get("client") or uuid.uuid4().hex
        await ws.send_json({"kind": "hello", "client": client, "status": hub.status()})

        async def receive() -> None:
            while True:
                message = json.loads(await ws.receive_text())
                if message.get("type") == "renew":
                    hub.renew(client)

        async with hub.events.listen() as queue:
            receiver = asyncio.create_task(receive())
            try:
                while not receiver.done():
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        await ws.send_text(json.dumps(getter.result(), default=_json_default))
                    else:
                        getter.cancel()
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                receiver.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, WebSocketDisconnect, RuntimeError, ValueError
                ):
                    await receiver
                await hub.client_gone(client)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _load_descriptions() -> dict[str, Any]:
    import os

    from cypilot.core.descriptions import DescriptionError, load_descriptions
    from cypilot.simulation import SimulationError, fetch_register_descriptions

    try:
        configured = os.environ.get("CYPILOT_REGISTERS")
        loaded = load_descriptions(Path(configured) if configured else fetch_register_descriptions())
    except (DescriptionError, SimulationError) as error:
        _logger.warning("no register descriptions: %s", error)
        return {"available": False, "error": str(error), "firmware": {}, "registers": {}}
    registers = {name: vars(d) | {"default": list(d.default)} for name, d in loaded.registers.items()}
    return {"available": True, "firmware": loaded.firmware, "registers": registers}


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    return item() if callable(item) else str(value)
