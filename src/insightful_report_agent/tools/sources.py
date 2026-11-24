"""Evidence tools shared by Feishu, Slack, CLI and A2A."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import Field, HttpUrl, ValidationError

from insightful_report_agent.config import Settings
from insightful_report_agent.models.factory import build_chat_model, named_key, resolve_model
from insightful_report_agent.runtime.preprocessor import PreprocessorError, preprocess
from insightful_report_agent.runtime.profiles import RuntimeProfile
from insightful_report_agent.tools.abstract_prompts import (
    BOUNDARY,
    COMPACT_BOUNDARY,
    CORPUS_PROMPT,
    VISUAL_PROMPT,
    Level,
    load_abstract_prompts,
)
from insightful_report_agent.tools.delivery import DeliveredFile, pending_reports
from insightful_report_agent.tools.fetch import FetchRefused
from insightful_report_agent.tools.files import (
    WorkspaceError,
    read_bytes,
    require_backend,
    workspace_path,
    write_bytes,
)
from insightful_report_agent.tools.media import _groq_transcript
from insightful_report_agent.tools.mineru import MAX_INPUT, SUPPORTED, MinerU, MinerUError
from insightful_report_agent.tools.source_web import fetch_source

LIBRARY = "/workspace/source-library"
INVENTORY = f"{LIBRARY}/inventory.json"
CONVERTER = "/workspace/tools/source_normalize.py"
RENDERER = "/workspace/tools/source_report.py"
BATCH_SIZE = 16
CALL_SIZE = 6
ABSTRACT_INPUT_CHARS = 18_000
COMPACT_ABSTRACT_LAYOUT = "prose-v1"
ABSTRACT_CAPS: dict[Level, int] = {"high": 450, "middle": 1500, "low": 4400}
VISUAL_LIMIT = 12
ABSTRACT_PAGE = 6000
SOURCE_ID = re.compile(r"s-[0-9a-f]{16}")
BLOCK_ID = re.compile(r"b\d{4,}")
VISUAL_ID = re.compile(r"a\d{4,}")
CITATION = re.compile(r"\[\[(s-[0-9a-f]{16})#(b\d{4,})\]\]")
ABSTRACT_REFERENCE = re.compile(rf"{CITATION.pattern}|{SOURCE_ID.pattern}#{BLOCK_ID.pattern}")
URL_SUFFIX = ".insightful-url"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"


def url_files(urls: Sequence[str]) -> list[tuple[str, bytes]]:
    return [(hashlib.sha256(url.encode()).hexdigest() + URL_SUFFIX, url.encode()) for url in urls]


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def grouped(values: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def abstract_inputs(doc: dict) -> list[str]:
    """Pack consecutive evidence by text length while retaining each original locator."""
    batches = []
    lines = []
    length = 0
    for block in doc["blocks"]:
        entry = f"[[{doc['id']}#{block['id']}]] · {block['locator']}\n{block['text']}"
        separator = 2 if lines else 0
        if lines and length + separator + len(entry) > ABSTRACT_INPUT_CHARS:
            batches.append("\n\n".join(lines))
            lines = []
            length = 0
            separator = 0
        lines.append(entry)
        length += separator + len(entry)
    if lines:
        batches.append("\n\n".join(lines))
    return batches


def page(text: str, offset: int) -> dict:
    end = min(len(text), offset + ABSTRACT_PAGE)
    if end < len(text):
        split = text.rfind("\n", offset + ABSTRACT_PAGE // 2, end)
        if split != -1:
            end = split + 1
    return {"text": text[offset:end], "next_offset": end if end < len(text) else None}


def format_abstract(doc: dict, level: Level, synopsis: str, parts: list[str]) -> str:
    if level != "low":
        name = " ".join(doc["name"].split())[:160]
        lines = [
            f"# {name} · {level}",
            f"Source ID: `{doc['id']}`",
            "## Abstract",
            synopsis,
            f"Coverage: {len(doc['blocks'])} evidence blocks and {len(doc['assets'])} visual items.",
        ]
        if doc["warnings"]:
            notes = " ".join(" ".join(doc["warnings"]).split())
            lines.append("Coverage notes: " + (notes[:197] + "…" if len(notes) > 200 else notes))
        return "\n\n".join(lines) + "\n"
    lines = [
        f"# {doc['name']} · {level}",
        f"Source: {doc['path']}",
        f"Normalized Markdown: {doc['bundle']}/source.md",
        "## Abstract",
        synopsis,
    ]
    if parts:
        lines.extend(["## Section summaries", *parts])
    if doc["warnings"]:
        lines.extend(["## Coverage notes", *[f"- {item}" for item in doc["warnings"]]])
    lines.append("## Evidence index")
    for block in doc["blocks"]:
        first_line = " ".join(block["text"].split())[:92]
        lines.append(f"- [[{doc['id']}#{block['id']}]] · {block['locator']} · {first_line}")
    if doc["assets"]:
        lines.append("## Visual index")
        for asset in doc["assets"]:
            lines.append(f"- {doc['id']}/{asset['id']} · {asset['locator']} · {asset['file']}")
    return "\n\n".join(lines) + "\n"


class SourceLibrary:
    def __init__(
        self,
        settings: Settings,
        profile: RuntimeProfile,
        lead: BaseChatModel,
        models: dict[str, BaseChatModel],
    ) -> None:
        self.settings = settings
        self.legacy = settings.source_input_mode == "legacy"
        self.search_key = (
            named_key("DEEPSEEK_API_KEY", profile.project_root)
            if settings.enable_web_search
            else None
        )
        self.exa_search = (
            settings.exa_api_key if self.legacy and settings.enable_web_search else None
        )
        self.preprocess_urls = bool(
            not self.legacy and settings.preprocessor_api_url and settings.preprocessor_api_key
        )
        self.mineru = (
            MinerU(settings.mineru_api_token.get_secret_value(), wait=settings.mineru_wait_seconds)
            if self.legacy and settings.mineru_api_token
            else None
        )
        self.profile = profile
        self.prompts = load_abstract_prompts(profile.project_root) if self.legacy else {}
        if self.legacy and settings.abstract_model:
            config = settings.model_copy(
                update={
                    "model": settings.abstract_model,
                    "model_base_url": settings.abstract_model_base_url,
                    "model_api_key_env": settings.abstract_model_api_key_env,
                    "model_effort": settings.abstract_model_effort,
                }
            )
        else:
            config = settings
        self.abstract = models.get("abstract") or (
            build_chat_model(config, project_root=profile.project_root)
            if self.legacy and settings.abstract_model
            else lead
        )
        self.vision = models.get("source_vision")
        vision_spec = None
        if settings.source_visual_analysis and self.vision is None:
            try:
                if resolve_model(settings, "lead").caps.vision:
                    self.vision = lead
                    vision_spec = settings.model
            except ValueError:
                pass
        self.common_key = {
            "model": config.model,
            "endpoint": config.model_base_url,
            "effort": config.model_effort,
            "vision": vision_spec or (type(self.vision).__name__ if self.vision else None),
            "visual_limit": VISUAL_LIMIT,
            "vision_prompt": VISUAL_PROMPT if self.vision else None,
            "parser": settings.mineru_model if self.mineru else "local",
            "transcription": "groq:whisper-large-v3-turbo" if settings.groq_api_key else None,
            "boundary": BOUNDARY,
            "call_size": CALL_SIZE,
        }
        self.limiter = asyncio.Semaphore(settings.source_parallelism)
        self._locks: dict[str, asyncio.Lock] = {}

    def backend(self, runtime: ToolRuntime) -> Any:
        return require_backend(self.profile.backend_for, runtime)

    async def get(self, backend: Any, path: str, cap: int = 64) -> Any:
        return json.loads(await read_bytes(backend, path, cap_mb=cap))

    async def optional(self, backend: Any, path: str) -> Any:
        try:
            return await self.get(backend, path)
        except (WorkspaceError, ValueError):
            return None

    async def put(self, backend: Any, path: str, value: Any) -> None:
        await self.save(backend, path, json_bytes(value))

    async def save(self, backend: Any, path: str, data: bytes) -> None:
        tmp = await write_bytes(backend, f"{path}.{uuid4().hex}.tmp", data)
        result = await backend.aexecute(f"mv -- {shlex.quote(tmp)} {shlex.quote(path)}", timeout=20)
        if result.exit_code:
            raise ToolException("Source cache could not be committed")

    async def run(self, backend: Any, script: str, action: str, payload: dict | None = None) -> str:
        argument = ""
        cleanup = ""
        if payload is not None:
            path = f"{LIBRARY}/requests/{uuid4().hex}.json"
            await write_bytes(backend, path, json_bytes(payload))
            argument = " " + shlex.quote(path)
            cleanup = f"trap 'rm -f -- {path}' EXIT; "
        result = await backend.aexecute(
            f"{cleanup}python3 {script} {action}{argument}", timeout=1500
        )
        output = (result.output or "").strip()
        if result.exit_code != 0:
            try:
                message = json.loads(output.splitlines()[-1])["error"]
            except (IndexError, KeyError, ValueError):
                message = "Sandbox source operation failed"
            raise ToolException(message)
        return output

    async def records(self, backend: Any) -> list[dict]:
        await self.run(backend, CONVERTER, "inventory" if self.legacy else "inventory-preprocessed")
        rows = await self.get(backend, INVENTORY)
        if self.mineru:
            for row in rows:
                if PurePosixPath(row["path"]).suffix.lower() in SUPPORTED | {URL_SUFFIX}:
                    row["bundle"] += f"-mineru-{self.settings.mineru_model}"
        return rows

    def snapshot(self, records: list[dict]) -> str:
        if not self.legacy:
            return fingerprint(["preprocessed", records])[:24]
        return fingerprint(
            [
                records,
                CORPUS_PROMPT,
                BATCH_SIZE,
                self.abstract_policy("high"),
            ]
        )[:24]

    def overview_root(self, records: list[dict]) -> str:
        return f"{LIBRARY}/overviews/{self.snapshot(records)}"

    async def summarize(
        self,
        system: str,
        content: Any,
        *,
        cap: int,
        model: BaseChatModel | None = None,
        omit_citations: bool = False,
    ) -> str:
        result = await (model or self.abstract).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=content)]
        )
        text = result.text.strip()
        if omit_citations:
            text = ABSTRACT_REFERENCE.sub("", text).strip()
        if not text:
            raise ToolException("The evidence model returned an empty response")
        if len(text) > cap:
            end = cap - 1  # Reserve space for the ellipsis.
            cut = max(text.rfind("\n", cap // 2, end), text.rfind("。", cap // 2, end))
            return text[: cut + 1 if cut >= 0 else end].rstrip() + "…"
        return text

    async def reduce(
        self, rows: list[str], system: str, *, cap: int, omit_citations: bool = False
    ) -> str:
        if not rows:
            raise ToolException("No source data to summarize")
        while len(rows) > CALL_SIZE:
            rows = [
                await self.summarize(
                    system, "\n\n".join(chunk), cap=cap, omit_citations=omit_citations
                )
                for chunk in grouped(rows, CALL_SIZE)
            ]
        if len(rows) == 1:
            return rows[0]
        return await self.summarize(
            system, "\n\n".join(rows), cap=cap, omit_citations=omit_citations
        )

    async def web_snapshot(self, backend: Any, record: dict) -> dict:
        location = f"{record['bundle']}/web.json"
        if saved := await self.optional(backend, location):
            return saved
        url = (await read_bytes(backend, record["path"], cap_mb=1)).decode().strip()
        try:
            data, suffix, resolved = await fetch_source(url)
        except (httpx.HTTPError, FetchRefused, ValueError, OSError) as error:
            raise ToolException(f"Public source download failed ({type(error).__name__})") from None
        path = await write_bytes(backend, f"{record['bundle']}/web-input{suffix}", data)
        images: dict[str, str] = {}
        if suffix == ".html":
            from bs4 import BeautifulSoup

            html = BeautifulSoup(data, "html.parser")
            section = html.find("main") or html.find("article") or html.body or html
            for raw in dict.fromkeys(
                str(tag.get("src") or "") for tag in section.find_all("img")[:12]
            ):
                if not raw:
                    continue
                try:
                    image, extension, _ = await fetch_source(
                        urljoin(resolved, raw), max_bytes=2 * 1024**2
                    )
                except (httpx.HTTPError, FetchRefused, ValueError, OSError):
                    continue
                if extension in {".jpg", ".png", ".webp", ".gif"}:
                    images[raw] = await write_bytes(
                        backend, f"{record['bundle']}/web-image-{len(images)}{extension}", image
                    )
        saved = {"url": url, "resolved_url": resolved, "path": path, "images": images}
        await self.put(backend, location, saved)
        return saved

    async def document(self, backend: Any, record: dict) -> dict:
        if record.get("error"):
            raise ToolException(record["error"])
        location = f"{record['bundle']}/document.json"
        doc = await self.optional(backend, location)
        if doc is None:
            web = (
                await self.web_snapshot(backend, record)
                if record["path"].endswith(URL_SUFFIX)
                else None
            )
            parser_input = web["path"] if web else record["path"]
            suffix = PurePosixPath(parser_input).suffix.lower()
            mineru_zip = (
                await self.mineru_archive(backend, record, parser_input, suffix)
                if self.mineru and suffix in SUPPORTED
                else None
            )
            await self.run(
                backend,
                CONVERTER,
                "normalize",
                {"record": record, "web": web, "mineru": mineru_zip},
            )
            doc = await self.get(backend, location)
        return doc

    async def mineru_archive(self, backend: Any, record: dict, path: str, suffix: str) -> str:
        mineru = self.mineru
        if mineru is None:
            raise ToolException("MinerU credentials are unavailable")
        archive = f"{record['bundle']}/mineru-result.zip"
        result = await backend.aexecute(f"test -s {shlex.quote(archive)}", timeout=5)
        if result.exit_code == 0:
            return archive
        ticket = f"{record['bundle']}/mineru-ticket.json"
        try:
            job = await self.optional(backend, ticket)
            if job is None:
                if not record["path"].endswith(URL_SUFFIX) and record["bytes"] > MAX_INPUT:
                    raise MinerUError("Source exceeds MinerU's 200 MB limit")
                source = await read_bytes(backend, path, cap_mb=200)
                if not source or len(source) > MAX_INPUT:
                    raise MinerUError("Source exceeds MinerU's permitted size")
                filename = record["id"] + suffix
                model = "MinerU-HTML" if suffix in {".html", ".htm"} else self.settings.mineru_model
                batch_id, signed = await mineru.submit(filename, record["id"], model)
                await mineru.upload(signed, source)
                del source
                job = {"batch_id": batch_id}
                await self.put(backend, ticket, job)
            result = await mineru.result(job["batch_id"], record["id"])
            await self.save(backend, archive, result)
            return archive
        except MinerUError as error:
            raise ToolException(str(error)) from None

    async def enriched(self, backend: Any, doc: dict) -> dict:
        if doc.get("preprocessed"):
            return doc
        if not doc["assets"] and not doc["audio"]:
            return doc
        location = f"{doc['bundle']}/analysis-{fingerprint(self.common_key)[:20]}.json"
        if saved := await self.optional(backend, location):
            return saved
        blocks = list(doc["blocks"])
        warnings = list(doc["warnings"])
        assets = doc["assets"]
        if assets and self.vision is None:
            warnings.append(
                "Visual interpretation is disabled or the lead model lacks vision. Available OCR and previews are retained."
            )
        elif assets:
            selected = [
                assets[i * len(assets) // min(len(assets), VISUAL_LIMIT)]
                for i in range(min(len(assets), VISUAL_LIMIT))
            ]
            if len(selected) < len(assets):
                warnings.append(
                    f"Automated visual interpretation covers {len(selected)}/{len(assets)} previews. Remaining previews are indexed."
                )
            for batch in grouped(selected, 3):
                parts = []
                for item in batch:
                    image = await read_bytes(backend, item["file"], cap_mb=5)
                    parts.extend(
                        [
                            {"type": "text", "text": f"{item['id']} · {item['locator']}"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64,"
                                    + base64.b64encode(image).decode()
                                },
                            },
                        ]
                    )
                description = await self.summarize(
                    BOUNDARY + "\n" + VISUAL_PROMPT, parts, cap=4500, model=self.vision
                )
                blocks.append(
                    {
                        "id": f"b{len(blocks) + 1:04d}",
                        "locator": "Model visual observation of "
                        + ", ".join(item["locator"] for item in batch),
                        "text": description,
                    }
                )
        if self.settings.groq_api_key and doc["audio"]:
            complete = True
            for i, path in enumerate(doc["audio"]):
                data = await read_bytes(backend, path, cap_mb=25)
                try:
                    transcript = await _groq_transcript(
                        self.settings.groq_api_key.get_secret_value(), data, path
                    )
                except (httpx.HTTPError, ValueError, OSError):
                    transcript = "transcription failed"
                if transcript.startswith("transcription failed"):
                    complete = False
                    warnings.append(f"Could not transcribe audio segment starting at {i * 600}s.")
                    continue
                for part in grouped(transcript.splitlines(), 35):
                    blocks.append(
                        {
                            "id": f"b{len(blocks) + 1:04d}",
                            "locator": f"Audio segment starting at {i * 600}s (timestamps relative to segment)",
                            "text": "\n".join(part),
                        }
                    )
            if complete:
                warnings = [note for note in warnings if not note.startswith("Audio requires")]
        enriched = dict(doc, blocks=blocks, warnings=list(dict.fromkeys(warnings)))
        await self.put(backend, location, enriched)
        return enriched

    def abstract_policy(self, level: Level) -> list:
        policy = [self.common_key, self.prompts[level]]
        if level != "low":
            policy.extend([COMPACT_BOUNDARY, COMPACT_ABSTRACT_LAYOUT, ABSTRACT_CAPS[level]])
        return policy

    def abstract_path(self, record: dict, level: Level) -> str:
        cache = fingerprint(self.abstract_policy(level))[:20]
        filename = PurePosixPath(record["path"]).name.encode()[:160].decode(errors="ignore")
        return f"{record['bundle']}/abstracts/{cache}/{filename}.abstract.{level}"

    async def cached_abstract(self, backend: Any, path: str) -> dict | None:
        result = await self.optional(backend, path + ".json")
        if result is None:
            return None
        check = await backend.aexecute(f"test -s {shlex.quote(path)}", timeout=10)
        return result if check.exit_code == 0 else None

    async def prepared_document(self, backend: Any, record: dict) -> dict:
        if record.get("error"):
            raise ToolException(record["error"])
        result = await self.optional(backend, f"{record['bundle']}/document.json")
        if result is None:
            if record.get("preprocessed"):
                lock = f"{self.thread_id()}:{record['bundle']}"
                async with self._locks.setdefault(lock, asyncio.Lock()):
                    return await self.document(backend, record)
            raise ToolException(
                "Normalized source is unavailable. Check the source coverage for intake errors."
            )
        return result

    async def abstract_for(
        self,
        backend: Any,
        record: dict,
        level: Level,
        *,
        on_summarizing: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        path = self.abstract_path(record, level)
        # Include thread identity so equal filenames/content in independent sandboxes never block one another.
        lock_key = f"{self.thread_id()}:{record['bundle']}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock, self.limiter:
            if result := await self.cached_abstract(backend, path):
                return result
            document = (
                await self.document(backend, record)
                if level == "high"
                else await self.prepared_document(backend, record)
            )
            if on_summarizing is not None:
                await on_summarizing(record)
            doc = await self.enriched(backend, document)
            compact = level != "low"
            boundary = COMPACT_BOUNDARY if compact else BOUNDARY
            system = f"{boundary}\n{self.prompts[level]}"
            cap = ABSTRACT_CAPS[level]
            sections = []
            inputs = abstract_inputs(doc)
            for index, formatted in enumerate(inputs):
                checkpoint = (
                    f"{path}.maps/{index}-{fingerprint(formatted)[:20]}.json"
                    if len(inputs) > 5
                    else None
                )
                summary = await self.optional(backend, checkpoint) if checkpoint else None
                if summary is None:
                    summary = await self.summarize(
                        system, formatted, cap=cap, omit_citations=compact
                    )
                    if checkpoint:
                        await self.put(backend, checkpoint, summary)
                sections.append(summary)
            synopsis = await self.reduce(sections, system, cap=cap, omit_citations=compact)
            text = format_abstract(
                doc, level, synopsis, sections if level == "low" and len(sections) > 1 else []
            )
            await self.save(backend, path, text.encode())
            result = {
                "path": path,
                "source_id": doc["id"],
                "name": doc.get("url", doc["name"]),
                "synopsis": synopsis,
                "warnings": doc["warnings"],
                "blocks": len(doc["blocks"]),
                "visuals": len(doc["assets"]),
                "normalized": f"{doc['bundle']}/source.md",
                **{
                    key: doc[key]
                    for key in ("parser", "url", "web_sha256", "resolved_url")
                    if key in doc
                },
            }
            await self.put(backend, path + ".json", result)
            return result

    def thread_id(self) -> str:
        from insightful_report_agent.runtime.sandbox.docker import current_thread_id

        return current_thread_id()

    async def covered_rows(self, backend: Any, records: list[dict]) -> list[dict]:
        root = self.overview_root(records)
        rows = []
        for cursor in range(0, len(records), BATCH_SIZE):
            batch = await self.optional(backend, f"{root}/batch-{cursor}.json")
            expected = [row["id"] for row in records[cursor : cursor + BATCH_SIZE]]
            if not isinstance(batch, list) or [row.get("source_id") for row in batch] != expected:
                raise ToolException(f"Prepare remaining source batch starting at cursor={cursor}")
            rows.extend(batch)
        return rows

    async def prepare_sources(
        self,
        backend: Any,
        records: list[dict],
        *,
        on_summarizing: Callable[[dict], Awaitable[None]] | None = None,
    ) -> list[dict]:
        """Create reusable normalization and high abstracts outside the report graph."""
        root = self.overview_root(records)

        async def prepare(record: dict) -> dict:
            if not self.legacy:
                row = {"source_id": record["id"], "name": record["name"]}
                if record.get("error"):
                    return {**row, "error": record["error"]}
                return {
                    **row,
                    "path": record["brief"],
                    "synopsis": record["synopsis"],
                    "warnings": [],
                    "wiki_index": record["wiki_index"],
                    "parser": "Bulk Reference Files Preprocessor",
                    **({"url": record["url"]} if "url" in record else {}),
                }
            try:
                return await self.abstract_for(
                    backend, record, "high", on_summarizing=on_summarizing
                )
            except (ToolException, WorkspaceError) as error:
                reason = str(error)[:300]
            except Exception as error:  # noqa: BLE001 - provider payloads can contain secrets
                reason = type(error).__name__
            return {"source_id": record["id"], "name": record["name"], "error": reason}

        prepared = []
        for cursor in range(0, len(records), BATCH_SIZE):
            path = f"{root}/batch-{cursor}.json"
            batch = records[cursor : cursor + BATCH_SIZE]
            rows = await self.optional(backend, path)
            if rows is None or any("error" in row for row in rows):
                rows = await asyncio.gather(*(prepare(record) for record in batch))
                await self.put(backend, path, rows)
            prepared.extend(rows)
        return prepared

    def tools(self) -> list[BaseTool]:
        async def list_sources(
            runtime: ToolRuntime,
            cursor: Annotated[int, Field(ge=0)] = 0,
        ) -> dict:
            """List thread source files, including ZIP entries, in stable pages with source IDs and original paths."""
            records = await self.records(self.backend(runtime))
            return {
                "total": len(records),
                "snapshot": self.snapshot(records),
                "sources": [
                    {key: row[key] for key in ("id", "name", "path", "url", "error") if key in row}
                    for row in records[cursor : cursor + BATCH_SIZE]
                ],
                "next_cursor": cursor + BATCH_SIZE if cursor + BATCH_SIZE < len(records) else None,
            }

        def locate(records: list[dict], file_path: str) -> dict:
            for item in records:
                if item["id"] == file_path:
                    return item
            normalized = workspace_path(file_path)
            matches = [item for item in records if item["path"] == normalized]
            if len(matches) == 1:
                return matches[0]
            raise ToolException(
                "Supply the exact source ID or original path returned by list_sources"
            )

        async def read_source_file_abstract(
            file_path: str,
            level: Level,
            runtime: ToolRuntime,
            offset: Annotated[int, Field(ge=0)] = 0,
        ) -> dict:
            """Read a source brief by source ID. Use level=high and next_offset to page. Only legacy intake supports generated middle/low abstracts."""
            backend = self.backend(runtime)
            record = locate(await self.records(backend), file_path)
            if not self.legacy:
                if record.get("error"):
                    raise ToolException(record["error"])
                if level != "high":
                    raise ToolException("Use level=high for the supplied brief or read_source_evidence")
                content = (await read_bytes(backend, record["brief"], cap_mb=5)).decode("utf-8-sig")
                return {
                    "source_id": record["id"],
                    "path": record["brief"],
                    "level": level,
                    "generated": False,
                    **page(content, offset),
                }
            generated = False

            async def on_summarizing(_record: dict) -> None:
                nonlocal generated
                generated = True

            if level == "high":
                result = await self.cached_abstract(backend, self.abstract_path(record, level))
                if result is None:
                    raise ToolException(
                        "High abstract unavailable. Check intake errors in prepare_source_overview."
                    )
            else:
                result = await self.abstract_for(
                    backend, record, level, on_summarizing=on_summarizing
                )
            content = (await read_bytes(backend, result["path"], cap_mb=32)).decode()
            return {**result, "level": level, "generated": generated, **page(content, offset)}

        async def prepare_source_overview(
            runtime: ToolRuntime,
            cursor: Annotated[int, Field(ge=0)] = 0,
        ) -> dict:
            """Read one batch of source coverage, including preprocessing failures. Follow next_cursor through all batches."""
            backend = self.backend(runtime)
            records = await self.records(backend)
            if not records or cursor >= len(records) or cursor % BATCH_SIZE:
                raise ToolException(
                    f"Use a batch cursor from 0 to {max(0, len(records) - 1)} in increments of {BATCH_SIZE}"
                )
            path = f"{self.overview_root(records)}/batch-{cursor}.json"

            rows = await self.optional(backend, path)
            if rows is None:
                raise ToolException("Source preparation is incomplete for this snapshot.")
            coverage = {
                "total": len(records),
                "snapshot": self.snapshot(records),
                "batch": str(path),
                "processed": len(rows),
                "failed": [
                    {"id": row["source_id"], "error": row["error"]}
                    for row in rows
                    if "error" in row
                ],
                "next_cursor": cursor + BATCH_SIZE if cursor + BATCH_SIZE < len(records) else None,
            }
            if not self.legacy:
                coverage["sources"] = [
                    {"id": row["source_id"], "name": row["name"], "summary": row["synopsis"][:600]}
                    for row in rows
                    if "error" not in row
                ]
            return coverage

        async def synthesize_source_overview(runtime: ToolRuntime) -> dict:
            """Open the full, snapshot-specific coverage catalog. For preprocessed sources it uses the supplied index entries without generating summaries."""
            backend = self.backend(runtime)
            records = await self.records(backend)
            corpus = await self.corpus_overview(backend, records)
            return {key: val for key, val in corpus.items() if key not in {"rows", "index"}}

        async def list_source_figures(
            file_path: str,
            runtime: ToolRuntime,
            cursor: Annotated[int, Field(ge=0)] = 0,
        ) -> dict:
            """Find figures in a selected source, independently of text evidence filters. Use look on a returned path to inspect a figure before embedding it."""
            backend = self.backend(runtime)
            record = locate(await self.records(backend), file_path)
            doc = await self.prepared_document(backend, record)
            assets = doc["assets"]
            page_size = 20
            return {
                "source_id": doc["id"],
                "total": len(assets),
                "figures": assets[cursor : cursor + page_size],
                "warnings": doc["warnings"],
                "next_cursor": cursor + page_size if cursor + page_size < len(assets) else None,
            }

        async def read_source_evidence(
            file_path: str,
            runtime: ToolRuntime,
            block_id: str | None = None,
            keyword: str | None = None,
            cursor: Annotated[int, Field(ge=0)] = 0,
            locator: str | None = None,
        ) -> dict:
            """Retrieve original evidence blocks and related visuals. Filter by exact block_id, text keyword, or original locator such as Page 4."""
            backend = self.backend(runtime)
            record = locate(await self.records(backend), file_path)
            doc = await self.enriched(backend, await self.prepared_document(backend, record))
            location = (
                re.compile(rf"(?<!\w){re.escape(locator.strip())}(?!\w)", re.IGNORECASE)
                if locator and locator.strip()
                else None
            )
            rows = [
                item
                for item in doc["blocks"]
                if (block_id is None or item["id"] == block_id)
                and (not keyword or keyword.casefold() in item["text"].casefold())
                and (location is None or location.search(item["locator"]))
            ]
            selected = rows[cursor : cursor + 2]
            ids = {row["id"] for row in selected}
            return {
                "source_id": doc["id"],
                "name": doc.get("url", doc["name"]),
                "original_path": doc["path"],
                "markdown": f"{doc['bundle']}/source.md",
                "blocks": selected,
                "assets": [item for item in doc["assets"] if ids.intersection(item["blocks"])],
                "warnings": doc["warnings"],
                "next_cursor": cursor + 2 if cursor + 2 < len(rows) else None,
            }

        async def add_source_urls(
            urls: Annotated[list[HttpUrl], Field(min_length=1, max_length=30)],
            runtime: ToolRuntime,
        ) -> dict:
            """Import selected public webpages as evidence. This may take several minutes. Refresh the corpus overview and read exact evidence blocks before citing."""
            if any(url.username is not None or url.password is not None for url in urls):
                raise ToolException("URLs with embedded credentials are refused")
            links = list(dict.fromkeys(map(str, urls)))
            backend = self.backend(runtime)
            if self.preprocess_urls:
                async with self._locks.setdefault(f"{self.thread_id()}:web-intake", asyncio.Lock()):
                    records = await self.records(backend)
                    existing = {record.get("url") for record in records if not record.get("error")}
                    pending = [link for link in links if link not in existing]
                    state = None
                    if pending:
                        try:
                            name, data, state = await preprocess(
                                self.settings, None, {}, urls=pending
                            )
                        except PreprocessorError as error:
                            raise ToolException(str(error)) from None
                        digest = hashlib.sha256(data).hexdigest()
                        await self.save(backend, f"/workspace/attachments/{digest}/{name}", data)
                        records = await self.records(backend)
                    await self.prepare_sources(backend, records)
                    return {
                        "submitted": len(pending),
                        "already_available": len(links) - len(pending),
                        "preprocessing_state": state,
                        "snapshot": self.snapshot(records),
                    }
            files = url_files(links)
            for name, content in files:
                await write_bytes(backend, f"/workspace/attachments/web/{name}", content)
            await self.prepare_sources(backend, await self.records(backend))
            return {"registered": len(links)}

        async def search_web(
            query: Annotated[str, Field(min_length=1, max_length=128)],
        ) -> list[dict]:
            """Search the public web for supplementary sources. Send only public search terms. Results are discovery leads, including provider summaries; verify selected pages before citing."""
            query = query.strip()
            if not query:
                raise ToolException("Enter a non-blank web search query")
            if self.exa_search:
                url = "https://api.exa.ai/search"
                headers = {"x-api-key": self.exa_search.get_secret_value()}
                body = {"query": query, "numResults": 8}
            elif self.search_key:
                url = DEEPSEEK_SEARCH_URL
                headers = {"Authorization": f"Bearer {self.search_key}"}
                body = {"query": query, "count": 8, "need_content": False}
            else:
                raise ToolException("Configure a search provider or a search MCP tool")
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(url, headers=headers, json=body)
                    response.raise_for_status()
                    rows = response.json()["results"]
                    if not isinstance(rows, list):
                        raise ValueError("Expected a results array")
            except httpx.HTTPStatusError as error:
                raise ToolException(
                    f"Web search provider HTTP {error.response.status_code}"
                ) from None
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                raise ToolException(
                    "Web search provider unavailable or returned an invalid response"
                ) from None
            results = []
            for row in rows[:8]:
                if not isinstance(row, dict):
                    continue
                try:
                    link = HttpUrl(row.get("url"))
                except ValidationError:
                    continue
                if link.username is not None or link.password is not None:
                    continue
                result = {"url": str(link)}
                for field, limit in (
                    ("title", 300),
                    ("date", 80),
                    ("publishedDate", 80),
                    ("site_name", 200),
                    ("snippet", 300),
                    ("summary", 1000),
                ):
                    if isinstance(value := row.get(field), str):
                        result[field] = value[:limit]
                results.append(result)
            return results

        async def publish_report(
            draft_path: str,
            runtime: ToolRuntime,
            validate_only: bool = False,
            require_source_figures: bool = False,
            min_visuals: Annotated[int, Field(ge=0)] = 0,
        ) -> dict:
            """Deliver the standalone HTML report. Use validate_only=true to check rendering, min_visuals for the planned minimum number of images/charts/diagrams, and require_source_figures=true when source illustrations are central. Drafts must be Markdown under /workspace/projects/."""
            return await self.publish(
                self.backend(runtime),
                draft_path,
                runtime,
                validate_only=validate_only,
                require_source_figures=require_source_figures,
                min_visuals=min_visuals,
            )

        functions = [
            list_sources,
            read_source_file_abstract,
            prepare_source_overview,
            synthesize_source_overview,
            list_source_figures,
            read_source_evidence,
            publish_report,
        ]
        if self.legacy or self.preprocess_urls:
            functions.append(add_source_urls)
        if self.exa_search or self.search_key:
            functions.append(search_web)
        return [
            StructuredTool.from_function(coroutine=fn, handle_tool_error=True) for fn in functions
        ]

    async def corpus_overview(
        self, backend: Any, records: list[dict], *, create: bool = True
    ) -> dict:
        if not records:
            raise ToolException("No source material has been registered")
        root = self.overview_root(records)
        rows = await self.covered_rows(backend, records)
        successes = [row for row in rows if "synopsis" in row]
        if not successes:
            raise ToolException("No source was successfully understood")
        index = fingerprint(rows)
        path = f"{root}/corpus.json"
        cached = await self.optional(backend, path)
        if cached is not None and cached["index"] == index:
            return cached
        if not create:
            raise ToolException("Call synthesize_source_overview for the current snapshot before publishing")
        if self.legacy:
            summaries = [
                row["source_id"]
                + " · "
                + row["name"]
                + "\n"
                + row["synopsis"]
                + ("\nCoverage notes: " + "; ".join(row["warnings"]) if row["warnings"] else "")
                for row in successes
            ]
            overview = await self.reduce(
                summaries, f"{COMPACT_BOUNDARY}\n{CORPUS_PROMPT}", cap=4600, omit_citations=True
            )
        else:
            overview = (
                f"{len(successes)}/{len(rows)} sources have an export and brief. "
                "Read the coverage catalog for the original index summaries and linked briefs."
            )
        catalog = f"{root}/catalog.md"
        await self.save(
            backend,
            catalog,
            (
                "# Source coverage\n\n"
                + "\n\n".join(
                    f"## {row['source_id']} · {row['name']}\n"
                    + (
                        f"Processing failed: {row['error']}"
                        if "error" in row
                        else row["synopsis"]
                        + f"\n\nBrief path: {row['path']}"
                        + (
                            "\n\nCoverage notes: " + "; ".join(row["warnings"])
                            if row["warnings"]
                            else ""
                        )
                    )
                    for row in rows
                )
            ).encode(),
        )
        result = {
            "snapshot": self.snapshot(records),
            "catalog": catalog,
            "overview": overview,
            "reviewed": len(successes),
            "failed": len(rows) - len(successes),
            "source_count": len(rows),
            "index": index,
            "rows": rows,
        }
        await self.put(backend, path, result)
        return result

    async def publish(
        self,
        backend: Any,
        draft_path: str,
        runtime: ToolRuntime,
        *,
        validate_only: bool = False,
        require_source_figures: bool = False,
        min_visuals: int = 0,
    ) -> dict:
        path = workspace_path(draft_path)
        if not path.startswith("/workspace/projects/") or not path.lower().endswith(".md"):
            raise ToolException("Draft must be a .md file under /workspace/projects/")
        records = await self.records(backend)
        corpus = await self.corpus_overview(backend, records, create=not self.legacy)
        try:
            draft = (await read_bytes(backend, path, cap_mb=2)).decode()
        except (WorkspaceError, UnicodeError):
            raise ToolException("Draft is not readable UTF-8 Markdown") from None
        citations = CITATION.findall(draft)
        visuals = re.findall(r"source:(s-[0-9a-f]{16})/(a\d{4,})", draft)
        cited = {source for source, _ in citations}
        visual = {source for source, _ in visuals}
        if not cited:
            raise ToolException("Report needs at least one valid inline evidence citation")
        by_id = {row["id"]: row for row in records}
        reviewed = {row["source_id"] for row in corpus["rows"] if "synopsis" in row}
        if unknown := (cited | visual) - reviewed:
            raise ToolException("Unreviewed or unknown source IDs: " + ", ".join(sorted(unknown)))
        documents = {}
        source_warnings = {}
        for key in cited | visual:
            doc = await self.enriched(backend, await self.prepared_document(backend, by_id[key]))
            source_warnings[key] = doc["warnings"]
            wanted_assets = {asset for source, asset in visuals if source == key}
            assets = {item["id"]: item for item in doc["assets"] if item["id"] in wanted_assets}
            wanted_blocks = {block for source, block in citations if source == key}
            wanted_blocks.update(block for item in assets.values() for block in item["blocks"][:3])
            documents[key] = {
                name: doc[name] for name in ("name", "url", "web_sha256") if name in doc
            }
            documents[key].update(
                assets=assets,
                blocks={item["id"]: item for item in doc["blocks"] if item["id"] in wanted_blocks},
            )
        adapter = self.profile.delivery
        if adapter is None:
            raise ToolException("Report delivery is unavailable on this surface")
        # Keep the standalone document comfortably within all delivery transports.
        limit = min(25 * 1024**2, getattr(adapter, "max_file_bytes", 25 * 1024**2))
        output = json.loads(
            await self.run(
                backend,
                RENDERER,
                "render",
                {
                    "draft": draft,
                    "documents": documents,
                    "rows": [
                        {**row, "warnings": source_warnings[row["source_id"]]}
                        if row["source_id"] in source_warnings
                        else row
                        for row in corpus["rows"]
                    ],
                    "records": records,
                    "snapshot": corpus["snapshot"],
                    "limit": limit,
                    "require_source_figures": require_source_figures,
                    "min_visuals": min_visuals,
                },
            )
        )
        if validate_only:
            return {"validated": True, **output}
        data = await read_bytes(backend, output["path"], cap_mb=25)
        name = PurePosixPath(output["path"]).name
        receipt = f"{LIBRARY}/deliveries/{name}.json"
        async with self._locks.setdefault(f"{self.thread_id()}:{receipt}", asyncio.Lock()):
            return await self.deliver(
                backend, adapter, runtime, receipt, name, data, output, corpus
            )

    async def deliver(self, backend, adapter, runtime, receipt, name, data, output, corpus) -> dict:
        status = "delivered"
        if await self.optional(backend, receipt) is None:
            thread = self.profile.thread_id_for(runtime)

            async def send() -> None:
                try:
                    await asyncio.to_thread(
                        adapter.deliver, thread, [DeliveredFile(name=name, content=data)]
                    )
                except Exception as error:  # noqa: BLE001
                    raise ToolException(
                        "Report rendered but delivery could not be confirmed "
                        f"({type(error).__name__})"
                    ) from None
                await self.put(backend, receipt, {"file": name})

            queue = pending_reports.get()
            if queue is None:
                await send()
            else:
                queue[name] = send
                status = "queued_for_delivery"
        return {
            status: name,
            "reviewed": corpus["reviewed"],
            "failed": corpus["failed"],
            "citations": output["citations"],
            "visuals": output["visuals"],
            "source_figures": output["source_figures"],
            "charts": output["charts"],
            "diagrams": output["diagrams"],
        }
