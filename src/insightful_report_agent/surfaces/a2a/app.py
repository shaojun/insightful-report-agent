"""Official SDK routing over the shared runtime; no second agent assembly."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

from a2a.auth.user import User
from a2a.helpers.proto_helpers import new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    ServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    Message,
    Part,
    Role,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotCancelableError, TaskNotFoundError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.local import final_text
from insightful_report_agent.surfaces.a2a.progress import PROGRESS_KEY
from insightful_report_agent.surfaces.a2a.store import PersistentTaskStore
from insightful_report_agent.tools.source_web import urls_from_text

_LOGGER = logging.getLogger(__name__)
MAX_INLINE_FILES = 16
MAX_INLINE_BYTES = 16 * 1024**2
MAX_INLINE_TOTAL = 32 * 1024**2


def input_parts(message: Message | None) -> tuple[str, list[tuple[str, bytes]]]:
    if message is None or message.role != Role.ROLE_USER or not message.parts:
        raise ValueError("Supply a user message with one or more parts.")
    text = []
    files = []
    total = 0
    for part in message.parts:
        kind = part.WhichOneof("content")
        if kind == "text":
            text.append(part.text)
        elif kind == "url":
            if len(part.url) > 4096 or urls_from_text(part.url) != [part.url]:
                raise ValueError("URL parts require a public HTTP(S) URL.")
            text.append(part.url)
        elif kind == "raw":
            if not part.filename or not part.raw or len(part.raw) > MAX_INLINE_BYTES:
                raise ValueError("Raw attachments need a filename and 1 byte to 16 MiB each.")
            total += len(part.raw)
            if total > MAX_INLINE_TOTAL or len(files) >= MAX_INLINE_FILES:
                raise ValueError("Use the context attachment endpoint for larger batches.")
            files.append((part.filename, part.raw))
        else:
            raise ValueError("Supported A2A parts are text, raw attachments, and public URLs.")
    prompt = "\n".join(text).strip()
    if len(prompt) > 20_000:
        raise ValueError("Use at most 20000 characters of text and URLs.")
    if not prompt and not files:
        raise ValueError("Supply text, a URL, or a file.")
    return prompt, files


def public_base(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "INSIGHTFUL_REPORT_AGENT_A2A_URL must be an http(s) origin without credentials or a path"
        )
    return url.rstrip("/")


def agent_card(settings: Settings) -> AgentCard:
    base = public_base(settings.a2a_url or "")
    legacy = settings.source_input_mode == "legacy"
    card = AgentCard(
        name=settings.a2a_name,
        version=settings.a2a_version,
        description="Analyze source collections and deliver a standalone report with evidence, visuals and charts.",
        documentation_url=settings.a2a_documentation_url or "",
        supported_interfaces=[
            AgentInterface(url=f"{base}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=(
            ["text/plain", "text/uri-list", "application/octet-stream"]
            if legacy
            else ["text/plain", "application/zip"]
        ),
        default_output_modes=["text/plain", "text/html"],
        skills=[
            AgentSkill(
                id="evidence-report",
                name="Source-grounded report",
                description=(
                    "Review a collection of documents, web pages and media and create a cited report."
                    if legacy
                    else "Create a cited HTML report from a question or outline and preprocessor result ZIPs."
                ),
                tags=["reports", "documents", "evidence", "charts"],
                examples=["Compare these sources and cite the evidence behind your findings."],
            )
        ],
    )
    if not settings.a2a_allow_anonymous:
        card.security_schemes["bearer"].http_auth_security_scheme.scheme = "bearer"
        card.security_requirements.add().schemes["bearer"].SetInParent()
    return card


class Caller(User):
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self.name


class CallerContext(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        # Only the verified identity crosses into SDK state; never retain auth headers in tasks.
        return ServerCallContext(
            user=Caller(request.state.a2a_caller),
            state={"headers": {"a2a-version": request.headers.get("a2a-version", "")}},
        )


def thread_for(caller: str, context_id: str) -> str:
    digest = hashlib.sha256(f"{len(caller)}:{caller}{context_id}".encode()).hexdigest()
    return f"a2a-{digest}"


class ReportExecutor(AgentExecutor):
    def __init__(self, app: FastAPI, base: str) -> None:
        self.app = app
        self.base = base
        self.locks: dict[str, asyncio.Lock] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id, context.context_id
        if not task_id or not context_id:
            raise ValueError("A2A task and context ids are required")
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[],
                )
            )
        await updater.submit()
        if context.configuration is not None and context.configuration.HasField(
            "task_push_notification_config"
        ):
            await updater.reject(
                self._message("Push notifications are not supported.", context_id, task_id)
            )
            return
        try:
            text, files = input_parts(context.message)
        except ValueError as error:
            await updater.reject(self._message(str(error), context_id, task_id))
            return
        state = self.app.state.api
        caller = context.call_context.user.user_name
        thread_id = thread_for(caller, context_id)
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            from insightful_report_agent.surfaces.api.app import THREADS, UPDATES, _files

            await state.store.aput(THREADS, thread_id, {"owner": caller})
            await updater.start_work(self._message("Agent started working.", context_id, task_id))

            async def progress(data: dict[str, Any]) -> None:
                labels = {"running": "开始", "completed": "完成", "failed": "失败"}
                text = f"工具 {data['tool']} · {labels[data['state']]} · {data['elapsedSeconds']}s"
                message = self._message(text, context_id, task_id)
                message.metadata.update({PROGRESS_KEY: data})
                await updater.update_status(TaskState.TASK_STATE_WORKING, message)

            self.app.state.a2a_progress[thread_id] = progress
            try:
                before = len(await state.store.asearch((*UPDATES, thread_id), limit=10_000))
                paths = await state.runtime.attach(thread_id, files) if files else []
                urls = urls_from_text(text)
                prose = text
                for url in urls:
                    prose = prose.replace(url, "")
                configurable = {"surface": "a2a"}
                legacy = state.runtime.components.source_library.legacy
                if legacy and (paths or urls) and not prose.strip():
                    configurable["sources_only"] = len(paths) + len(urls)
                elif paths and not text:
                    configurable["sources_only"] = len(paths)
                added = await state.runtime.ask(
                    text, thread_id=thread_id, configurable=configurable
                )
                reply = final_text(added)
                if reply:
                    await updater.start_work(self._message(reply, context_id, task_id))
                # A lead can return while its helpers render. Keep the A2A task working.
                # Read helper replies while they run, rather than collecting only at completion.
                while True:
                    pending = not configurable.get("sources_only") and state.runtime.thread_busy(
                        thread_id
                    )
                    updates = sorted(
                        await state.store.asearch((*UPDATES, thread_id), limit=10_000),
                        key=lambda i: i.key,
                    )
                    for item in updates[before:]:
                        reply = str(item.value.get("text", "")) or reply
                        await updater.start_work(self._message(reply, context_id, task_id))
                    before = len(updates)
                    if not pending:
                        break
                    await asyncio.sleep(0.5)
                if reply:
                    await updater.add_artifact([Part(text=reply)], name="reply")
                for name in _files(state.runtime, thread_id):
                    await updater.add_artifact(
                        [
                            Part(
                                url=f"{self.base}/threads/{thread_id}/files/{quote(name, safe='')}",
                                filename=name,
                                media_type=mimetypes.guess_type(name)[0]
                                or "application/octet-stream",
                            )
                        ],
                        name=name,
                    )
                await updater.complete(
                    self._message(reply or "Turn completed.", context_id, task_id)
                )
            except Exception as error:  # noqa: BLE001 - model/tool errors become safe task failures
                _LOGGER.warning("A2A execution failed: %s", type(error).__name__)
                await updater.failed(
                    self._message(
                        "Agent execution failed; contact the operator with this task id.",
                        context_id,
                        task_id,
                    )
                )
            finally:
                self.app.state.a2a_progress.pop(thread_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:  # noqa: ARG002
        # Stopping the lead alone would leave renders/helpers running and falsely claim cancellation.
        raise TaskNotCancelableError(
            message="This runtime cannot safely cancel a complete report job through A2A."
        )

    @staticmethod
    def _message(text: str, context_id: str, task_id: str) -> Message:
        return Message(
            message_id=uuid4().hex,
            role=Role.ROLE_AGENT,
            parts=[Part(text=text)],
            context_id=context_id,
            task_id=task_id,
        )


class ReportRequestHandler(DefaultRequestHandler):
    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        # Reject before the SDK attempts to cancel the producer: helpers may still be rendering.
        if await self.task_store.get(params.id, context) is None:
            raise TaskNotFoundError()
        raise TaskNotCancelableError(
            message="A2A job cancellation is not supported by this runtime."
        )


def install_a2a(app: FastAPI, settings: Settings) -> DefaultRequestHandler:
    from insightful_report_agent.surfaces.api.app import ANONYMOUS_A2A_CALLER, _caller

    card = agent_card(settings)
    app.state.a2a_progress = {}
    store = PersistentTaskStore(app.state)
    handler = ReportRequestHandler(
        ReportExecutor(app, public_base(settings.a2a_url or "")), store, card
    )
    app.state.a2a_store = store
    app.state.a2a_card = card
    app.routes.extend(create_agent_card_routes(card))
    app.routes.extend(
        create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=CallerContext())
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if request.url.path.rstrip("/") == "/a2a":
            try:
                request.state.a2a_caller = (
                    ANONYMOUS_A2A_CALLER if settings.a2a_allow_anonymous else _caller(request)
                )
            except HTTPException as error:
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code, headers=error.headers
                )
        return await call_next(request)

    return handler
