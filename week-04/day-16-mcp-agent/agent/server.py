"""FastAPI backend: MCP status, MCP tools, streamed chat and the static UI.

Everything runs on one origin: the API under ``/api`` and the frontend mounted
at ``/``. The browser never receives a credential or an upstream header; the
model API key stays in the backend process.

A failing dependency is reported as a controlled error: an unreachable MCP
server is answered with ``connected: false`` on the status endpoints and with an
``error`` event on the chat stream, never with a fabricated successful answer.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.chats import ChatError, ChatService, MAX_CHATS
from agent.mcp_adapter import McpClient, SdkMcpClient
from agent.mcp_hub import McpHub, ServerProbe
from agent.monitor import NotifierMonitor
from agent.notifier_client import NotifierClient, NotifierUnavailable
from agent.orchestrator import ErrorEvent, Orchestrator
from agent.provider import OpenAICompatibleProvider
from agent.sessions import SessionRegistry
from agent.settings import SERVICE_NAME, SERVICE_VERSION, Settings, load_settings
from agent.trace import TraceWriter
from storage.db import Database, StorageError

KEEPALIVE_SECONDS = 15.0
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger("agent.server")


class NoCacheStaticFiles(StaticFiles):
    """Static assets must always be revalidated.

    Without an explicit header, the browser may keep serving a cached
    ``styles.css`` after the file changed, which is exactly the stale-CSS bug
    that made chat links unreadable.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


class HealthResponse(BaseModel):
    """Liveness of the backend process."""

    status: str
    service: str
    version: str


class ServerInfo(BaseModel):
    """Identity of the connected MCP server."""

    name: str | None = None
    version: str | None = None


class ErrorInfo(BaseModel):
    """Controlled failure of a dependency."""

    category: str
    message: str


class McpStatusResponse(BaseModel):
    """Current MCP connectivity and tool count."""

    connected: bool
    protocol_version: str | None = None
    server: ServerInfo | None = None
    tools_count: int = 0
    error: ErrorInfo | None = None
    checked_at: float


class ToolInfo(BaseModel):
    """One MCP tool as exposed to the frontend."""

    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict = Field(default_factory=dict)


class McpToolsResponse(BaseModel):
    """The full MCP tool list; ``connected`` is false when MCP is down."""

    connected: bool
    tools: list[ToolInfo] = Field(default_factory=list)
    count: int = 0
    protocol_version: str | None = None
    server: ServerInfo | None = None
    error: ErrorInfo | None = None


class ServerEntry(BaseModel):
    """One MCP server of the hub as exposed to the frontend."""

    label: str
    endpoint: str | None = None
    connected: bool = False
    protocol_version: str | None = None
    server: ServerInfo | None = None
    tools_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class McpServersResponse(BaseModel):
    """Both MCP servers read through a single ``probe_servers()`` call."""

    servers: list[ServerEntry] = Field(default_factory=list)
    checked_at: float


class ChatWatchesResponse(BaseModel):
    """The notification watches and deliveries of one chat, read from server B.

    ``available`` is false when B is unreachable or not configured; that is a
    successful HTTP response, never a 5xx.
    """

    chat_id: str
    available: bool
    watches: list[dict] = Field(default_factory=list)
    deliveries: list[dict] = Field(default_factory=list)
    error: ErrorInfo | None = None


class ChatRequest(BaseModel):
    """One chat turn of one saved chat."""

    chat_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)


class ChatInfo(BaseModel):
    """One chat as shown in the chat list."""

    id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int = 0
    active_tasks_count: int = 0


class ChatListResponse(BaseModel):
    """The chat list plus the configured limit."""

    chats: list[ChatInfo] = Field(default_factory=list)
    count: int = 0
    limit: int = MAX_CHATS
    limit_reached: bool = False


class ChatCreateRequest(BaseModel):
    """Payload of ``POST /api/chats``; an empty title gets the default."""

    title: str = ""


class ChatRenameRequest(BaseModel):
    """Payload of ``PATCH /api/chats/{chat_id}``."""

    title: str


