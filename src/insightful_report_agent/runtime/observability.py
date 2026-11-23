"""Trace the host request and its LangGraph model/tool calls in one Langfuse tree."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from opentelemetry.trace import INVALID_SPAN, use_span

from insightful_report_agent.config import Settings


class ReportCallback(CallbackHandler):
    def on_tool_start(self, serialized, input_str, **kwargs):
        tool = kwargs.get("name") or (serialized or {}).get("name")
        args = kwargs.get("inputs") or {}
        if isinstance(args, dict):
            if tool == "publish_report":
                kwargs["name"] = (
                    "report.validate" if args.get("validate_only") is True else "report.publish"
                )
            elif tool in {"write_file", "edit_file"}:
                path = args.get("file_path")
                if isinstance(path, str) and path.startswith("/workspace/projects/"):
                    kind = {
                        "outline.md": "outline",
                        "report-progress.md": "progress",
                    }.get(path.rsplit("/", 1)[-1], "draft")
                    kwargs["name"] = f"report.{kind}.{tool}"
        return super().on_tool_start(serialized, input_str, **kwargs)


class Observability:
    def __init__(self, settings: Settings) -> None:
        self.client = None
        self.public_key = None
        self._thread: ContextVar[str | None] = ContextVar("langfuse_report_thread", default=None)
        if (
            settings.langfuse_tracing_enabled
            and settings.langfuse_url
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        ):
            self.public_key = settings.langfuse_public_key.get_secret_value()
            self.client = Langfuse(
                base_url=settings.langfuse_url,
                public_key=self.public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                timeout=5,
            )

    @contextmanager
    def request(self, thread: str, surface: str, user: str | None = None) -> Iterator[None]:
        if self.client is None or self._thread.get() == thread:
            yield
            return
        name = f"report.request.{surface}"
        session = sha256(thread.encode()).hexdigest()
        user_id = sha256(f"{surface}:{user}".encode()).hexdigest() if user else None
        token = self._thread.set(thread)
        try:
            # Transport instrumentation can supply a parent that Langfuse never receives.
            with (
                use_span(INVALID_SPAN),
                self.span(name),
                propagate_attributes(
                    session_id=session,
                    user_id=user_id,
                    tags=["insightful-report-agent", surface],
                    metadata={"surface": surface},
                    trace_name=name,
                ),
            ):
                yield
        finally:
            self._thread.reset(token)

    @contextmanager
    def span(self, name: str, **metadata: Any) -> Iterator[Any]:
        if self.client is None:
            yield None
            return
        with self.client.start_as_current_observation(name=name, metadata=metadata) as span:
            try:
                yield span
            except BaseException as error:
                span.update(level="ERROR", status_message=type(error).__name__)
                raise

    def callbacks(self) -> list[CallbackHandler]:
        return [ReportCallback(public_key=self.public_key)] if self.client else []

    async def flush(self) -> None:
        if self.client:
            await asyncio.to_thread(self.client.flush)
