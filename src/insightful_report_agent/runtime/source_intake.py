"""Host-owned preparation of received sources, independent of a report graph turn."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from typing import Literal

from langchain_core.runnables.config import var_child_runnable_config
from langgraph.store.base import BaseStore

from insightful_report_agent.middleware.workspace import stage
from insightful_report_agent.runtime.observability import Observability
from insightful_report_agent.runtime.sandbox import SandboxBinding
from insightful_report_agent.tools.sources import SourceLibrary
from insightful_report_agent.workspace.manifest import Manifest

_LOGGER = logging.getLogger(__name__)
IntakeStage = Literal["normalizing", "summarizing", "prepared", "failed"]
IntakeNotify = Callable[[str, IntakeStage, list[dict]], Awaitable[None]]


class SourceIntake:
    """One worker per thread, with originals and preparation results as durable state."""

    def __init__(
        self,
        binding: SandboxBinding,
        manifest: Manifest,
        library: SourceLibrary,
        store: BaseStore,
        surface: str,
        notify: IntakeNotify | None = None,
        *,
        trace: Observability | None = None,
    ) -> None:
        self.binding = binding
        self.manifest = manifest
        self.library = library
        self.store = store
        self.namespace = ("insightful_report_agent", "source_intake", surface)
        self.notify = notify
        self.trace = trace
        self.surface = surface
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._dirty: set[str] = set()
        self._ready: set[str] = set()

    def _lock(self, thread: str) -> asyncio.Lock:
        return self._locks.setdefault(thread, asyncio.Lock())

    async def _pending_attachments(self, thread: str) -> set[str]:
        item = await self.store.aget(self.namespace, thread)
        return set(item.value.get("attachments", ())) if item else set()

    async def attach(self, thread: str, files: Mapping[str, bytes], *, notify: bool = True) -> None:
        async with self._lock(thread):
            # Persist the marker before the upload so an interrupted process can scan whatever landed.
            pending: dict = {"pending": True}
            if self.notify is not None:
                paths = await self._pending_attachments(thread)
                pending["attachments"] = sorted(paths.union(files) if notify else paths)
            await self.store.aput(self.namespace, thread, pending)
            self._ready.discard(thread)
            try:
                await self.upload(thread, files)
            finally:
                self._schedule(thread)

    async def upload(self, thread: str, files: Mapping[str, bytes]) -> None:
        token = var_child_runnable_config.set({"configurable": {"thread_id": thread}})
        try:
            uploaded = await self.binding.sandbox.aupload_files(list(files.items()))
            if len(uploaded) != len(files) or any(item.error for item in uploaded):
                raise RuntimeError("Attachment upload failed")
        finally:
            var_child_runnable_config.reset(token)

    def _schedule(self, thread: str) -> asyncio.Task[None]:
        self._dirty.add(thread)
        if task := self.tasks.get(thread):
            return task
        task = asyncio.create_task(self._run(thread), name="source intake")
        self.tasks[thread] = task
        # Uploads can finish without a report waiter; consume exceptions in either case.
        task.add_done_callback(self._consume_result)
        return task

    @staticmethod
    def _consume_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _notify(self, thread: str, stage: IntakeStage, rows: list[dict]) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(thread, stage, rows)
        except Exception as error:  # noqa: BLE001 - channel failures cannot stop preparation
            _LOGGER.warning("Source intake notification failed (%s)", type(error).__name__)

    async def _run(self, thread: str) -> None:
        trace = self.trace
        with (
            trace.request(thread, self.surface) if trace else nullcontext(),
            trace.span("sources.import") if trace else nullcontext(),
        ):
            await self._import_sources(thread)

    async def _import_sources(self, thread: str) -> None:
        token = var_child_runnable_config.set({"configurable": {"thread_id": thread}})
        paths: set[str] = set()
        try:
            if self.notify is not None:
                paths = await self._pending_attachments(thread)
            await asyncio.to_thread(stage, self.binding.staging, self.manifest)
            while True:
                async with self._lock(thread):
                    self._dirty.discard(thread)
                    if self.notify is not None:
                        paths = await self._pending_attachments(thread)
                    records = await self.library.records(self.binding.sandbox)
                await self._prepare(thread, records, paths)
                async with self._lock(thread):
                    if thread in self._dirty:
                        if paths:
                            remaining = await self._pending_attachments(thread) - paths
                            await self.store.aput(
                                self.namespace,
                                thread,
                                {"pending": True, "attachments": sorted(remaining)},
                            )
                        continue
                    await self.store.adelete(self.namespace, thread)
                    self._ready.add(thread)
                    return
        except Exception as error:  # noqa: BLE001 - exceptions may contain provider data
            _LOGGER.warning("Source intake could not finish (%s)", type(error).__name__)
            if paths:
                await self._notify(thread, "failed", [])
            raise RuntimeError("Source intake could not finish; retry the request.") from None
        finally:
            var_child_runnable_config.reset(token)
            self.tasks.pop(thread, None)
            self._dirty.discard(thread)

    async def _prepare(self, thread: str, records: list[dict], paths: set[str]) -> None:
        if not paths:
            await self.library.prepare_sources(self.binding.sandbox, records)
            return
        selected = {r["id"]: r for r in records if r.get("container_path", r["path"]) in paths}
        if not self.library.legacy:
            rows = await self.library.prepare_sources(self.binding.sandbox, records)
            await self._notify(
                thread,
                "prepared",
                [
                    {**row, "wiki_index": selected[row["source_id"]].get("wiki_index")}
                    for row in rows
                    if row["source_id"] in selected
                ],
            )
            return
        await self._notify(thread, "normalizing", [])
        announced = False

        async def summarizing(record: dict) -> None:
            nonlocal announced
            if not announced and record["id"] in selected:
                announced = True
                await self._notify(thread, "summarizing", [])

        rows = await self.library.prepare_sources(
            self.binding.sandbox, records, on_summarizing=summarizing
        )
        await self._notify(
            thread, "prepared", [row for row in rows if row["source_id"] in selected]
        )

    async def wait_ready(self, thread: str) -> None:
        async with self._lock(thread):
            task = self.tasks.get(thread)
            if task is None and thread not in self._ready:
                if await self.store.aget(self.namespace, thread) is None:
                    await self.store.aput(self.namespace, thread, {"pending": True})
                task = self._schedule(thread)
        if task is not None:
            await asyncio.shield(task)

    async def restore(self, *, thread_prefix: str | tuple[str, ...] = "") -> None:
        pending = []
        while batch := await self.store.asearch(self.namespace, limit=100, offset=len(pending)):
            pending.extend(batch)
        for item in pending:
            if item.key.startswith(thread_prefix):
                self._schedule(item.key)

    async def aclose(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
