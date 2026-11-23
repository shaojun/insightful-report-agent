from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from langgraph.store.memory import InMemoryStore

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.waker.local import CHECK_INS, InProcessWaker
from insightful_report_agent.surfaces import service
from insightful_report_agent.surfaces.api import app as api_app
from insightful_report_agent.surfaces.api.app import create_app
from insightful_report_agent.surfaces.slack import bolt_app


@pytest.fixture
def stub_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def build(*args, **kwargs):
        return SimpleNamespace(
            binding=SimpleNamespace(name="scripted", runner=None),
            services=SimpleNamespace(sweep=AsyncMock()),
            source_intake=SimpleNamespace(restore=AsyncMock()),
            waker=SimpleNamespace(restore=AsyncMock()),
            aclose=AsyncMock(),
        )

    @asynccontextmanager
    async def persistence(*args):
        yield None, InMemoryStore()

    monkeypatch.setattr(api_app, "open_persistence", persistence)
    monkeypatch.setattr(api_app, "servers_for_runtime", lambda *args: [])
    monkeypatch.setattr(service, "create_app", partial(create_app, build=build))


async def test_built_in_routes_without_channel_or_caller_config(
    tmp_path: Path, stub_runtime: None
) -> None:
    settings = Settings(_env_file=None, workspace_root=tmp_path, api_port=9183)
    web = service.create_service_app(tmp_path, settings)
    async with (
        web.router.lifespan_context(web),
        AsyncClient(transport=ASGITransport(app=web), base_url="http://test") as client,
    ):
        assert (await client.get("/health")).json()["ok"]
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:9183/a2a"
        assert card["securityRequirements"]
        assert (await client.post("/a2a", json={})).status_code == 401
        assert (
            await client.post("/threads/api-test/messages", json={"text": "test"})
        ).status_code == 401
        assert (await client.post("/slack/events", json={})).status_code == 404


@pytest.mark.parametrize("api_only", [False, True])
async def test_configured_channels_share_service_lifecycle(
    tmp_path: Path, stub_runtime: None, monkeypatch: pytest.MonkeyPatch, api_only: bool
) -> None:
    class SlackHandler:
        async def handle(self, request):
            return {"received": await request.json()}

    events: list[str] = []
    env_file = tmp_path / ".env.override"
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path,
        feishu_app_id="test",
        feishu_app_secret="test",
        feishu_tenant_key="test",
        slack_transport="http",
        slack_bot_token="test",
        slack_signing_secret="test",
    )

    async def run_feishu(root, override):
        assert (root, override) == (tmp_path, env_file)
        events.append("feishu started")
        try:
            await asyncio.Future()
        finally:
            events.append("feishu stopped")

    async def run_slack(root, selected, *, web):
        assert (root, selected) == (tmp_path, settings)
        web.state.slack_handler = SlackHandler()
        events.append("slack started")
        try:
            await asyncio.Future()
        finally:
            events.append("slack stopped")

    monkeypatch.setattr(service, "_run_feishu", run_feishu)
    monkeypatch.setattr(bolt_app, "serve", run_slack)
    web = service.create_service_app(tmp_path, settings, env_file=env_file, api_only=api_only)
    async with (
        web.router.lifespan_context(web),
        AsyncClient(transport=ASGITransport(app=web), base_url="http://test") as client,
    ):
        assert (await client.get("/health")).json()["ok"]
        response = await client.post("/slack/events", json={"type": "event_callback"})
        if api_only:
            assert response.status_code == 404
        else:
            assert response.json() == {"received": {"type": "event_callback"}}
    assert set(events) == (
        set()
        if api_only
        else {"feishu started", "feishu stopped", "slack started", "slack stopped"}
    )


async def test_recovery_leaves_other_surfaces_check_ins_pending() -> None:
    store = InMemoryStore()
    for thread in ("api-one", "a2a-two", "feishu:tenant:chat:dm", "slack-T1-C1-1.2"):
        await store.aput(
            CHECK_INS, thread, {"thread_id": thread, "at": "2099-01-01T00:00:00+00:00"}
        )
    waker = InProcessWaker(AsyncMock(), store=store)
    assert await waker.restore(thread_prefix=("api-", "a2a-")) == 2
    assert waker.pending_threads() == {"api-one", "a2a-two"}
    await waker.aclose()
    assert len(await store.asearch(CHECK_INS)) == 4
