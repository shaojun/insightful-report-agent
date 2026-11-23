"""Unit contracts for compression, stable evidence indexes and cache invalidation."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.profiles import RuntimeProfile
from insightful_report_agent.tools import sources
from insightful_report_agent.tools.abstract_prompts import PROMPTS_FILE
from insightful_report_agent.tools.sources import (
    ABSTRACT_CAPS,
    SourceLibrary,
    fingerprint,
    format_abstract,
    page,
)
from tests.scripted_model import RespondingChatModel

ROOT = Path(__file__).resolve().parents[1]


class MemoryBackend(dict):
    async def aexecute(self, command, *, timeout):
        command, option, path = shlex.split(command)
        assert command == "test" and option == "-s"
        return SimpleNamespace(exit_code=0 if self.get(path) else 1)


class MemoryLibrary(SourceLibrary):
    document_reads = 0

    async def optional(self, backend, path):
        return backend.get(path)

    async def put(self, backend, path, value):
        backend[path] = value

    async def save(self, backend, path, data):
        backend[path] = data

    async def document(self, backend, record):
        self.document_reads += 1
        return record

    prepared_document = document


def fixture() -> dict:
    return {
        "id": "s-017894abcdeffedc",
        "name": "Quarter.md",
        "path": "/workspace/attachments/Quarter.md",
        "bundle": "/workspace/source-library/revision-1",
        "blocks": [
            {
                "id": f"b{i:04d}",
                "locator": f"Original line {i}",
                "text": f"Fact {i}\n" + "detail " * 440,
            }
            for i in range(1, 15)
        ],
        "assets": [],
        "audio": [],
        "warnings": ["One missing chart"],
    }


async def test_each_level_reads_all_blocks_and_caches_by_policy(
    tmp_path: Path, monkeypatch
) -> None:
    seen = []

    def respond(messages):
        seen.append([message.text for message in messages])
        return AIMessage(content="Extracted facts. [[s-017894abcdeffedc#b0001]]")

    model = RespondingChatModel(respond=respond)
    config = tmp_path / PROMPTS_FILE
    config.parent.mkdir()
    prompts = json.loads((ROOT / PROMPTS_FILE).read_text(encoding="utf-8"))
    config.write_text(json.dumps(prompts), encoding="utf-8")
    profile = RuntimeProfile("local", tmp_path, lambda _: None, lambda _: None)
    library = MemoryLibrary(
        Settings(_env_file=None, source_input_mode="legacy", source_visual_analysis=False), profile, model, {}
    )
    storage = MemoryBackend()
    record = fixture()
    for level in ("high", "middle", "low"):
        before = len(seen)
        result = await library.abstract_for(storage, record, level)
        assert result["path"].endswith(f"Quarter.md.abstract.{level}")
        assert len(seen) - before == 4  # 3 maps plus reduce
        assert "Fact 14" in seen[-2][1]
        markdown = storage[result["path"]].decode()
        assert ("[[s-017894abcdeffedc#b0014]] · Original line 14" in markdown) == (level == "low")
        assert "Coverage notes" in markdown
        assert ("Section summaries" in markdown) == (level == "low")
        assert ("wherever possible" in seen[-1][0]) == (level == "low")
        assert await library.abstract_for(storage, record, level) == result
        assert len(seen) - before == 4
    assert library.document_reads == 3
    middle = await library.abstract_for(storage, record, "middle")
    del storage[middle["path"]]  # A stale JSON cache index must not hide a deleted abstract.
    before = model.call_count
    assert await library.abstract_for(storage, record, "middle") == middle
    assert model.call_count == before + 4
    prompts["high"] = "Custom high instruction."
    config.write_text(json.dumps(prompts), encoding="utf-8")
    separate_model = RespondingChatModel(respond=respond)
    monkeypatch.setattr(sources, "build_chat_model", lambda *_args, **_kwargs: separate_model)
    overridden = MemoryLibrary(
        Settings(
            _env_file=None,
            source_input_mode="legacy", source_visual_analysis=False, abstract_model="openai:deepseek-flash"
        ),
        profile,
        model,
        {},
    )
    modified = await overridden.abstract_for(storage, record, "high")
    assert separate_model.call_count == 4
    assert "Custom high instruction." in seen[-1][0]
    assert modified["path"] != result["path"]
    changed = dict(record, bundle="/workspace/source-library/revision-2")
    assert (await overridden.abstract_for(storage, changed, "high"))["path"] != modified["path"]


async def test_mineru_short_spans_share_one_model_call() -> None:
    seen = []

    def respond(messages):
        seen.append(messages[-1].text)
        return AIMessage(content="Summary of the slides.")

    model = RespondingChatModel(respond=respond)
    profile = RuntimeProfile("local", ROOT, lambda _: None, lambda _: None)
    library = MemoryLibrary(
        Settings(_env_file=None, source_input_mode="legacy", source_visual_analysis=False), profile, model, {}
    )
    record = fixture()
    record["blocks"] = [
        {"id": f"b{i:04d}", "locator": f"Page {i // 27 + 1}", "text": f"Slide fragment {i}"}
        for i in range(1, 266)
    ]
    storage = MemoryBackend()

    result = await library.abstract_for(storage, record, "high")

    assert model.call_count == 1
    markdown = storage[result["path"]].decode()
    assert result["synopsis"] == "Summary of the slides."
    assert "Coverage: 265 evidence blocks" in markdown
    assert "Evidence index" not in markdown
    for block in record["blocks"]:
        ref = f"[[{record['id']}#{block['id']}]] · {block['locator']}"
        assert ref in seen[0]


@pytest.mark.parametrize("level", ["high", "middle"])
async def test_compact_abstract_drops_id_overhead_and_replaces_old_layout(level: str) -> None:
    record = fixture()
    ref = f"{record['id']}#b0014"
    text = "锁定组件负责切换状态（权利要求 3）。"
    model = RespondingChatModel(
        respond=lambda _: AIMessage(content=f"[[{ref}]]" * 60 + ref + text + "补充内容" * 500)
    )
    profile = RuntimeProfile("local", ROOT, lambda _: None, lambda _: None)
    library = MemoryLibrary(
        Settings(_env_file=None, source_input_mode="legacy", source_visual_analysis=False), profile, model, {}
    )
    record["assets"] = [{"id": "a0001", "locator": "Page 1", "file": "/workspace/image.jpg"}]
    old_layout = ["compact-v1"] if level == "high" else []
    old_cache = fingerprint([library.common_key, library.prompts[level], *old_layout])[:20]
    old_path = f"{record['bundle']}/abstracts/{old_cache}/Quarter.md.abstract.{level}"
    storage = MemoryBackend({old_path: b"Old abstract", old_path + ".json": {"path": old_path}})

    result = await library.abstract_for(storage, record, level)

    assert result["path"] != old_path
    assert storage[old_path] == b"Old abstract"
    assert result["synopsis"].startswith(text)
    assert len(result["synopsis"]) == ABSTRACT_CAPS[level]
    markdown = storage[result["path"]].decode()
    assert text in markdown
    assert ref not in markdown
    assert "Evidence index" not in markdown
    assert "Visual index" not in markdown
    assert "Section summaries" not in markdown
    assert "14 evidence blocks and 1 visual items" in markdown


def test_low_abstract_page_preserves_complete_wiki_index() -> None:
    doc = fixture()
    doc["blocks"] *= 50
    full = format_abstract(doc, "low", "Synopsis", [])
    offset = 0
    copies = []
    while offset is not None:
        part = page(full, offset)
        copies.append(part["text"])
        offset = part["next_offset"]
    assert len(copies) > 1
    assert "".join(copies) == full
