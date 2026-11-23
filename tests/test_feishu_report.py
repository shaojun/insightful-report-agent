"""Simulated Feishu events and REST through the real report graph and Docker sandbox."""

from __future__ import annotations

import asyncio
import io
import json
import os
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
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
from insightful_report_agent.runtime.preprocessor import PreprocessorError
from insightful_report_agent.surfaces.feishu import app as feishu_app
from insightful_report_agent.surfaces.feishu.app import (
    FeishuAdapter,
    FeishuFileDelivery,
    FeishuProgressMiddleware,
    thread_id_for,
)
from insightful_report_agent.surfaces.feishu.client import (
    FeishuClient,
    FeishuError,
    FolderAttachmentError,
)
from insightful_report_agent.tools.delivery import LocalOutputAdapter
from insightful_report_agent.tools.mineru import MinerU
from tests.scripted_model import RespondingChatModel, ScriptedChatModel, reply, tool_call
from tests.test_report_graph import tool_data

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    ("count", "batch_bytes", "error"),
    [
        (20, 40, None),
        (101, 202, "每条消息最多可发送 100 个附件"),
        (20, 37, "附件大小超限"),
    ],
)
async def test_feishu_post_batches_pdf_attachments(monkeypatch, count, batch_bytes, error):
    monkeypatch.setattr(feishu_app, "MAX_BATCH_BYTES", batch_bytes)
    client = MagicMock(spec=FeishuClient)

    def download(_message, _key, _kind, *, max_bytes):
        if max_bytes < 2:
            raise FeishuError("attachment_too_large")
        return b"ok"

    client.download.side_effect = download
    runtime = MagicMock()
    runtime.attach = AsyncMock()
    runtime.ask = AsyncMock(return_value=[AIMessage(content="已收到报告要求")])
    adapter = FeishuAdapter(
        client,
        "cli_test",
        "tenant",
        "ou_bot",
        enable_progress=False,
        runtime=runtime,
        store=InMemoryStore(),
    )
    prompt = "write me a report of 2k text"
    payload = event(
        "om_batch",
        "post",
        {
            "content": [[{"tag": "text", "text": prompt}]],
            "files": [
                {"file_key": f"file_{i}", "file_name": f"patent-{i}.pdf"} for i in range(count)
            ],
        },
        first=True,
    )

    await adapter.handle(payload)

    if error:
        assert error in str(client.send.call_args.args[3])
        runtime.attach.assert_not_awaited()
        runtime.ask.assert_not_awaited()
        if count > feishu_app.MAX_FILES:
            client.download.assert_not_called()
        else:
            assert client.download.call_args.kwargs == {"max_bytes": 1}
    else:
        runtime.attach.assert_awaited_once()
        thread, files = runtime.attach.await_args.args
        assert files == [(f"patent-{i}.pdf", b"ok") for i in range(count)]
        runtime.ask.assert_awaited_once_with(prompt, thread_id=thread)


