"""The self-hosted HTTP API: one process runs the agent, its helpers, wake-ups and housekeeping.

Not the LangGraph Agent Server protocol; a small surface for your own tools and the Slack adapter:

- `POST /threads/{thread_id}/messages` `{"text": "..."}` runs one turn and returns the reply, the
  files delivered on the thread so far, and whether background work is still running.
- `GET /threads/{thread_id}/updates?after=N` returns what background work posted since (wake-up
  replies), in order, with a cursor.
- `POST /threads/{thread_id}/attachments` (multipart `files`) puts files in the thread's
  `/workspace/attachments/`; mention them in the next message.
- `GET /threads/{thread_id}/files/{name}` downloads a delivered file.
- `GET /health`.

Ordinary thread routes need `Authorization: Bearer <token>`; `INSIGHTFUL_REPORT_AGENT_ACCESS_TOKEN_CALLERS` maps
tokens to callers (`token:caller,...`). A thread belongs to the caller who started it; another
caller gets 404 for it, never its contents. With no tokens configured, protected routes return 401.
A2A requires caller tokens by default (see surfaces/a2a/).

Durability comes from `runtime/persistence.py` (Postgres with `DATABASE_URL`). At startup the
server sweeps what a restart killed and re-arms check-ins; every 15 minutes it sweeps again.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime import maintenance
from insightful_report_agent.runtime.local import LocalRuntime, build_local_runtime, final_text
from insightful_report_agent.runtime.persistence import open_persistence, workspace_of
from insightful_report_agent.tools.mcp import load_mcp_tools, servers_for_runtime
from insightful_report_agent.tools.paper import PaperPolicy, admit_paper_tools

_LOGGER = logging.getLogger(__name__)

THREADS = ("insightful_report_agent", "api_threads")
UPDATES = ("insightful_report_agent", "api_updates")
THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
MAX_ATTACHMENTS = 100
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024
MAX_BATCH_BYTES = 256 * 1024 * 1024
THREAD_PREFIXES = ("api-", "a2a-")


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class MessageOut(BaseModel):
    reply: str
    files: list[str]
    working: bool


class Updates(BaseModel):
    updates: list[str]
    cursor: int


@dataclass
class ApiState:
    runtime: LocalRuntime
    store: Any
    tokens: dict[str, str]
    _counters: dict[str, int] = field(default_factory=dict)
    ownership_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record_update(self, thread_id: str, text: str) -> None:
        count = self._counters.get(thread_id)
        if count is None:
            count = len(await self.store.asearch((*UPDATES, thread_id), limit=10_000))
        self._counters[thread_id] = count + 1
        await self.store.aput((*UPDATES, thread_id), f"{count:08d}", {"text": text})


ANONYMOUS_A2A_CALLER = "a2a-anonymous"


def _caller(request: Request) -> str:
    state: ApiState = request.app.state.api
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        for known, caller in state.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                return caller
    raise HTTPException(
        status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )


async def _owned(request: Request, thread_id: str, caller: str, *, create: bool) -> None:
    if not THREAD_ID.fullmatch(thread_id):
        raise HTTPException(status_code=404, detail="no such thread")
    state: ApiState = request.app.state.api
    async with state.ownership_lock:
        item = await state.store.aget(THREADS, thread_id)
        if item is None and create:
            if not thread_id.startswith("api-"):
                raise HTTPException(
                    status_code=400, detail="new HTTP thread ids must start with api-"
                )
            await state.store.aput(THREADS, thread_id, {"owner": caller})
            return
        if item is None or not hmac.compare_digest(str(item.value.get("owner")), caller):
            raise HTTPException(status_code=404, detail="no such thread")


async def upload_batch(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if len(files) > MAX_ATTACHMENTS:
        raise HTTPException(status_code=413, detail=f"at most {MAX_ATTACHMENTS} files")
    loaded = []
    total = 0
    for upload in files:
        content = await upload.read(min(MAX_ATTACHMENT_BYTES, MAX_BATCH_BYTES - total) + 1)
        total += len(content)
        if len(content) > MAX_ATTACHMENT_BYTES or total > MAX_BATCH_BYTES:
            raise HTTPException(status_code=413, detail="limit 200 MiB per file, 256 MiB per batch")
        loaded.append((upload.filename or "attachment", content))
    return loaded


def _files(runtime: LocalRuntime, thread_id: str) -> list[str]:
    folder = runtime.out_dir / thread_id
    return sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []


def create_app(
    project_root: Path,
    settings: Settings | None = None,
    *,
    build: Callable[..., LocalRuntime] = build_local_runtime,
    channels: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    tokens = settings.access_token_callers_map()
    register_nacos = bool(settings.nacos_url and settings.a2a_url)
    if settings.nacos_url and not register_nacos:
        _LOGGER.warning("Nacos registration skipped. Set INSIGHTFUL_REPORT_AGENT_A2A_URL.")
    if not settings.a2a_url:
        # Advertise a usable address when the listener binds a wildcard address.
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(settings.api_host, settings.api_host)  # noqa: S104
        authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
        settings = settings.model_copy(
            update={"a2a_url": f"http://{authority}:{settings.api_port}"}
        )
    registry = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        servers = servers_for_runtime(project_root, settings, "self_hosted")
        loaded = await load_mcp_tools(servers)
        servers = [s for s in servers if s.name not in loaded.failed]
        loaded.tools = admit_paper_tools(loaded.tools, PaperPolicy.from_settings(settings))
        async with open_persistence(settings, workspace_of(settings, project_root)) as (
            checkpointer,
            store,
        ):
            holder: list[ApiState] = []

            def posted(thread_id: str, text: str) -> None:
                asyncio.get_running_loop().create_task(holder[0].record_update(thread_id, text))

            from insightful_report_agent.surfaces.a2a.progress import A2AProgressMiddleware

            surface_middleware = (A2AProgressMiddleware(app.state.a2a_progress),)

            runtime = build(
                settings,
                project_root,
                checkpointer=checkpointer,
                store=store,
                replies=posted,
                surface="api",
                mcp_servers=servers,
                mcp_tools=loaded.tools,
                surface_middleware=surface_middleware,
            )
            holder.append(ApiState(runtime=runtime, store=store, tokens=tokens))
            app.state.api = holder[0]
            async with AsyncExitStack() as stack:
                if runtime.binding.runner is not None:
                    stack.callback(runtime.binding.runner.stop_all)
                stack.push_async_callback(runtime.aclose)
                stack.push_async_callback(a2a_handler.aclose)
                await app.state.a2a_store.recover()
                await maintenance.start(runtime, store, thread_prefix=THREAD_PREFIXES)
                keeper = asyncio.create_task(
                    maintenance.run(runtime, store, thread_prefix=THREAD_PREFIXES),
                    name="api maintenance",
                )

                async def stop_maintenance() -> None:
                    keeper.cancel()
                    await asyncio.gather(keeper, return_exceptions=True)

                stack.push_async_callback(stop_maintenance)
                if registry is not None:
                    registry.start()
                    stack.push_async_callback(registry.aclose)
                if channels is not None:
                    await stack.enter_async_context(channels(app))
                yield

    app = FastAPI(title="Insightful Report Agent", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state: ApiState | None = getattr(request.app.state, "api", None)
        return {
            "ok": state is not None,
            "sandbox": state.runtime.binding.name if state else None,
            "nacos": {"registered": registry.registered, "error": registry.last_error}
            if registry is not None
            else None,
        }

    @app.post("/threads/{thread_id}/messages", response_model=MessageOut)
    async def post_message(
        thread_id: str, body: MessageIn, request: Request, caller: str = Depends(_caller)
    ) -> MessageOut:
        await _owned(request, thread_id, caller, create=True)
        state: ApiState = request.app.state.api
        with state.runtime.trace.request(thread_id, "api", caller):
            added = await state.runtime.ask(body.text, thread_id=thread_id)
            return MessageOut(
                reply=final_text(added),
                files=_files(state.runtime, thread_id),
                working=state.runtime.busy(),
            )

    @app.post("/threads/{thread_id}/attachments")
    async def attach(
        thread_id: str,
        request: Request,
        files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI's declaration style
        caller: str = Depends(_caller),
    ) -> dict[str, list[str]]:
        await _owned(request, thread_id, caller, create=True)
        state: ApiState = request.app.state.api
        with state.runtime.trace.request(thread_id, "api", caller):
            return {"paths": await state.runtime.attach(thread_id, await upload_batch(files))}

    @app.post("/a2a/contexts/{context_id}/attachments")
    async def attach_context(
        context_id: str,
        request: Request,
        files: list[UploadFile] = File(...),  # noqa: B008
        caller: str = Depends(_caller),
    ) -> dict[str, list[str]]:
        from insightful_report_agent.surfaces.a2a.app import thread_for

        if settings.a2a_allow_anonymous:
            raise HTTPException(status_code=403, detail="context uploads require authenticated A2A")
        if len(context_id) > 256 or not context_id.strip():
            raise HTTPException(status_code=400, detail="invalid context id")
        state: ApiState = request.app.state.api
        thread_id = thread_for(caller, context_id)
        await _owned(request, thread_id, caller, create=False)
        with state.runtime.trace.request(thread_id, "a2a", caller):
            return {"paths": await state.runtime.attach(thread_id, await upload_batch(files))}

    @app.get("/threads/{thread_id}/updates", response_model=Updates)
    async def updates(
        thread_id: str, request: Request, after: int = 0, caller: str = Depends(_caller)
    ) -> Updates:
        await _owned(request, thread_id, caller, create=False)
        state: ApiState = request.app.state.api
        items = sorted(
            await state.store.asearch((*UPDATES, thread_id), limit=10_000), key=lambda i: i.key
        )
        fresh = [str(i.value.get("text", "")) for i in items[after:]]
        return Updates(updates=fresh, cursor=len(items))

    def file_caller(thread_id: str, request: Request) -> str:
        # Anonymous access covers A2A output files only, never ordinary HTTP threads.
        if settings.a2a_allow_anonymous and thread_id.startswith("a2a-"):
            return ANONYMOUS_A2A_CALLER
        return _caller(request)

    @app.get("/threads/{thread_id}/files/{name}")
    async def download(
        thread_id: str, name: str, request: Request, caller: str = Depends(file_caller)
    ) -> FileResponse:
        await _owned(request, thread_id, caller, create=False)
        state: ApiState = request.app.state.api
        folder = (state.runtime.out_dir / thread_id).resolve()
        path = (folder / name).resolve()
        if path.parent != folder or not path.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(path, filename=path.name, headers={"X-Content-Type-Options": "nosniff"})

    from insightful_report_agent.surfaces.a2a.app import install_a2a

    a2a_handler = install_a2a(app, settings)
    if register_nacos:
        from insightful_report_agent.surfaces.a2a.nacos import NacosRegistry

        registry = NacosRegistry(settings, app.state.a2a_card)
    return app
