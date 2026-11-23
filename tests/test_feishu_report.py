"""Simulated Feishu events and REST through the real report graph and Docker sandbox."""

from __future__ import annotations

import asyncio
import io
import json
import os
from email import policy
from email.parser import BytesParser
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from bs4 import BeautifulSoup
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore
from PIL import Image

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.local import build_local_runtime
from insightful_report_agent.surfaces.feishu.app import (
    FeishuAdapter,
    FeishuFileDelivery,
    FeishuProgressMiddleware,
    thread_id_for,
)
from insightful_report_agent.surfaces.feishu.client import FeishuClient
from insightful_report_agent.tools.delivery import LocalOutputAdapter
from insightful_report_agent.tools.mineru import MinerU
from tests.scripted_model import RespondingChatModel, ScriptedChatModel, reply, tool_call
from tests.test_report_graph import tool_data

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.getenv("INSIGHTFUL_DOCKER_TESTS") != "1", reason="needs the report Docker image"
)


def event(mid: str, kind: str, body: dict, *, first: bool = False) -> dict:
    return {
        "header": {
            "event_type": "im.message.receive_v1",
            "app_id": "cli_test",
            "tenant_key": "tenant",
        },
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_person"},
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": mid,
                "chat_id": "oc_chat",
                "chat_type": "group",
                "root_id": "om_first",
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}] if first else [],
                "message_type": kind,
                "content": json.dumps(body, ensure_ascii=False),
            },
        },
    }