async def test_feishu_dm_reminds_user_while_attachment_waits_for_active_turn():
    client = MagicMock(spec=FeishuClient)
    client.download.return_value = b"extra material"
    runtime = MagicMock()
    runtime.attach = AsyncMock()
    started = asyncio.Event()
    finish = asyncio.Event()
    reminder = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def ask(text, *, thread_id):
        if text == "write a report":
            started.set()
            await finish.wait()
        return [AIMessage(content="收到")]

    def post(_chat, _root, _kind, body):
        if "新附件会在本轮结束后" in body["zh_cn"]["content"][0][0]["text"]:
            loop.call_soon_threadsafe(reminder.set)

    client.send.side_effect = post
    runtime.ask = AsyncMock(side_effect=ask)
    adapter = FeishuAdapter(client, "cli_test", "tenant", "ou_bot", False, runtime, InMemoryStore())

    async def send(mid, kind, body):
        payload = event(mid, kind, body)
        payload["event"]["message"]["chat_type"] = "p2p"
        await adapter.handle(payload)

    old_thread = thread_id_for("tenant", "oc_chat", "dm")
    new_thread = thread_id_for("tenant", "oc_chat", "dm_om_reset")
    document = {"file_key": "file_extra", "file_name": "补充.pdf"}
    await send("om_initial", "file", document)
    client.send.assert_not_called()
    runtime.attach.reset_mock()

    report = asyncio.create_task(send("om_report", "text", {"text": "write a report"}))
    upload = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        upload = asyncio.create_task(send("om_upload", "file", document))
        await asyncio.wait_for(reminder.wait(), timeout=2)
        client.send.assert_called_once()
        chat, root, kind, body = client.send.call_args.args
        assert (chat, root, kind) == ("oc_chat", "dm", "post")
        assert "`/new`" in body["zh_cn"]["content"][0][0]["text"]
        assert not upload.done()
        runtime.attach.assert_not_awaited()
        runtime.ask.assert_awaited_once_with("write a report", thread_id=old_thread)
        await asyncio.wait_for(send("om_upload", "file", document), timeout=2)
        await asyncio.wait_for(send("om_reset", "text", {"text": "/new"}), timeout=2)
        await asyncio.wait_for(send("om_next", "text", {"text": "a different report"}), timeout=2)
        runtime.ask.assert_awaited_with("a different report", thread_id=new_thread)
        assert runtime.ask.await_count == 2
        assert sum(
            "新附件会在本轮结束后" in call.args[3]["zh_cn"]["content"][0][0]["text"]
            for call in client.send.call_args_list
        ) == 1
    finally:
        finish.set()
        await asyncio.gather(*(task for task in (report, upload) if task is not None))
    runtime.attach.assert_awaited_once_with(
        old_thread, [("补充.pdf", b"extra material")], on_preprocess=None
    )


@pytest.mark.skipif(os.getenv("INSIGHTFUL_DOCKER_TESTS") != "1", reason="requires report sandbox")
async def test_feishu_sends_report_progress_during_writing(tmp_path):
    client = MagicMock(spec=FeishuClient)
    client.send.return_value = "om_reply"
    chat = f"oc_{uuid4().hex}"
    path = "/workspace/projects/demo/report-progress.md"
    initial = "# 写作进度\n\n第一章待起草。\n"
    updated = initial.replace("待起草", "已完成")

    def next_chapter(_):
        assert client.send_file.call_count == 1
        assert client.send_file.call_args.args == (
            chat,
            "om_first",
            "report-progress.md",
            initial.encode(),
        )
        return tool_call(
            "write_file",
            {"file_path": "/workspace/projects/demo/chapter-1.md", "content": "# Chapter"},
        )

    def checkpoint(_):
        assert client.send_file.call_count == 1
        return tool_call(
            "edit_file", {"file_path": path, "old_string": "待起草", "new_string": "已完成"}
        )

    def retry_unchanged(_):
        assert client.send_file.call_count == 2
        assert client.send_file.call_args.args == (
            chat,
            "om_first",
            "report-progress.md",
            updated.encode(),
        )
        return tool_call(
            "edit_file", {"file_path": path, "old_string": "已完成", "new_string": "已完成"}
        )

    def fail_edit(_):
        assert client.send_file.call_count == 2
        return tool_call(
            "edit_file", {"file_path": path, "old_string": "不存在", "new_string": "更新"}
        )

    store = InMemoryStore()
    adapter = FeishuAdapter(client, "cli_test", "tenant", "ou_bot", store=store)
    lead = ScriptedChatModel(
        steps=[
            lambda _: tool_call("write_file", {"file_path": path, "content": initial}),
            next_chapter,
            checkpoint,
            retry_unchanged,
            fail_edit,
            lambda _: reply("报告阶段性工作结束。"),
        ]
    )
    runtime = build_local_runtime(
        Settings(_env_file=None, workspace_root=tmp_path),
        ROOT,
        model=lead,
        store=store,
        surface="feishu",
        surface_middleware=(FeishuProgressMiddleware(adapter),),
    )
    adapter.runtime = runtime
    payload = event("om_first", "text", {"text": "撰写长报告"}, first=True)
    payload["event"]["message"]["chat_id"] = chat
    try:
        await adapter.handle(payload)
        assert client.send_file.call_count == 2
        assert lead.call_count == 6
        previews = [
            item.args[3]["zh_cn"]["content"][0][0]["text"]
            for item in client.send.call_args_list
            if item.args[2] == "post" and "报告写作进度" in str(item.args[3])
        ]
        assert len(previews) == 2
        assert "第 1 次更新" in previews[0] and "第一章待起草" in previews[0]
        assert "第 2 次更新" in previews[1] and "第一章已完成" in previews[1]
    finally:
        await runtime.aclose()
        runtime.binding.runner.stop_all()