class ChatDeleteResponse(BaseModel):
    """Result of deleting a chat."""

    deleted: bool
    stopped_tasks: int = 0


class MessageInfo(BaseModel):
    """One stored chat message."""

    role: str
    content: str
    created_at: float


class ChatMessagesResponse(BaseModel):
    """The full stored history of one chat."""

    chat_id: str
    messages: list[MessageInfo] = Field(default_factory=list)
    count: int = 0


class ChatClearResponse(BaseModel):
    """Result of clearing one chat."""

    cleared: bool
    messages_deleted: int = 0


class TaskRunInfo(BaseModel):
    """The latest stored run of a scheduled task."""

    status: str
    ran_at: float
    result_count: int = 0
    results: list[dict] = Field(default_factory=list)
    error: str | None = None


class TaskInfo(BaseModel):
    """One scheduled task as shown in the task panel."""

    task_id: str
    query: str
    interval_seconds: int
    status: str
    created_at: float
    next_run_at: float
    last_run_at: float | None = None
    last_status: str | None = None
    last_error: str | None = None
    run_count: int = 0
    last_run: TaskRunInfo | None = None


class ChatTasksResponse(BaseModel):
    """The scheduled tasks of one chat."""

    chat_id: str
    tasks: list[TaskInfo] = Field(default_factory=list)
    count: int = 0


class ReportInfo(BaseModel):
    """One saved report as shown in the Saved reports panel."""

    report_id: str
    topic: str
    created_at: float
    source_count: int = 0


class ChatReportsResponse(BaseModel):
    """The saved reports of one chat, newest first."""

    chat_id: str
    reports: list[ReportInfo] = Field(default_factory=list)
    count: int = 0


class ReportDetail(BaseModel):
    """One saved report with its plain-text summary and sources."""

    report_id: str
    chat_id: str
    topic: str
    summary: str
    created_at: float
    source_count: int = 0
    sources: list[dict] = Field(default_factory=list)


def port_is_available(host: str, port: int) -> bool:
    """Whether a loopback port can be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((str(host), int(port)))
        except OSError:
            return False
    return True


def _server_info(status) -> ServerInfo | None:
    if status.server_name is None and status.server_version is None:
        return None
    return ServerInfo(name=status.server_name, version=status.server_version)


def _error_info(error) -> ErrorInfo | None:
    if error is None:
        return None
    return ErrorInfo(category=error.category, message=error.message)


def _chat_http_error(exc: ChatError) -> HTTPException:
    """Map a controlled chat failure to the documented error format."""
    detail = {"category": exc.category, "message": exc.message}
    detail.update(exc.extra)
    return HTTPException(status_code=exc.status_code, detail=detail)


def _storage_http_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "category": "chat_storage_unavailable",
            "message": "Chat storage is unavailable",
        },
    )


def _guard_chat(operation):
    """Run a chat service call, mapping controlled failures to HTTP errors."""
    try:
        return operation()
    except ChatError as exc:
        raise _chat_http_error(exc) from exc
    except StorageError as exc:
        raise _storage_http_error() from exc


def build_provider(settings: Settings) -> OpenAICompatibleProvider:
    """Create the provider from settings (the key is never rendered)."""
    return OpenAICompatibleProvider(
        base_url=settings.model_base_url,
        model=settings.model_name,
        api_key=settings.model_api_key,
        name="openai-compatible",
        default_timeout_s=settings.model_timeout_seconds,
        configured=settings.model_configured,
    )


def build_mcp_client(settings: Settings) -> McpHub:
    """Build the day-20 MCP hub from server A and server B.

    An empty ``MCP_NOTIFIER_URL`` keeps the B slot but leaves it without a
    client, so the hub reports it as ``not_configured`` without probing it.
    """
    a_client = SdkMcpClient(
        settings.mcp_server_url,
        connect_timeout_s=settings.mcp_connect_timeout_seconds,
        call_timeout_s=settings.mcp_call_timeout_seconds,
    )
    notifier_url = str(settings.mcp_notifier_url or "").strip()
    if notifier_url:
        b_client: McpClient | None = SdkMcpClient(
            notifier_url,
            connect_timeout_s=settings.mcp_notifier_connect_timeout_seconds,
            call_timeout_s=settings.mcp_notifier_call_timeout_seconds,
        )
    else:
        b_client = None
    servers = [
        ("A", a_client, _endpoint_label(settings.mcp_server_url)),
        ("B", b_client, _endpoint_label(notifier_url)),
    ]
    return McpHub(servers, primary="A")


def _endpoint_label(url: str) -> str:
    """Return the loopback ``host:port`` of an endpoint, never a full URL."""
    try:
        split = urlsplit(str(url or ""))
    except ValueError:
        return ""
    host = split.hostname or ""
    if not host:
        return ""
    return f"{host}:{split.port}" if split.port else host


def _endpoint_for(client, label: str, settings: Settings) -> str | None:
    get_endpoint = getattr(client, "endpoint_for", None)
    if callable(get_endpoint):
        value = get_endpoint(label)
        if value:
            return value
    if label == "A":
        return _endpoint_label(settings.mcp_server_url) or None
    if label == "B":
        return _endpoint_label(settings.mcp_notifier_url) or None
    return None


def build_notifier_client(mcp_client, settings: Settings) -> NotifierClient:
    """Build the read-only host→B client from the hub's B slot."""
    client = None
    client_for = getattr(mcp_client, "client_for", None)
    if callable(client_for):
        client = client_for("B")
    return NotifierClient(
        client, timeout_s=settings.mcp_notifier_call_timeout_seconds
    )