async def test_feishu_multiple_messages_and_files_to_rich_report(tmp_path: Path) -> None:
    photo = io.BytesIO()
    Image.new("RGB", (200, 90), "#c0e5ed").save(photo, "PNG")
    archive = io.BytesIO()
    with ZipFile(archive, "w") as members:
        members.writestr("业绩资料.txt", "第三季度订单 30 单，第四季度订单 42 单。")
    inputs = {"file_orders": archive.getvalue(), "img_photo": photo.getvalue()}
    downloads: list[tuple[str, str]] = []
    sent: list[tuple[str, dict]] = []
    uploaded: dict[str, bytes] = {}
    cards: list[dict] = []

    mineru_calls: list[str] = []
    mineru_result = io.BytesIO()
    with ZipFile(mineru_result, "w") as result:
        result.writestr("full.md", "![Figure](images/photo.png)")
        result.writestr("images/photo.png", photo.getvalue())
        result.writestr(
            "photo_content_list.json",
            json.dumps(
                [
                    {
                        "type": "image",
                        "page_idx": 0,
                        "img_path": "images/photo.png",
                        "image_caption": ["Source illustration"],
                    }
                ]
            ),
        )

    def mineru_platform(request: httpx.Request) -> httpx.Response:
        mineru_calls.append(request.method)
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer test-mineru-token"
            assert json.loads(request.content)["files"][0]["name"].endswith(".png")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "test-intake",
                        "file_urls": ["https://example.aliyuncs.com/upload"],
                    },
                },
            )
        if request.method == "PUT":
            assert request.content == photo.getvalue()
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "state": "done",
                                "full_zip_url": "https://cdn-mineru.openxlab.org.cn/result.zip",
                            },
                        ]
                    },
                },
            )
        return httpx.Response(200, content=mineru_result.getvalue())

    def platform(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer test-token"
        if "/resources/" in path:
            key = path.split("/")[-1]
            downloads.append((key, request.url.params["type"]))
            return httpx.Response(200, content=inputs[key])
        if path.endswith("/im/v1/files"):
            raw = (
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            form = list(BytesParser(policy=policy.default).parsebytes(raw).iter_parts())
            item = next(part for part in form if part.get_filename())
            uploaded[item.get_filename()] = item.get_payload(decode=True)
            assert (
                next(
                    part
                    for part in form
                    if part.get_param("name", header="content-disposition") == "file_type"
                ).get_content()
                == "stream"
            )
            return httpx.Response(
                200, json={"code": 0, "data": {"file_key": f"file_{len(uploaded)}"}}
            )
        if path.endswith("/reply"):
            sent.append((path, json.loads(request.content)))
        if request.method == "PATCH":
            cards.append(json.loads(json.loads(request.content)["content"]))
        return httpx.Response(
            200, json={"code": 0, "data": {"message_id": f"om_reply_{len(sent)}"}}
        )

    def read_high(messages):
        sources = tool_data(messages, "list_sources")["sources"]
        source = next(item["id"] for item in sources if item["name"].endswith("业绩资料.txt"))
        return tool_call("read_source_file_abstract", {"file_path": source, "level": "high"})

    def read_middle(messages):
        high = tool_data(messages, "read_source_file_abstract")
        assert high["path"].endswith(".abstract.high")
        assert high["level"] == "high" and high["generated"] is False
        assert abstract.call_count == 3  # Two original abstracts and one corpus overview
        return tool_call(
            "read_source_file_abstract", {"file_path": high["source_id"], "level": "middle"}
        )

    def read_cached_middle(messages):
        middle = tool_data(messages, "read_source_file_abstract")
        assert middle["path"].endswith(".abstract.middle")
        assert middle["level"] == "middle" and middle["generated"] is True
        assert abstract.call_count == 4
        return tool_call(
            "read_source_file_abstract", {"file_path": middle["source_id"], "level": "middle"}
        )

    def read_low(messages):
        middle = tool_data(messages, "read_source_file_abstract")
        assert middle["generated"] is False
        assert abstract.call_count == 4
        return tool_call(
            "read_source_file_abstract", {"file_path": middle["source_id"], "level": "low"}
        )

    def draft(messages):
        assert tool_data(messages, "read_source_file_abstract")["path"].endswith(".abstract.low")
        assert abstract.call_count == 5
        sources = tool_data(messages, "list_sources")["sources"]
        image = next(item["id"] for item in sources if item["name"].endswith(".png"))
        text = next(item["id"] for item in sources if item["name"].endswith("业绩资料.txt"))
        ref = f"[[{text}#b0001]]"
        chart = json.dumps(
            {
                "title": "订单对比",
                "unit": "单",
                "data": [
                    {"label": "Q3", "value": 30, "evidence": ref},
                    {"label": "Q4", "value": 42, "evidence": ref},
                ],
            },
            ensure_ascii=False,
        )
        content = (
            "# 季度报告\n\n"
            f"两季度订单分别为 30 单和 42 单，第四季度比第三季度多 12 单。{ref}"
            "当前材料只覆盖这两个季度，无法判断增长趋势是否持续。"
            "建议后续按月汇总订单，并用相同的统计口径核对同期数据和主要订单来源。\n\n"
        )
        content += f"![来源截图](source:{image}/a0001)\n\n```chart\n{chart}\n```\n"
        return tool_call(
            "write_file", {"file_path": "/workspace/projects/feishu-report.md", "content": content}
        )

    def published(messages):
        result = tool_data(messages, "publish_report")
        assert result["reviewed"] == 2 and result["visuals"] == 2
        assert result["source_figures"] == 1
        return reply("季度报告已交付。")

    def publish_after_validation(messages):
        result = tool_data(messages, "publish_report")
        assert result["validated"] is True and "delivered" not in result
        assert not any(name.endswith(".html") for name in uploaded)
        return tool_call(
            "publish_report",
            {
                "draft_path": "/workspace/projects/feishu-report.md",
                "require_source_figures": True,
            },
        )

    primary = ScriptedChatModel(
        steps=[
            lambda _: tool_call("prepare_source_overview", {}),
            lambda _: tool_call("synthesize_source_overview", {}),
            lambda _: tool_call("list_sources", {}),
            read_high,
            read_middle,
            read_cached_middle,
            read_low,
            draft,
            lambda _: tool_call(
                "publish_report",
                {
                    "draft_path": "/workspace/projects/feishu-report.md",
                    "validate_only": True,
                    "require_source_figures": True,
                },
            ),
            publish_after_validation,
            published,
        ]
    )
    abstract = RespondingChatModel(respond=lambda _: AIMessage(content="测试材料摘要。"))
    store = InMemoryStore()
    client = FeishuClient("cli_test", "test-only-secret", transport=httpx.MockTransport(platform))
    adapter = FeishuAdapter(client, "cli_test", "tenant", "ou_bot", store=store)
    runtime = build_local_runtime(
        Settings(
            _env_file=None,
            source_input_mode="legacy",
            workspace_root=tmp_path,
            source_visual_analysis=False,
            mineru_api_token="test-mineru-token",
        ),
        ROOT,
        model=primary,
        role_models={"abstract": abstract},
        store=store,
        surface="feishu",
        surface_middleware=(FeishuProgressMiddleware(adapter),),
        source_intake_notify=adapter.notify_source_intake,
        delivery=FeishuFileDelivery(client, "tenant", LocalOutputAdapter(tmp_path / "out")),
    )
    adapter.runtime = runtime
    runtime.components.source_library.mineru = MinerU(
        "test-mineru-token", transport=httpx.MockTransport(mineru_platform)
    )

    chat = f"oc_{uuid4().hex}"
    thread = thread_id_for("tenant", chat, "om_first")

    async def receive(payload: dict) -> None:
        payload["event"]["message"]["chat_id"] = chat
        adapter.dispatch(payload)
        await asyncio.gather(*adapter._tasks)

    try:
        first = event(
            "om_first", "file", {"file_key": "file_orders", "file_name": "材料.zip"}, first=True
        )
        await receive(first)
        await receive(first)  # Replayed provider event must not duplicate the upload.
        await receive(event("om_photo", "image", {"image_key": "img_photo"}))
        assert downloads == [("file_orders", "file"), ("img_photo", "image")]
        if job := runtime.source_intake.tasks.get(thread):
            await asyncio.wait_for(asyncio.shield(job), timeout=90)
        assert primary.call_count == 0 and abstract.call_count == 2
        assert mineru_calls == ["POST", "PUT", "GET", "GET"]
        assert set(uploaded) == {"业绩资料.txt.abstract.high.md", "img_photo.png.abstract.high.md"}
        for abstract_bytes in uploaded.values():
            assert "测试材料摘要。" in abstract_bytes.decode()
            assert "## Evidence index" not in abstract_bytes.decode()
        posts = [
            json.loads(data["content"])["zh_cn"]["content"][0][0]["text"]
            for _, data in sent
            if data["msg_type"] == "post"
        ]
        for status in ("正在解析并生成归一化", "正在生成高层摘要", "附件处理结束"):
            assert any(status in post for post in posts)
        assert sum(data["msg_type"] == "file" for _, data in sent) == 2

        # Sending identical bytes again can reuse the prepared source and must not resend its file.
        await receive(event("om_duplicate_photo", "image", {"image_key": "img_photo"}))
        await asyncio.wait_for(runtime.source_intake.wait_ready(thread), timeout=20)
        assert primary.call_count == 0 and abstract.call_count == 2
        assert sum(data["msg_type"] == "file" for _, data in sent) == 2

        runner = runtime.binding.runner
        assert runner is not None
        staged = await asyncio.to_thread(
            runner.exec, thread, "find /workspace/source-library -type f -printf '%f\n'"
        )
        names = staged.output.splitlines()
        assert names.count("source.md") == 2
        assert sum(name.endswith(".abstract.high") for name in names) == 2
        assert not any(name.endswith((".abstract.middle", ".abstract.low")) for name in names)
        await receive(
            event(
                "om_request",
                "post",
                {
                    "content": [
                        [{"tag": "text", "text": "请为这批材料生成一份有引用和图表的季度报告。"}]
                    ],
                    "files": [{"file_key": "file_orders", "file_name": "材料.zip"}],
                },
            )
        )
        assert len(uploaded) == 3
        filename, report = next(
            (name, data) for name, data in uploaded.items() if name.endswith(".html")
        )
        assert next((tmp_path / "out").rglob(filename)).read_bytes() == report
        html = BeautifulSoup(report, "html.parser")
        assert html.h1.get_text() == "季度报告"
        assert "2/2 reviewed" in html.get_text()
        assert html.img["src"].startswith("data:image/jpeg;base64,")
        assert html.svg is not None and "42" in html.get_text()
        assert "业绩资料.txt" in html.get_text()
        assert all(html.find(id=node["href"][1:]) for node in html.select("a.cite"))
        delivery = [(path, data) for path, data in sent if data["msg_type"] == "file"]
        assert len(delivery) == 3
        assert all(
            path.endswith("/om_first/reply") and data["reply_in_thread"] for path, data in delivery
        )
        assert [json.loads(data["content"]) for _, data in delivery] == [
            {"file_key": "file_1"},
            {"file_key": "file_2"},
            {"file_key": "file_3"},
        ]
        assert any(
            "季度报告已交付" in json.loads(data["content"])["zh_cn"]["content"][0][0]["text"]
            for _, data in sent
            if data["msg_type"] == "post"
        )
        assert any(
            "已收到你的文字请求，接下来会继续处理"
            in json.loads(data["content"])["zh_cn"]["content"][0][0]["text"]
            for _, data in sent
            if data["msg_type"] == "post"
        )
        assert cards and "处理完成" in cards[-1]["header"]["title"]["content"]
        last_card = json.dumps(cards[-1], ensure_ascii=False)
        for step in (
            "已综合 2 份材料的高层摘要",
            "已读取高层摘要",
            "已生成中层摘要",
            "已读取中层摘要",
            "已生成低层摘要",
        ):
            assert step in last_card
    finally:
        await adapter.aclose()
        await runtime.aclose()
        runner = runtime.binding.runner
        assert runner is not None
        runner.stop_all()
        import subprocess

        container, volume = runner.names_for(thread_id_for("tenant", chat, "om_first"))
        subprocess.run(["docker", "container", "rm", container], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
        client.close()