@pytest.mark.skipif(os.getenv("INSIGHTFUL_DOCKER_TESTS") != "1", reason="requires report sandbox")
@pytest.mark.parametrize("state", ["succeeded", "partial", "failed", "prepared_zip"])
async def test_feishu_external_preprocessor_status_and_index(tmp_path, monkeypatch, state):
    failed = int(state == "partial")
    index = (
        f"# 资料索引\n\n已完成 1 个文件 · 处理失败 {failed} 个文件 · 未能接收 0 个来源\n\n"
        "- memo.md — 摘要内容 · [摘要](memo.md.brief.md) · [正文](memo.md.export.md)\n"
    )
    if failed:
        index += "- invalid.pdf — 解析失败\n"
    archive = io.BytesIO()
    with ZipFile(archive, "w") as result:
        result.writestr("wiki-index.md", index)
        result.writestr("memo.md.brief.md", "摘要内容")
        result.writestr("memo.md.export.md", "# Memo\n\nEvidence.")
    data = archive.getvalue()
    actions = []
    chat = f"oc_{uuid4().hex}"
    client = MagicMock(spec=FeishuClient)
    client.download.return_value = data if state == "prepared_zip" else b"raw source"
    client.send.side_effect = lambda chat, root, kind, body: actions.append(
        body["zh_cn"]["content"][0][0]["text"]
    )

    async def external(_settings, _trace, inputs):
        assert inputs == {"memo.md": b"raw source"}
        assert "即将把附件提交给外部预处理服务" in actions[-1]
        actions.append("external request")
        if state == "failed":
            raise PreprocessorError("test failure")
        return "result.zip", data, state

    monkeypatch.setattr("insightful_report_agent.runtime.local.preprocess", external)
    store = InMemoryStore()
    adapter = FeishuAdapter(client, "cli_test", "tenant", "ou_bot", store=store)
    lead = ScriptedChatModel(steps=[])
    runtime = build_local_runtime(
        Settings(
            _env_file=None,
            workspace_root=tmp_path,
            preprocessor_api_url="https://preprocessor.example",
            preprocessor_api_key="test-key",
        ),
        ROOT,
        model=lead,
        store=store,
        surface="feishu",
        source_intake_notify=adapter.notify_source_intake,
    )
    adapter.runtime = runtime
    thread = thread_id_for("tenant", chat, "om_first")
    filename = "result.zip" if state == "prepared_zip" else "memo.md"
    payload = event(
        "om_first", "file", {"file_key": "file_material", "file_name": filename}, first=True
    )
    payload["event"]["message"]["chat_id"] = chat
    try:
        await adapter.handle(payload)
        if state == "failed":
            assert "外部预处理未返回可用" in actions[-1]
            client.send_file.assert_not_called()
            return
        await runtime.source_intake.wait_ready(thread)
        assert lead.call_count == 0
        if state == "prepared_zip":
            assert not any("外部预处理" in action for action in actions)
        else:
            assert "外部预处理" + ("部分完成" if failed else "完成") in actions[2]
            assert "耗时" in actions[2]
        assert actions[-1] == f"预处理资料包检查完成。可用材料 1 项，失败 {failed} 项。"
        assert not any("wiki-index.md 预览" in action or "摘要内容" in action for action in actions)
        client.send_file.assert_called_once()
        assert client.send_file.call_args.args == (
            chat,
            "om_first",
            "wiki-index.md",
            index.encode(),
        )
    finally:
        await runtime.aclose()
        runtime.binding.runner.stop_all()


