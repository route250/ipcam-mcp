"""FastAPI browser endpoints and a Streamable HTTP MCP endpoint."""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from .ptz import ControlError, Controller
from .state import CameraState

HOST = "127.0.0.1:8000"
ORIGIN = f"http://{HOST}"
NO_STORE = {"Cache-Control": "no-store"}
INDEX = Path(__file__).with_name("index.html")
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def mcp_app(state, controller, allowed_hosts=None):
    tools = [
        types.Tool(name="camera_get_info", description="Read current pan/tilt and environment measurements.",
                   inputSchema=EMPTY_SCHEMA, annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(name="camera_snapshot", description="Read a 640×360 JPEG and its frame-time measurements.",
                   inputSchema=EMPTY_SCHEMA, annotations=types.ToolAnnotations(readOnlyHint=True)),
        types.Tool(name="camera_goto", description="Move to calibrated pan/tilt angles in degrees.",
                   inputSchema={
                       "type": "object", "additionalProperties": False,
                       "properties": {
                           "pan_degrees": {"type": "number", "minimum": -177.5, "maximum": 177.5},
                           "tilt_degrees": {"type": "number", "minimum": -30, "maximum": 90},
                       },
                       "anyOf": [{"required": ["pan_degrees"]}, {"required": ["tilt_degrees"]}],
                   }),
    ]

    async def list_tools(_context, _params):
        return types.ListToolsResult(tools=tools)

    async def call_tool(_context, params):
        args = params.arguments or {}
        content = []
        try:
            if params.name == "camera_goto":
                if set(args) - {"pan_degrees", "tilt_degrees"} or any(value is None for value in args.values()):
                    raise ControlError("invalid_argument", "Provide pan_degrees or tilt_degrees as a JSON number")
                data = await controller.goto(args.get("pan_degrees"), args.get("tilt_degrees"))
            elif params.name in {"camera_get_info", "camera_snapshot"}:
                if args:
                    raise ControlError("invalid_argument", "This tool takes no arguments")
                if params.name == "camera_snapshot":
                    frame = state.current_frame()
                    if frame is None:
                        raise ControlError("frame_unavailable", "No current camera image is available")
                    data = frame.status
                    content.append(types.ImageContent(type="image", mimeType="image/jpeg",
                                                      data=base64.b64encode(frame.jpeg).decode("ascii")))
                else:
                    data = state.status()
            else:
                raise ControlError("invalid_argument", "Unknown tool")
        except ControlError as error:
            data = error.response()
            return types.CallToolResult(isError=True, structuredContent=data,
                                        content=[types.TextContent(type="text", text=json.dumps(data))])
        content.append(types.TextContent(type="text", text=json.dumps(data)))
        return types.CallToolResult(content=content, structuredContent=data)

    server = Server("ipcam-server", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool)
    hosts = sorted(allowed_hosts or {HOST})
    return server.streamable_http_app(transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    ))


class LocalRequests:
    def __init__(self, app, allowed_hosts=None):
        self.app = app
        self.allowed_hosts = {host.encode() for host in (allowed_hosts or {HOST})}

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind in {"http", "websocket"}:
            headers = scope["headers"]
            host = [value for key, value in headers if key == b"host"]
            origins = [value for key, value in headers if key == b"origin"]
            accepted_host = len(host) == 1 and host[0] in self.allowed_hosts
            accepted_origin = accepted_host and origins in ([], [b"http://" + host[0]])
            if not accepted_origin or (kind == "websocket" and not origins):
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Host or Origin rejected"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def mjpeg(state, first):
    frame = first
    previous = 0
    while frame is not None:
        if frame.status["frame"]["id"] != previous:
            previous = frame.status["frame"]["id"]
            yield (b"--ipcam\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame.jpeg)).encode() + b"\r\n\r\n" + frame.jpeg + b"\r\n")
        changed = state.frame_changed
        frame = state.current_frame()
        if frame is None:
            break
        if frame.status["frame"]["id"] == previous:
            try:
                async with asyncio.timeout(5):
                    await changed.wait()
            except TimeoutError:
                break
            frame = state.current_frame()


async def handle_ptz(websocket, controller):
    await websocket.accept()
    owner = object()
    queue = asyncio.Queue(maxsize=24)
    controller.subscribers[owner] = queue

    async def send_events():
        while True:
            await websocket.send_json(await queue.get())

    sender = asyncio.create_task(send_events())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw) if len(raw) <= 256 else None
                if not isinstance(payload, dict):
                    raise ValueError
                if set(payload) == {"type", "direction"} and payload["type"] == "drive":
                    if not isinstance(payload["direction"], str):
                        raise ValueError
                    controller.drive(owner, payload["direction"])
                elif payload == {"type": "stop"}:
                    controller.stop_manual(owner)
                else:
                    raise ValueError
                reply = dict(ok=True, ptz_state=controller.state.ptz_state)
            except ValueError:
                reply = dict(ok=False, code="invalid_argument", message="Expected a drive or stop message")
            except ControlError as error:
                reply = dict(ok=False, **error.response())
            controller.notify(queue, reply)
    except (WebSocketDisconnect, KeyError):
        pass
    finally:
        controller.stop_manual(owner)
        controller.subscribers.pop(owner, None)
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)


def create_app(settings, *, allowed_hosts=None):
    state = CameraState(settings)
    controller = Controller(state)
    mcp = mcp_app(state, controller, allowed_hosts)

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.router.lifespan_context(mcp):
            camera_task = asyncio.create_task(state.run(controller))
            try:
                yield
            finally:
                await controller.close()
                camera_task.cancel()
                await asyncio.gather(camera_task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalRequests, allowed_hosts=allowed_hosts)
    app.router.routes.extend(mcp.routes)

    @app.get("/")
    async def index():
        return FileResponse(INDEX, headers=NO_STORE)

    @app.get("/api/status")
    async def status():
        return JSONResponse(state.status(), headers=NO_STORE)

    @app.get("/api/stream.mjpg")
    async def stream():
        frame = state.current_frame()
        if not frame:
            return JSONResponse({"code": "frame_unavailable"}, status_code=503, headers=NO_STORE)
        return StreamingResponse(mjpeg(state, frame), media_type="multipart/x-mixed-replace; boundary=ipcam",
                                 headers=NO_STORE)

    @app.websocket("/api/ptz")
    async def ptz(websocket: WebSocket):
        await handle_ptz(websocket, controller)

    return app