def create_app(
    settings: Settings | None = None,
    *,
    provider: Any = None,
    mcp_client: McpClient | None = None,
    sessions: SessionRegistry | None = None,
    trace: TraceWriter | None = None,
    chat_service: ChatService | None = None,
    notifier: NotifierClient | None = None,
    enable_monitor: bool = False,
) -> FastAPI:
    """Build the FastAPI application.

    ``provider``, ``mcp_client``, ``sessions``, ``trace``, ``chat_service`` and
    ``notifier`` can be injected so the API can be tested without a model, an MCP
    server, a trace file or the shared database file. ``create_app`` itself has no
    network or background side effect: the monitor starts only when
    ``enable_monitor`` is true (and ``NOTIFIER_MONITOR_ENABLED`` is not off) and
    the application lifespan actually runs.
    """
    resolved = settings or load_settings()
    log_level = getattr(logging, resolved.log_level, logging.INFO)
    logging.basicConfig(level=log_level)

    trace = trace or TraceWriter(resolved.trace_path)

    @asynccontextmanager
    async def _lifespan(_application: FastAPI):
        monitor = None
        if enable_monitor and resolved.notifier_monitor_enabled:
            monitor = NotifierMonitor(
                chats=app.state.chats,
                notifier=app.state.notifier,
                tick_seconds=resolved.notifier_monitor_tick_seconds,
                orchestrator_factory=lambda: Orchestrator(
                    provider=app.state.provider,
                    mcp_client=app.state.mcp_client,
                    trace=app.state.trace,
                    max_tool_rounds=resolved.max_tool_rounds,
                ),
                session_registry=app.state.sessions,
            )
            await monitor.start()
        try:
            yield
        finally:
            if monitor is not None:
                await monitor.stop()
            trace.close()

    app = FastAPI(
        title="Day 16 MCP Agent",
        version=SERVICE_VERSION,
        description=(
            "Local agent with a real MCP server (Streamable HTTP), a discovery "
            "CLI and a streamed browser chat."
        ),
        lifespan=_lifespan,
    )
    app.state.settings = resolved
    app.state.provider = provider or build_provider(resolved)
    app.state.mcp_client = mcp_client or build_mcp_client(resolved)
    app.state.sessions = sessions or SessionRegistry()
    app.state.chats = chat_service or ChatService(Database(resolved.db_path))
    app.state.trace = trace
    app.state.notifier = notifier or build_notifier_client(
        app.state.mcp_client, resolved
    )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok", service=SERVICE_NAME, version=SERVICE_VERSION
        )

    @app.get("/api/mcp/status", response_model=McpStatusResponse)
    async def mcp_status() -> McpStatusResponse:
        status = await app.state.mcp_client.status()
        return McpStatusResponse(
            connected=bool(status.connected),
            protocol_version=status.protocol_version,
            server=_server_info(status),
            tools_count=int(status.tools_count),
            error=_error_info(status.error),
            checked_at=time.time(),
        )

    @app.get("/api/mcp/tools", response_model=McpToolsResponse)
    async def mcp_tools() -> McpToolsResponse:
        client = app.state.mcp_client
        # One probe opens exactly one MCP session (handshake + full tools/list).
        status, tools = await client.probe()
        if not status.connected:
            return McpToolsResponse(
                connected=False,
                count=0,
                protocol_version=None,
                server=None,
                error=_error_info(status.error),
            )
        infos = [
            ToolInfo(
                name=tool.name,
                title=tool.title,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in tools
        ]
        return McpToolsResponse(
            connected=True,
            tools=infos,
            count=len(infos),
            protocol_version=status.protocol_version,
            server=_server_info(status),
            error=None,
        )

    @app.get("/api/mcp/servers", response_model=McpServersResponse)
    async def mcp_servers() -> McpServersResponse:
        client = app.state.mcp_client
        # Exactly one probe_servers() call: it probes every server in parallel.
        prober = getattr(client, "probe_servers", None)
        if callable(prober):
            probes: list[ServerProbe] = await prober()
        else:  # pragma: no cover - a single-client injection falls back to A
            status, tools = await client.probe()
            probes = [ServerProbe("A", status, tools)]
        entries = [
            ServerEntry(
                label=str(probe.label),
                endpoint=_endpoint_for(client, str(probe.label), resolved),
                connected=bool(probe.status.connected),
                protocol_version=probe.status.protocol_version,
                server=_server_info(probe.status),
                tools_count=int(probe.status.tools_count),
                tool_names=[
                    str(tool.name)
                    for tool in (probe.tools or [])
                    if getattr(tool, "name", None)
                ],
                error=_error_info(probe.status.error),
            )
            for probe in probes
        ]
        return McpServersResponse(servers=entries, checked_at=time.time())

    @app.get("/api/chats/{chat_id}/watches", response_model=ChatWatchesResponse)
    async def chat_watches(chat_id: str) -> ChatWatchesResponse:
        try:
            exists = app.state.chats.chat_exists(chat_id)
        except StorageError:
            raise _storage_http_error() from None
        # The chat check happens before any contact with server B.
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={
                    "category": "chat_not_found",
                    "message": "The chat was not found",
                },
            )
        notifier = app.state.notifier
        try:
            watches_payload = await notifier.list_watches(chat_id)
            deliveries_payload = await notifier.list_deliveries(chat_id)
        except NotifierUnavailable as exc:
            return ChatWatchesResponse(
                chat_id=chat_id,
                available=False,
                error=ErrorInfo(category=exc.category, message=exc.message),
            )
        return ChatWatchesResponse(
            chat_id=chat_id,
            available=True,
            watches=list(watches_payload.get("watches") or []),
            deliveries=list(deliveries_payload.get("deliveries") or []),
        )

    @app.get("/api/chats", response_model=ChatListResponse)
    async def list_chats() -> ChatListResponse:
        chats = _guard_chat(app.state.chats.list_chats)
        return ChatListResponse(
            chats=chats,
            count=len(chats),
            limit=MAX_CHATS,
            limit_reached=len(chats) >= MAX_CHATS,
        )

    @app.post("/api/chats", response_model=ChatInfo, status_code=201)
    async def create_chat(body: ChatCreateRequest) -> ChatInfo:
        info = _guard_chat(lambda: app.state.chats.create_chat(body.title))
        return ChatInfo(**info)

    @app.patch("/api/chats/{chat_id}", response_model=ChatInfo)
    async def rename_chat(chat_id: str, body: ChatRenameRequest) -> ChatInfo:
        info = _guard_chat(lambda: app.state.chats.rename_chat(chat_id, body.title))
        return ChatInfo(**info)

    @app.delete("/api/chats/{chat_id}", response_model=ChatDeleteResponse)
    async def delete_chat(
        chat_id: str, force: bool = Query(default=False)
    ) -> ChatDeleteResponse:
        result = _guard_chat(
            lambda: app.state.chats.delete_chat(chat_id, force=force)
        )
        return ChatDeleteResponse(**result)

    @app.get("/api/chats/{chat_id}/messages", response_model=ChatMessagesResponse)
    async def chat_messages(chat_id: str) -> ChatMessagesResponse:
        messages = _guard_chat(lambda: app.state.chats.get_messages(chat_id))
        return ChatMessagesResponse(
            chat_id=chat_id, messages=messages, count=len(messages)
        )

    @app.post("/api/chats/{chat_id}/clear", response_model=ChatClearResponse)
    async def clear_chat(chat_id: str) -> ChatClearResponse:
        result = _guard_chat(lambda: app.state.chats.clear_chat(chat_id))
        return ChatClearResponse(**result)

    @app.get("/api/chats/{chat_id}/tasks", response_model=ChatTasksResponse)
    async def chat_tasks(chat_id: str) -> ChatTasksResponse:
        tasks = _guard_chat(lambda: app.state.chats.tasks_for_chat(chat_id))
        return ChatTasksResponse(chat_id=chat_id, tasks=tasks, count=len(tasks))

    @app.get("/api/chats/{chat_id}/reports", response_model=ChatReportsResponse)
    async def chat_reports(chat_id: str) -> ChatReportsResponse:
        reports = _guard_chat(lambda: app.state.chats.reports_for_chat(chat_id))
        return ChatReportsResponse(chat_id=chat_id, reports=reports, count=len(reports))

    @app.get(
        "/api/chats/{chat_id}/reports/{report_id}", response_model=ReportDetail
    )
    async def chat_report(chat_id: str, report_id: str) -> ReportDetail:
        report = _guard_chat(
            lambda: app.state.chats.report_for_chat(chat_id, report_id)
        )
        return ReportDetail(**report)

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request, body: ChatRequest):
        try:
            exists = app.state.chats.chat_exists(body.chat_id)
        except StorageError:
            raise _storage_http_error() from None
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={
                    "category": "chat_not_found",
                    "message": "The chat was not found",
                },
            )
        chat_id = body.chat_id

        def history_provider():
            return app.state.chats.context_messages(
                chat_id, app.state.settings.chat_context_messages
            )

        def on_success(user_text: str, assistant_text: str) -> None:
            app.state.chats.append_exchange(chat_id, user_text, assistant_text)

        session = app.state.sessions.session(
            chat_id,
            history_provider=history_provider,
            on_success=on_success,
        )
        orchestrator = Orchestrator(
            provider=app.state.provider,
            mcp_client=app.state.mcp_client,
            trace=app.state.trace,
            max_tool_rounds=app.state.settings.max_tool_rounds,
        )
        request_id = uuid.uuid4().hex
        logger.info("chat request started (message_chars=%d)", len(body.message))

        async def producer(queue: "asyncio.Queue") -> None:
            try:
                async for event in orchestrator.run(request_id, session, body.message):
                    await queue.put(("event", event))
            except asyncio.CancelledError:  # pragma: no cover - client disconnect
                raise
            except Exception:  # noqa: BLE001 - reported as a controlled error
                logger.exception("chat request failed unexpectedly")
                await queue.put(
                    (
                        "event",
                        ErrorEvent(
                            request_id,
                            "internal",
                            "The agent failed to finish the request",
                        ),
                    )
                )
            finally:
                await queue.put(("end", None))

        async def event_stream() -> AsyncIterator[str]:
            queue: "asyncio.Queue" = asyncio.Queue()
            task = asyncio.create_task(producer(queue))
            try:
                while True:
                    try:
                        kind, item = await asyncio.wait_for(
                            queue.get(), timeout=KEEPALIVE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if kind == "end":
                        break
                    yield item.to_sse()
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if STATIC_DIR.is_dir():
        app.mount(
            "/",
            NoCacheStaticFiles(directory=str(STATIC_DIR), html=True),
            name="static",
        )

    return app
