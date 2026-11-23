"""Offline report and A2A contracts through the shared Deep Agents graph and Docker."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image, ImageDraw

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.local import build_local_runtime, final_text
from insightful_report_agent.surfaces.api.app import create_app
from tests.scripted_model import RespondingChatModel, ScriptedChatModel, reply, tool_call

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.getenv("INSIGHTFUL_DOCKER_TESTS") != "1", reason="needs the report Docker image"
)


def synthetic_inputs() -> list[tuple[str, bytes]]:
    image = Image.new("RGB", (240, 120), "#eef2fa")
    ImageDraw.Draw(image).text((15, 20), "Q4 sales 42", fill="#111111", font_size=19)
    data = io.BytesIO()
    image.save(data, "JPEG")
    packed = io.BytesIO()
    with ZipFile(packed, "w") as archive:
        archive.writestr("nested/数据.txt", "2026 Q4 volume was 42 units.")
        archive.writestr("image.jpg", data.getvalue())
    corrupt = io.BytesIO()
    with ZipFile(corrupt, "w") as archive:
        archive.writestr("../unsafe.txt", "blocked")
    return [
        ("材料.zip", packed.getvalue()),
        ("unsafe.zip", corrupt.getvalue()),
        *[(f"source-{i}.md", f"# Memo {i}\nQ4 actual 42 units.".encode()) for i in range(15)],
    ]


def tool_data(messages, name):
    for item in reversed(messages):
        if isinstance(item, ToolMessage) and item.name == name:
            assert item.status != "error", str(item.content)
            return json.loads(item.content)
    raise AssertionError(f"Missing tool result {name}")


async def test_preprocessed_archive_to_html_without_provider_calls(tmp_path: Path) -> None:
    name = "资料/产品 A.pdf"
    asset_root = f"_preprocessor/assets/{hashlib.sha256(name.encode()).hexdigest()}"
    archive = io.BytesIO()
    figure = io.BytesIO()
    Image.new("RGB", (24, 24), "blue").save(figure, "JPEG")
    with ZipFile(archive, "w") as output:
        output.writestr(
            "wiki-index.md",
            "# 资料索引\n\n已完成 1 个文件 · 处理失败 1 个文件 · 未能接收 1 个来源\n\n"
            "- [资料（1–2）](_preprocessor/index/1.md)\n\n"
            "## 未能接收的来源\n\n- original.zip — 无法解包\n",
        )
        output.writestr(
            "_preprocessor/index/1.md",
            f"# 资料\n\n- {name} — Reported Q4 results. · "
            "[摘要](../../资料/产品%20A.pdf.brief.md) · "
            "[正文](../../资料/产品%20A.pdf.export.md)\n"
            "- 资料/另一个.pdf — 摘要失败 · request timeout\n",
        )
        output.writestr(f"{name}.brief.md", "# Supplied brief\n\nSales reached 42 units.")
        output.writestr(
            f"{name}.export.md",
            f"# Q4\n\nSales reached 42 units.\n\n![Source chart](../{asset_root}/chart.jpg)\n",
        )
        output.writestr(f"{asset_root}/chart.jpg", figure.getvalue())

    def coverage(messages):
        batch = tool_data(messages, "prepare_source_overview")
        assert batch["total"] == 3
        assert len(batch["failed"]) == 2
        assert batch["sources"][0]["summary"] == "Reported Q4 results."
        return tool_call("synthesize_source_overview", {})

    state = {}

    def source(messages):
        assert tool_data(messages, "synthesize_source_overview")["reviewed"] == 1
        state["id"] = tool_data(messages, "prepare_source_overview")["sources"][0]["id"]
        return tool_call("read_source_file_abstract", {"file_path": state["id"], "level": "high"})

    def evidence(messages):
        brief = tool_data(messages, "read_source_file_abstract")
        assert not brief["generated"]
        assert "Supplied brief" in brief["text"]
        return tool_call("read_source_evidence", {"file_path": state["id"]})

    def write(messages):
        result = tool_data(messages, "read_source_evidence")
        assert "Export Markdown lines" in result["blocks"][0]["locator"]
        assert result["assets"][0]["locator"].startswith("Export Markdown line 5")
        source_id = state["id"]
        draft = (
            "# Findings\n\nSales reached 42 units in the supplied observation period. "
            "The material covers one quarter and provides insufficient evidence for a trend. "
            f"[[{source_id}#b0001]]\n\n![Original chart](source:{source_id}/a0001)"
        )
        return tool_call(
            "write_file", {"file_path": "/workspace/projects/report.md", "content": draft}
        )

    def delivered(messages):
        result = tool_data(messages, "publish_report")
        assert result["source_figures"] == 1
        return reply(result["delivered"])

    lead = ScriptedChatModel(
        steps=[
            lambda _: tool_call("prepare_source_overview", {"cursor": 0}),
            coverage,
            source,
            evidence,
            write,
            lambda _: tool_call("publish_report", {"draft_path": "/workspace/projects/report.md"}),
            delivered,
        ]
    )

    def reject_model(_):
        pytest.fail("Intake and evidence indexing must use the supplied artifacts")

    provider = RespondingChatModel(respond=reject_model)
    settings = Settings(
        _env_file=None,
        workspace_root=tmp_path,
        mineru_api_token="legacy-token",
        exa_api_key="legacy-search-key",
        abstract_model="unused:legacy-provider",
    )
    runtime = build_local_runtime(
        settings, ROOT, model=lead, role_models={"abstract": provider, "source_vision": provider}
    )
    thread = "cli-preprocessed-" + uuid4().hex[:8]
    try:
        assert runtime.components.source_library.mineru is None
        assert not {"search_web", "add_source_urls"} & {
            tool.name for tool in runtime.components.tools
        }
        await runtime.attach(thread, [("result.zip", archive.getvalue())])
        messages = await runtime.ask("Write a sourced report on sales.", thread_id=thread)
        html = (runtime.out_dir / thread / final_text(messages)).read_text()
        assert "1/3 reviewed" in html
        assert "Export SHA-256" in html and "ZIP SHA-256" in html
        assert "Original workspace path" not in html
        assert "data:image/jpeg;base64," in html
    finally:
        await runtime.aclose()
        runtime.binding.runner.stop_all()


async def test_large_intake_before_publish_and_offline_report(tmp_path: Path) -> None:
    found = {}

    def collect(messages):
        listing = tool_data(messages, "list_sources")
        assert listing["total"] == 18
        found["first_page"] = listing["sources"]
        return tool_call("prepare_source_overview", {"cursor": 0})

    def next_batch(messages):
        batch = tool_data(messages, "prepare_source_overview")
        assert batch["processed"] == 16
        return tool_call("prepare_source_overview", {"cursor": batch["next_cursor"]})

    def integrate(messages):
        assert tool_data(messages, "prepare_source_overview")["next_cursor"] is None
        return tool_call("synthesize_source_overview", {})

    def later_list(messages):
        summary = tool_data(messages, "synthesize_source_overview")
        assert summary["reviewed"] == 17 and summary["failed"] == 1
        return tool_call("list_sources", {"cursor": 16})

    def inspect_image(messages):
        items = found["first_page"] + tool_data(messages, "list_sources")["sources"]
        source = next(item for item in items if item["name"].endswith("image.jpg"))
        found["source"] = source["id"]
        return tool_call("read_source_evidence", {"file_path": source["id"], "locator": "Image"})

    def write(messages):
        evidence = tool_data(messages, "read_source_evidence")
        assert evidence["blocks"][0]["locator"] == "Image"
        asset = evidence["assets"][0]["id"]
        source = found["source"]
        citation = f"[[{source}#b0001]]"
        chart = json.dumps(
            {
                "title": "Q4 sales",
                "unit": "units",
                "data": [{"label": "Q4", "value": 42, "evidence": citation}],
            }
        )
        draft = (
            f"# Findings\n\nSales reached 42 units {citation}. "
            "The source provides a single period, so this figure describes volume rather than a trend.\n\n"
        )
        draft += f"![Source visual](source:{source}/{asset})\n\n```chart\n{chart}\n```\n"
        return tool_call(
            "write_file", {"file_path": "/workspace/projects/report.md", "content": draft}
        )

    def delivered(messages):
        result = tool_data(messages, "publish_report")
        assert result["citations"] == 1 and result["visuals"] == 2
        return reply(result["delivered"])

    lead = ScriptedChatModel(
        steps=[
            lambda _: tool_call("list_sources", {}),
            collect,
            next_batch,
            integrate,
            later_list,
            inspect_image,
            write,
            lambda _: tool_call("publish_report", {"draft_path": "/workspace/projects/report.md"}),
            delivered,
        ]
    )
    abstract = RespondingChatModel(
        respond=lambda _: AIMessage(content="Synthetic scope: Q4 sales were 42 units.")
    )
    settings = Settings(
        _env_file=None,
        source_input_mode="legacy",
        workspace_root=tmp_path / "out",
        source_visual_analysis=False,
    )
    runtime = build_local_runtime(settings, ROOT, model=lead, role_models={"abstract": abstract})
    thread = "cli-test-" + uuid4().hex[:8]
    try:
        await runtime.attach(thread, synthetic_inputs())
        messages = await runtime.ask("Create a fully sourced report.", thread_id=thread)
        file = runtime.out_dir / thread / final_text(messages)
        contents = file.read_text()
        assert "17/18 reviewed" in contents
        assert "data:image/jpeg;base64," in contents and "<svg " in contents
        assert "ZIP SHA-256" in contents and "unsafe.zip" in contents
        assert f'id="{found["source"]}-b0001"' in contents
    finally:
        await runtime.aclose()
        runtime.binding.runner.stop_all()


def rpc(api: TestClient, headers: dict, parts: list[dict], context: str | None = None) -> dict:
    message = {"messageId": uuid4().hex, "role": "ROLE_USER", "parts": parts}
    if context:
        message["contextId"] = context
    result = api.post(
        "/a2a",
        headers={**headers, "A2A-Version": "1.0"},
        json={
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": "SendMessage",
            "params": {"message": message},
        },
    )
    assert result.status_code == 200, result.text
    return result.json()["result"]["task"]


def test_a2a_upload_and_caller_isolation(tmp_path: Path) -> None:
    key_a, key_b = "token-alice-test-123", "token-bob-test-456"
    settings = Settings(
        _env_file=None,
        source_input_mode="legacy",
        workspace_root=tmp_path,
        source_visual_analysis=False,
        access_token_callers=f"{key_a}:alice,{key_b}:bob",
        a2a_url="http://testserver",
    )

    def draft(messages):
        sources = tool_data(messages, "list_sources")
        assert sources["total"] == 2
        source = sources["sources"][0]["id"]
        return tool_call(
            "write_file",
            {
                "file_path": "/workspace/projects/api-report.md",
                "content": (
                    f"# A2A report\n\nThe registered source contains a short note [[{source}#b0001]]. "
                    "The available excerpt gives no supporting measurements or dates for a comparison."
                ),
            },
        )

    lead = ScriptedChatModel(
        steps=[
            lambda _: tool_call("prepare_source_overview", {"cursor": 0}),
            lambda _: tool_call("synthesize_source_overview", {}),
            lambda _: tool_call("list_sources", {}),
            draft,
            lambda _: tool_call(
                "publish_report", {"draft_path": "/workspace/projects/api-report.md"}
            ),
            lambda messages: reply(tool_data(messages, "publish_report")["delivered"]),
        ]
    )
    abstract = RespondingChatModel(respond=lambda _: AIMessage(content="Source contents."))

    def build(settings, root, **kwargs):
        return build_local_runtime(
            settings, root, model=lead, role_models={"abstract": abstract}, **kwargs
        )

    a, b = {"Authorization": f"Bearer {key_a}"}, {"Authorization": f"Bearer {key_b}"}
    with TestClient(create_app(ROOT, settings, build=build)) as api:
        card = api.get("/.well-known/agent-card.json").json()
        assert card["skills"][0]["id"] == "evidence-report" and card["securityRequirements"]
        first = rpc(
            api, a, [{"raw": base64.b64encode(b"first source").decode(), "filename": "中文.txt"}]
        )
        assert first["status"]["state"] == "TASK_STATE_COMPLETED"
        ctx = first["contextId"]
        url = f"/a2a/contexts/{ctx}/attachments"
        batch = [("files", ("second.txt", b"second", "text/plain"))]
        assert api.post(url, headers=b, files=batch).status_code == 404
        allowed = api.post(url, headers=a, files=batch)
        assert allowed.status_code == 200, allowed.text
        second = rpc(
            api, a, [{"text": "Consider both sources and deliver an HTML report."}], context=ctx
        )
        assert second["status"]["state"] == "TASK_STATE_COMPLETED"
        artifact = next(p for item in second["artifacts"] for p in item["parts"] if "url" in p)
        assert artifact["mediaType"] == "text/html"
        report = api.get(artifact["url"], headers=a)
        assert report.status_code == 200 and "2/2 reviewed" in report.text
        assert api.get(artifact["url"], headers=b).status_code == 404
