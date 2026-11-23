"""Exercise the standalone exporter with real inputs and offline service responses."""

from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote
from zipfile import ZipFile

import httpx
from PIL import Image

from insightful_report_agent.tools.mineru import MinerU
from insightful_report_agent.tools.source_export import CHUNK_CHARS, SourceExporter, source_chunks
from tests.scripted_model import RespondingChatModel, reply

PROJECT = Path(__file__).resolve().parents[1]
PATENTS = sorted((PROJECT / "tests/assets/patent_docs").glob("*.pdf"))


class SourceExporterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if os.getenv("INSIGHTFUL_KEEP_EXPORT_TEST_OUTPUT") == "1":
            output = PROJECT / "workspace" / "out" / "source-export-tests"
            output.mkdir(parents=True, exist_ok=True)
            self.root = Path(mkdtemp(prefix="run-", dir=output))
            print(f"Source export test output saved under {self.root}", flush=True)
        else:
            directory = TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            self.root = Path(directory.name)
        self.model = RespondingChatModel(respond=lambda _: reply("座椅的背部支撑装置包含支撑件。"))
        self.exporter = SourceExporter(self.root / "outputs", model=self.model)

    async def test_batch_patent_pdfs_reuses_mineru_result_and_preserves_originals(self):
        uploads = []
        markdown = "# 背部支撑装置\n\n支撑件安装在框架上。\n\n![图1](images/fig.png)\n"
        sources = PATENTS[:2]
        self.assertEqual(len(sources), 2)

        png = io.BytesIO()
        Image.new("RGB", (5, 5)).save(png, "PNG")
        output = io.BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("nested/full.md", markdown)
            archive.writestr("nested/images/fig.png", png.getvalue())
            archive.writestr(
                "nested/content_list.json",
                json.dumps([{"type": "text", "page_idx": 2, "text": "支撑件安装在框架上。"}]),
            )
            archive.writestr("nested/../../ignored.png", png.getvalue())

        batches = {}

        def request(http_request):
            if http_request.url.host == "mineru.net" and http_request.method == "POST":
                data = json.loads(http_request.content)
                batch = f"batch-{len(batches)}"
                file = data["files"][0]
                self.assertTrue(file["name"].endswith(".pdf"))
                batches[batch] = file["data_id"]
                data = {"batch_id": batch, "file_urls": ["https://test.aliyuncs.com/upload"]}
            elif http_request.method == "PUT":
                uploads.append(http_request.content)
                return httpx.Response(200)
            elif http_request.url.host == "mineru.net":
                batch = http_request.url.path.split("/")[-1]
                data = {
                    "extract_result": [
                        {
                            "state": "done",
                            "data_id": batches[batch],
                            "full_zip_url": "https://test.mineru.net/result.zip",
                        }
                    ]
                }
            else:
                return httpx.Response(200, content=output.getvalue())
            return httpx.Response(200, json={"code": 0, "data": data})

        self.exporter.mineru = MinerU(
            "test-token", wait=2, interval=0.01, transport=httpx.MockTransport(request)
        )
        rows = await self.exporter.prepare(*sources)
        self.assertEqual(uploads, [source.read_bytes() for source in sources])
        for source, result in zip(sources, rows, strict=True):
            self.assertIsNone(result.error, result.error)
            self.assertEqual(result.original.read_bytes(), source.read_bytes())
            self.assertTrue(result.export.name == f"{source.name}.export.md")
            self.assertEqual(result.brief.name, f"{source.name}.brief.md")
            self.assertEqual(result.content_index.name, f"{source.name}.content-index.md")
            self.assertEqual(result.export.read_text(), markdown)
            self.assertEqual(json.loads(result.evidence.read_bytes())[0]["page_idx"], 2)
            assets = result.export.parent / "images/fig.png"
            self.assertEqual(assets.read_bytes(), png.getvalue())
            self.assertFalse(result.export.parent.joinpath("ignored.png").exists())
            brief = result.brief.read_text()
            front, closing, body = brief.partition("\n---\n")
            fields = {
                key: json.loads(value)
                for key, _, value in (line.partition(": ") for line in front.splitlines()[1:])
            }
            self.assertEqual(fields["original-file-path"], str(result.original))
            self.assertEqual(result.brief.parent / fields["export-file-path"], result.export)
            self.assertEqual(
                result.brief.parent / fields["content-index-file-path"], result.content_index
            )
            self.assertEqual(len(fields), 4)
            self.assertEqual(fields["file-content-brief"], "座椅的背部支撑装置包含支撑件。")
            self.assertEqual(closing, "\n---\n")
            self.assertEqual(body, "")
            index = result.content_index.read_text()
            self.assertIn("## Contents", index)
            self.assertIn(f"export/{source.name}.export.md#L1-L5", unquote(index))
        calls = self.model.call_count
        # Reopen an output written in the former combined layout.
        previous = rows[0]
        brief = previous.brief.read_text()
        index = previous.content_index.read_text()
        description = brief.splitlines()[4].replace("file-content-brief: ", "description: ", 1)
        front = f"---\nname: {json.dumps(str(previous.original))}\n{description}\n---\n"
        previous.brief.write_text(front + "\n" + index)
        previous.content_index.unlink()
        self.assertEqual(await self.exporter.prepare(*sources), rows)
        self.assertEqual(previous.brief.read_text(), brief)
        self.assertEqual(previous.content_index.read_text(), index)
        previous.brief.write_text(front)
        self.assertEqual(await self.exporter.prepare(*sources), rows)
        self.assertEqual(previous.brief.read_text(), brief)
        legacy_pointer = (
            f"\nUse the [content index]({quote(previous.content_index.name)}) to find relevant passages "
            "and read their line or character ranges in the export.\n"
        )
        previous.brief.write_text(
            brief.replace("\nfile-content-brief: ", "\ndescription: ", 1) + legacy_pointer
        )
        self.assertEqual(await self.exporter.prepare(*sources), rows)
        self.assertEqual(previous.brief.read_text(), brief)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(self.model.call_count, calls)

    async def test_url_snapshot_persists_even_when_another_source_fails(self):
        document = b"# A source\n\nFacts from a public web page.\n"
        url = "https://example.org/source"
        resolved = "https://example.org/redirected.md"
        fetch = AsyncMock(return_value=(document, ".md", resolved))
        with patch("insightful_report_agent.tools.source_export.fetch_source", fetch):
            bad, good = await self.exporter.prepare(self.root / "missing.pdf", url)
            (again,) = await self.exporter.prepare(url)
        self.assertIn("Snapshot failed", bad.error)
        self.assertIsNone(good.error)
        self.assertEqual(good, again)
        fetch.assert_awaited_once_with(url)
        self.assertEqual(good.original.read_bytes(), document)
        self.assertEqual(good.export.read_bytes(), document)
        self.assertEqual(json.loads(good.snapshot.read_bytes())["resolved_url"], resolved)

    async def test_index_fanout_and_resume_after_summary_failure(self):
        source = self.root / "test.md"
        # Unicode plus a very long line exercises both line and character addressing.
        markdown = "# Beginning\n" + "座椅" * (CHUNK_CHARS * 5) + "\n# End\n"
        source.write_text(markdown)
        calls = []

        def respond(messages):
            calls.append(messages[-1].content)
            if len(calls) == 2:
                raise TimeoutError
            return reply("支撑件和框架。")

        self.model.respond = respond
        (failed,) = await self.exporter.prepare(source)
        self.assertIn("Brief generation failed", failed.error)
        self.assertIsNone(failed.brief)
        self.assertIsNone(failed.content_index)
        self.assertEqual(failed.export.read_text(), markdown)
        first_input = calls[0]
        (result,) = await self.exporter.prepare(source)
        self.assertIsNone(result.error)
        self.assertEqual(calls.count(first_input), 1)
        self.assertEqual(len(result.brief.read_text().splitlines()), 6)
        self.assertNotIn("[Subsections]", result.brief.read_text())
        self.assertIn("[Subsections]", result.content_index.read_text())
        pages = [result.content_index, *result.content_index.parent.glob("*.index.md")]
        for page in pages:
            self.assertLessEqual(page.read_text().count("\n- "), 8)
        self.assertTrue(all(len(content) <= CHUNK_CHARS for content in calls))
        chunks = list(source_chunks(markdown))
        self.assertEqual("".join(text for _, text in chunks), markdown)
        self.assertTrue(all(markdown[item.start : item.end] == text for item, text in chunks))