@pytest.mark.parametrize("kind", ["folder", "post"])
async def test_feishu_dm_downloads_folder_and_preserves_paths(kind: str) -> None:
    contents = {"file_root": b"first", "file_nested": b"second"}

    def platform(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer test-token"
        if path == "/open-apis/im/v1/files/file_folder/folder":
            assert request.url.params == httpx.QueryParams(
                {"srctype": "message", "srcid": "om_folder", "recursive": "true"}
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "all_count": 4,
                        "items": [
                            {"file_key": "file_root", "name": "资料.md", "is_folder": False},
                            {
                                "name": "子目录",
                                "is_folder": True,
                                "children_count": "1",
                                "children": [
                                    {
                                        "file_key": "file_nested",
                                        "name": "资料.md",
                                        "is_folder": False,
                                    },
                                ],
                            },
                        ],
                    },
                },
            )
        parent, key = path.rsplit("/", 1)
        if parent == "/open-apis/im/v1/messages/om_folder/resources":
            assert request.url.params["type"] == "file"
            return httpx.Response(200, content=contents[key])
        assert path in {
            "/open-apis/im/v1/messages/om_folder/reactions",
            "/open-apis/im/v1/messages",
        }
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_reply"}})

    runtime = MagicMock()
    runtime.settings = Settings(
        _env_file=None,
        preprocessor_api_url="https://preprocessor.example",
        preprocessor_api_key="test-key",
    )
    runtime.attach = AsyncMock()
    runtime.ask = AsyncMock(return_value=[AIMessage(content="已收到报告要求")])
    client = FeishuClient("cli_test", "test-secret", transport=httpx.MockTransport(platform))
    adapter = FeishuAdapter(
        client,
        "cli_test",
        "tenant",
        "ou_bot",
        enable_progress=False,
        runtime=runtime,
        store=InMemoryStore(),
    )
    folder = {"file_key": "file_folder", "file_name": "材料", "is_folder": True}
    text = "请基于这些材料写报告"
    body = (
        folder
        if kind == "folder"
        else {
            "content": [[{"tag": "text", "text": text}]],
            "files": [folder],
        }
    )
    payload = event("om_folder", kind, body)
    payload["event"]["message"]["chat_type"] = "p2p"

    try:
        await adapter.handle(payload)
        await adapter.handle(payload)
    finally:
        client.close()

    runtime.attach.assert_awaited_once()
    thread, [(name, archive)] = runtime.attach.await_args.args
    assert name == "材料.zip"
    with ZipFile(io.BytesIO(archive)) as files:
        assert {name: files.read(name) for name in files.namelist()} == {
            "材料/资料.md": b"first",
            "材料/子目录/资料.md": b"second",
        }
    if kind == "post":
        runtime.ask.assert_awaited_once_with(text, thread_id=thread)
    else:
        runtime.ask.assert_not_awaited()


async def test_feishu_dm_with_unreadable_folder_replies_with_zip_instructions() -> None:
    client = MagicMock(spec=FeishuClient)
    client.folder_zip.side_effect = FolderAttachmentError("incomplete_folder_tree")
    runtime = MagicMock()
    runtime.settings.source_input_mode = "legacy"
    adapter = FeishuAdapter(
        client,
        "cli_test",
        "tenant",
        "ou_bot",
        enable_progress=False,
        runtime=runtime,
        store=InMemoryStore(),
    )
    payload = event(
        "om_folder",
        "post",
        {
            "content": [[{"tag": "text", "text": "帮我写一份报告"}]],
            "files": [{"file_key": "file_folder", "file_name": "材料", "is_folder": True}],
        },
    )
    payload["event"]["message"]["chat_type"] = "p2p"

    await adapter.handle(payload)
    await adapter.handle(payload)

    client.send.assert_called_once()
    chat, root, kind, body = client.send.call_args.args
    assert (chat, root, kind) == ("oc_chat", "dm", "post")
    assert "ZIP" in body["zh_cn"]["content"][0][0]["text"]
    client.download.assert_not_called()
    runtime.attach.assert_not_called()
    runtime.ask.assert_not_called()


@pytest.mark.parametrize(
    ("name", "count", "error"),
    [("..", 2, "invalid_folder_entry_name"), ("notes.md", 3, "incomplete_folder_tree")],
)
def test_feishu_folder_rejects_unsafe_or_incomplete_listing(name, count, error) -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "test-token", "expire": 7200}
            )
        assert request.url.path.endswith("/file_folder/folder")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "all_count": count,
                    "items": [{"file_key": "file_one", "name": name, "is_folder": False}],
                },
            },
        )

    client = FeishuClient("cli_test", "test-secret", transport=httpx.MockTransport(platform))
    try:
        with pytest.raises(FolderAttachmentError, match=error):
            client.folder_zip("om_folder", "file_folder", "材料")
    finally:
        client.close()


@pytest.mark.skipif(
    os.getenv("INSIGHTFUL_DOCKER_TESTS") != "1", reason="needs the report Docker image"
)
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
            "已核对 2 份材料的资料总览",
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
