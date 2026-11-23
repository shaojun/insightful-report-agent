"""Durable Host intake when more attachments arrive or a worker is interrupted."""

import asyncio
from types import SimpleNamespace

from langgraph.store.memory import InMemoryStore

from insightful_report_agent.runtime import source_intake


async def test_upload_during_processing_and_resume_after_interruption(monkeypatch) -> None:
    monkeypatch.setattr(source_intake, "stage", lambda *_: None)
    paths = set()
    started = asyncio.Event()
    release = asyncio.Event()
    processed = []
    events = []
    historical = "historical"
    paths.add(historical)

    async def upload(files):
        paths.update(path for path, _ in files)
        return [SimpleNamespace(error=None) for _ in files]

    async def records(_backend):
        return [{"id": path, "path": path} for path in sorted(paths)]

    async def prepare(_backend, sources, *, on_summarizing):
        processed.append([source["id"] for source in sources])
        for source in sources:
            await on_summarizing(source)
        if len(processed) == 1:
            started.set()
            await release.wait()
        return [{"source_id": source["id"]} for source in sources]

    async def notify(thread, stage, rows):
        events.append((thread, stage, [row["source_id"] for row in rows]))

    binding = SimpleNamespace(staging=None, sandbox=SimpleNamespace(aupload_files=upload))
    library = SimpleNamespace(records=records, prepare_sources=prepare, legacy=True)
    store = InMemoryStore()
    thread = "feishu-test"
    worker = source_intake.SourceIntake(binding, None, library, store, "feishu", notify=notify)

    await worker.attach(thread, {"first": b"first"})
    await asyncio.wait_for(started.wait(), timeout=5)
    await worker.attach(thread, {"second": b"second"})
    assert await store.aget(worker.namespace, thread) is not None
    await worker.aclose()  # Stop before either source has a committed intake snapshot.

    restarted = source_intake.SourceIntake(binding, None, library, store, "feishu", notify=notify)
    try:
        await restarted.restore()
        task = restarted.tasks[thread]
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert processed == [["first", historical], ["first", historical, "second"]]
        assert events == [
            (thread, "normalizing", []),
            (thread, "summarizing", []),
            (thread, "normalizing", []),
            (thread, "summarizing", []),
            (thread, "prepared", ["first", "second"]),
        ]
        assert await store.aget(worker.namespace, thread) is None
    finally:
        await restarted.aclose()
