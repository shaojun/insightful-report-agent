"""Self-hosted Feishu ingress and delivery; the official SDK owns the socket event loop.

The agent runs on a separate loop so SDK callbacks acknowledge without waiting for a model,
render or attachment. Events never supply credentials, domains or delivery destinations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import ToolException
from langgraph.types import Command

from insightful_report_agent.config import Settings
from insightful_report_agent.harness import notices
from insightful_report_agent.models.registry import ModelConfigurationError
from insightful_report_agent.runtime.local import LocalRuntime, final_text
from insightful_report_agent.runtime.preprocessor import PreprocessorError, PreprocessStage
from insightful_report_agent.runtime.profiles import thread_id_from_config
from insightful_report_agent.runtime.source_intake import IntakeStage
from insightful_report_agent.surfaces.feishu.client import (
    MAX_DOWNLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    FeishuClient,
    FeishuError,
    FolderAttachmentError,
    identifier,
)
from insightful_report_agent.surfaces.feishu.progress import FeishuProgress, duration
from insightful_report_agent.tools.delivery import (
    DeliveredFile,
    LocalOutputAdapter,
    pending_reports,
)
from insightful_report_agent.tools.files import WorkspaceError, read_bytes, workspace_path

_LOGGER = logging.getLogger(__name__)
_NAMESPACE = ("insightful_report_agent", "feishu", "ingress")
_ABSTRACT_DELIVERY = ("insightful_report_agent", "feishu", "abstract_delivery")
_REPORT_PROGRESS_DELIVERY = ("insightful_report_agent", "feishu", "report_progress_delivery")
_DM_NAMESPACE = ("insightful_report_agent", "feishu", "dm_sessions")
MAX_FILES = 100
MAX_BATCH_BYTES = 256 * 1024**2
RECENT_MESSAGES = 256
MAX_POST_CHARS = 3500
PREVIEW_CHARS = 1600
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _markdown_post(text: str) -> dict:
    return {"zh_cn": {"content": [[{"tag": "md", "text": text}]]}}


def _file_preview(title: str, data: bytes) -> str:
    text = data.decode("utf-8-sig")
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS].rstrip() + "\n… 其余内容见附件"
    # Keep untrusted text inside a literal preview. Attachments retain the original bytes.
    return f"**{title}**\n\n```text\n{text.replace('`', '｀')}\n```"


def _fence_state(text: str, state: tuple[str, int, str] | None) -> tuple[str, int, str] | None:
    """Track fenced code blocks so long replies can be split without breaking them."""
    for line in text.splitlines():
        match = _FENCE.match(line)
        if match is None:
            continue
        marker, info = match.groups()
        if state is None:
            state = (marker[0], len(marker), info.strip())
        elif marker[0] == state[0] and len(marker) >= state[1] and not info.strip():
            state = None
    return state


def _markdown_chunks(text: str, limit: int = MAX_POST_CHARS) -> list[str]:
    """Split long Markdown at natural boundaries and keep fenced blocks valid per message."""
    remaining = text.strip()
    chunks: list[str] = []
    fence: tuple[str, int, str] | None = None
    while len(remaining) > limit:
        split = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split <= 0:
            split = limit
        chunk = remaining[:split].rstrip()
        remaining = (
            remaining[split:].lstrip("\n")
            if remaining[split : split + 1] == "\n"
            else remaining[split + 1 :]
            if remaining[split : split + 1] == " "
            else remaining[split:]
        )
        if not chunk:
            chunk, remaining = remaining[:limit], remaining[limit:]
        next_fence = _fence_state(chunk, fence)
        if fence is not None:
            chunk = f"{fence[0] * fence[1]}{fence[2]}\n{chunk}"
        if next_fence is not None:
            chunk = f"{chunk}\n{next_fence[0] * next_fence[1]}"
        chunks.append(chunk)
        fence = next_fence
    if remaining:
        final_fence = _fence_state(remaining, fence)
        if fence is not None:
            remaining = f"{fence[0] * fence[1]}{fence[2]}\n{remaining}"
        if final_fence is not None:
            remaining = f"{remaining}\n{final_fence[0] * final_fence[1]}"
        chunks.append(remaining)
    return chunks


class FeishuSdkLogFilter(logging.Filter):
    """Expose connection health without the SDK's authenticated socket URLs or payloads."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message.startswith("connected to "):
            safe = "[feishu] long connection established"
        elif message.startswith("disconnected to "):
            safe = "[feishu] long connection closed"
        elif message.startswith("trying to reconnect "):
            safe = "[feishu] reconnecting"
        elif record.levelno >= logging.WARNING:
            safe = "[feishu] SDK transport error; check app credentials and network"
        else:
            return False
        record.msg, record.args = safe, ()
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def thread_id_for(tenant: str, chat: str, root: str) -> str:
    return "feishu:" + ":".join(identifier(value) for value in (tenant, chat, root))


def target_of(thread: str, tenant: str) -> tuple[str, str] | None:
    parts = thread.split(":")
    if len(parts) != 4 or parts[:2] != ["feishu", tenant]:
        return None
    root = identifier(parts[3])
    return identifier(parts[2]), "dm" if root.startswith("dm_") else root


class FeishuFileDelivery:
    max_file_bytes = MAX_UPLOAD_BYTES

    def __init__(self, client: FeishuClient, tenant: str, local: LocalOutputAdapter) -> None:
        self.client, self.tenant, self.local = client, tenant, local

    def deliver(self, thread_id: str, files: Sequence[DeliveredFile]) -> None:
        target = target_of(thread_id, self.tenant)
        if target is None:
            raise FeishuError("wrong_delivery_target")
        if any(not f.content or len(f.content) > MAX_UPLOAD_BYTES for f in files):
            raise FeishuError("file_must_be_between_1_byte_and_30_MiB")
        self.local.deliver(thread_id, files)
        for item in files:
            # A partial batch retry reuses each message's UUID (Feishu's deduplication window).
            uuid = hashlib.sha256(
                thread_id.encode() + item.name.encode() + hashlib.sha256(item.content).digest()
            ).hexdigest()[:32]
            self.client.send_file(*target, item.name, item.content, uuid=uuid)


def message_content(message: Mapping[str, Any], bot: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Return text and (resource key, safe name, resource type), never arbitrary URLs."""
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except ValueError:
        raise FeishuError("invalid_message_content") from None
    if not isinstance(content, dict):
        raise FeishuError("invalid_message_content")
    kind = message.get("message_type")
    files: list[tuple[str, str, str]] = []
    text = ""
    if kind == "text":
        text = str(content.get("text") or "")
    elif kind == "folder" or (kind == "file" and content.get("is_folder") is True):
        key = identifier(str(content.get("file_key") or ""))
        files.append((key, str(content.get("file_name") or key), "folder"))
    elif kind in ("file", "image", "audio", "media"):
        image = kind == "image"
        key = str(content.get("image_key" if image else "file_key") or "")
        # Native video events may omit file_name; keep a video suffix so read_file
        # can select its decoder rather than treating the attachment as generic bytes.
        suffix = ".png" if image else ".mp4" if kind == "media" else ".bin"
        default = f"{key}{suffix}"
        files.append(
            (
                identifier(key),
                str(content.get("file_name") or default),
                "image" if image else "file",
            )
        )
    elif kind == "post":
        # Received posts normally contain title/content directly; tolerate localized posts too.
        if "content" not in content:
            content = next((v for v in content.values() if isinstance(v, dict)), {})
        lines = [str(content.get("title") or "")]
        for row in content.get("content") or []:
            words: list[str] = []
            for node in row:
                tag = node.get("tag")
                if tag in ("text", "a"):
                    words.append(str(node.get("text") or node.get("href") or ""))
                    if tag == "a" and node.get("href") and node.get("href") != node.get("text"):
                        words.append(f" ({node['href']})")
                elif tag == "at" and node.get("user_id") != bot:
                    words.append(str(node.get("user_name") or "@user"))
                elif tag == "img":
                    key = identifier(str(node.get("image_key") or ""))
                    files.append((key, f"{key}.png", "image"))
            lines.append("".join(words))
        text = "\n".join(lines)
        # The client can send a single post bubble with text and a separate files list.
        # These resources do not appear as nodes in the post's content rows.
        for resource in content.get("files") or []:
            if not isinstance(resource, dict):
                raise FeishuError("invalid_message_content")
            key = identifier(str(resource.get("file_key") or ""))
            if resource.get("is_folder") is True:
                files.append((key, str(resource.get("file_name") or key), "folder"))
            else:
                files.append((key, str(resource.get("file_name") or f"{key}.bin"), "file"))
    else:
        raise FeishuError("unsupported_message_type")
    for mention in message.get("mentions") or []:
        if (mention.get("id") or {}).get("open_id") == bot:
            key = str(mention.get("key") or "")
            if key:
                text = text.replace(key, "")
    if len(files) > MAX_FILES:
        raise FeishuError("too_many_attachments")
    return text.strip(), files


@dataclass
class FeishuAdapter:
    client: FeishuClient
    app_id: str
    tenant: str
    bot: str
    enable_progress: bool = True
    runtime: LocalRuntime | None = None
    store: Any = None
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _progress: dict[str, FeishuProgress] = field(default_factory=dict)
    _progress_delivery_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _running_turns: set[str] = field(default_factory=set)

    def accepts(self, payload: Mapping[str, Any]) -> bool:
        return self.rejection_reason(payload) is None

    def rejection_reason(self, payload: Mapping[str, Any]) -> str | None:
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        message = event.get("message") or {}
        checks = (
            (header.get("event_type") == "im.message.receive_v1", "unsupported_event"),
            (header.get("app_id") == self.app_id, "app_mismatch"),
            (header.get("tenant_key") == self.tenant, "tenant_mismatch"),
            (sender.get("sender_type") == "user", "sender_not_user"),
            (sender.get("tenant_key", self.tenant) == self.tenant, "sender_tenant_mismatch"),
            (bool((sender.get("sender_id") or {}).get("open_id")), "missing_sender"),
            (message.get("chat_type") in ("p2p", "group"), "unsupported_chat_type"),
            (
                bool(message.get("message_id") and message.get("chat_id")),
                "missing_message_identity",
            ),
        )
        return next((reason for valid, reason in checks if not valid), None)

    def _task(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning(
                "[feishu] background operation failed: %s", type(task.exception()).__name__
            )

    def dispatch(self, payload: Mapping[str, Any]) -> None:
        reason = self.rejection_reason(payload)
        if reason is not None:
            _LOGGER.warning("[feishu] message event ignored: %s", reason)
            return
        _LOGGER.warning("[feishu] message event accepted")
        self._task(self.handle(payload))

    async def _direct_thread(self, chat: str, mid: str, *, reset: bool) -> str | None:
        key = thread_id_for(self.tenant, chat, "dm")
        # Route independently of the old agent turn, including for /new.
        async with self._locks.setdefault("route:" + key, asyncio.Lock()):
            saved = await self.store.aget(_DM_NAMESPACE, key)
            state = dict(saved.value) if saved is not None else {}
            recent = list(state.get("messages") or [])
            if mid in recent:
                return None
            root = "dm_" + mid if reset else str(state.get("root") or "dm")
            await self.store.aput(
                _DM_NAMESPACE,
                key,
                {"root": root, "messages": (recent + [mid])[-RECENT_MESSAGES:]},
            )
            thread = thread_id_for(self.tenant, chat, root)
            if reset:
                await self.post(
                    thread,
                    "已开始新会话。请发送新的任务和所需文件。旧会话保留，工作文件按服务保留期清理；正在执行的旧任务会继续完成。",
                )
                return None
            return thread

    async def handle(self, payload: Mapping[str, Any]) -> None:
        if self.runtime is None:
            raise RuntimeError("wire the runtime before serving")
        message = payload["event"]["message"]
        sender = payload["event"].get("sender") or {}
        sender_id = (sender.get("sender_id") or {}).get("open_id")
        chat, mid = identifier(message["chat_id"]), identifier(message["message_id"])
        direct = message["chat_type"] == "p2p"
        root = "dm" if direct else identifier(message.get("root_id") or mid)
        content_error = None
        try:
            text, resources = message_content(message, self.bot)
        except FeishuError as error:
            content_error = error
            text, resources = "", []
        thread = (
            await self._direct_thread(
                chat,
                mid,
                reset=message.get("message_type") == "text" and text == "/new",
            )
            if direct
            else thread_id_for(self.tenant, chat, root)
        )
        if thread is None:
            return
        if direct and resources and thread in self._running_turns:
            self.post_soon(
                thread,
                "当前任务正在处理。新附件会在本轮结束后加入当前会话，可用于后续修订。"
                "如要写一份独立报告，请先发送 `/new`，再重新发送附件和报告要求。",
            )
        async with self._locks.setdefault(thread, asyncio.Lock()):
            saved = await self.store.aget(_NAMESPACE, thread)
            state = dict(saved.value) if saved is not None else {}
            mentioned = any(
                (m.get("id") or {}).get("open_id") == self.bot
                for m in message.get("mentions") or []
            )
            if not direct and not mentioned and not state:
                _LOGGER.warning("[feishu] group message ignored: no mention or known topic")
                return
            recent = list(state.get("messages") or [])
            if mid in recent:
                _LOGGER.warning("[feishu] duplicate message ignored")
                return
            # Persist before invoking the graph: retries/restarts cannot start a second run.
            await self.store.aput(
                _NAMESPACE, thread, {"messages": (recent + [mid])[-RECENT_MESSAGES:]}
            )
            progress = None
            try:
                try:
                    await asyncio.to_thread(self.client.react, mid, "Get")
                except FeishuError as error:
                    # A receipt reaction is presentation only; it must never block the work.
                    _LOGGER.warning("[feishu] receipt reaction unavailable: %s", error)
                with self.runtime.trace.request(thread, "feishu", sender_id):
                    if content_error is not None:
                        raise content_error
                    if any(kind == "folder" for _, _, kind in resources):
                        settings = self.runtime.settings
                        if settings.source_input_mode != "legacy" and not (
                            settings.preprocessor_api_url and settings.preprocessor_api_key
                        ):
                            raise FolderAttachmentError("folder_preprocessor_not_configured")
                        if self.enable_progress:
                            try:
                                await self.post(
                                    thread, "已收到文件夹，正在读取目录和下载其中的文件。"
                                )
                            except FeishuError as error:
                                _LOGGER.warning("[feishu] folder receipt unavailable: %s", error)
                    if text or not resources:
                        progress = FeishuProgress(
                            self.client, (chat, root), enabled=self.enable_progress
                        )
                        self._progress[thread] = progress
                        await progress.start()
                    attached: list[tuple[str, bytes]] = []
                    remaining_bytes = MAX_BATCH_BYTES
                    for key, name, kind in resources:
                        if remaining_bytes <= 0:
                            raise FeishuError("attachment_batch_too_large")
                        limit = min(MAX_DOWNLOAD_BYTES, remaining_bytes)
                        if kind == "folder":
                            name, data = await asyncio.to_thread(
                                self.client.folder_zip, mid, key, name, max_bytes=limit
                            )
                        else:
                            data = await asyncio.to_thread(
                                self.client.download, mid, key, kind, max_bytes=limit
                            )
                        attached.append((name, data))
                        remaining_bytes -= len(data)
                    if attached:

                        async def on_preprocess(stage: PreprocessStage, elapsed: float) -> None:
                            await self.notify_preprocessor(thread, stage, elapsed)

                        await self.runtime.attach(
                            thread,
                            attached,
                            on_preprocess=on_preprocess if self.enable_progress else None,
                        )
                        if not text:
                            return
                    reports: dict[str, Callable[[], Awaitable[None]]] = {}
                    queue_token = pending_reports.set(reports)
                    self._running_turns.add(thread)
                    finalization_failed = False
                    try:
                        added = await self.runtime.ask(text or "(attachment)", thread_id=thread)
                        summary = final_text(added)
                    except Exception as error:
                        if not reports:
                            raise
                        _LOGGER.warning("[feishu] report finalization failed: %s", type(error).__name__)
                        finalization_failed = True
                        summary = "本轮收尾未完成，已生成的 HTML 报告将在下方发送。"
                    finally:
                        self._running_turns.discard(thread)
                        pending_reports.reset(queue_token)
                    try:
                        await self.post(thread, summary)
                    except FeishuError:
                        if not reports:
                            raise
                        _LOGGER.warning("[feishu] report summary could not be sent")
                    if progress is not None:
                        await progress.close(failed=finalization_failed)
                    if reports:
                        config_token = var_child_runnable_config.set(
                            {"configurable": {"thread_id": thread}}
                        )
                        try:
                            for send in reports.values():
                                await send()
                        finally:
                            var_child_runnable_config.reset(config_token)
            except asyncio.CancelledError:
                if progress is not None:
                    await progress.close(failed=True)
                raise
            except Exception as error:  # noqa: BLE001 - never echo payloads or provider errors
                reason = str(error) if isinstance(error, FeishuError) else type(error).__name__
                _LOGGER.warning("[feishu] turn failed: %s", reason)
                if progress is not None:
                    await progress.close(failed=True)
                if isinstance(error, PreprocessorError):
                    await self.post(thread, "外部预处理未返回可用的资料包。请重新发送附件以重试。")
                elif isinstance(error, ToolException):
                    await self.post(thread, "报告已生成，但未能确认 HTML 附件送达。")
                elif reason == "too_many_attachments":
                    await self.post(
                        thread,
                        f"每条消息最多可发送 {MAX_FILES} 个附件。请分批上传，最后发送报告要求。",
                    )
                elif reason in {
                    "attachment_batch_too_large",
                    "attachment_too_large",
                    "folder_too_large",
                }:
                    await self.post(
                        thread,
                        f"附件大小超限。单个文件或文件夹最多 {MAX_DOWNLOAD_BYTES // 1024**2} MiB，"
                        f"每条消息合计最多 {MAX_BATCH_BYTES // 1024**2} MiB。请拆分后上传。",
                    )
                elif reason == "folder_preprocessor_not_configured":
                    await self.post(
                        thread,
                        "当前服务未启用原始资料预处理。请上传预处理完成的结果 ZIP 和报告要求。",
                    )
                elif reason == "empty_folder":
                    await self.post(thread, "这个文件夹中没有可下载的文件。请补充资料后重新发送。")
                elif isinstance(error, FolderAttachmentError):
                    await self.post(
                        thread,
                        "未能完整读取飞书文件夹。请将文件夹压缩为 ZIP，并和原来的报告要求一起重新发送。",
                    )
                else:
                    await self.post(
                        thread, "处理失败，附件可能未读取或回复未送达。请重新发送消息重试。"
                    )
            finally:
                self._progress.pop(thread, None)

    async def notify_preprocessor(
        self, thread: str, stage: PreprocessStage, elapsed: float
    ) -> None:
        if stage == "started":
            message = (
                "即将把附件提交给外部预处理服务，进行文档解析和材料摘要。大批量资料可能需要几分钟。"
            )
        else:
            status = "完成" if stage == "succeeded" else "部分完成"
            message = f"外部预处理{status}，结果资料包已下载。耗时 {duration(elapsed)}，正在核对材料索引。"
        await self.post(thread, message)

    async def notify_source_intake(self, thread: str, stage: IntakeStage, rows: list[dict]) -> None:
        messages = {
            "normalizing": "已收到附件，正在解析并生成归一化 Markdown。可以继续上传或发送报告目标。",
            "summarizing": "已有材料完成归一化，正在生成高层摘要。",
            "failed": "附件处理未完成。请重新发送附件以重试。",
        }
        if stage != "prepared":
            if self.enable_progress:
                await self.post(thread, messages[stage])
            return

        target = target_of(thread, self.tenant)
        if target is None or self.runtime is None:
            return
        if not self.runtime.components.source_library.legacy:
            failures = [row for row in rows if "error" in row]
            message = f"预处理资料包检查完成。可用材料 {len(rows) - len(failures)} 项，失败 {len(failures)} 项。"
            indexes = sorted({row["wiki_index"] for row in rows if row.get("wiki_index")})
            if failures and not indexes:
                message += " 未找到通过检查的 wiki-index.md，请重新提交资料。"
            await self.post(thread, message)
            for index in indexes:
                await self._send_wiki_index(thread, target, index)
            return
        backend = self.runtime.binding.sandbox
        namespace = (*_ABSTRACT_DELIVERY, thread)
        ready = delivered = failed_to_send = 0
        for row in rows:
            if "error" in row:
                continue
            ready += 1
            path = row["path"]
            delivery_id = hashlib.sha256(path.encode()).hexdigest()
            try:
                if await self.store.aget(namespace, delivery_id) is not None:
                    continue
                data = await read_bytes(backend, path, cap_mb=MAX_UPLOAD_BYTES // 1024**2)
                uuid = hashlib.sha256(
                    thread.encode() + path.encode() + hashlib.sha256(data).digest()
                ).hexdigest()[:32]
                await asyncio.to_thread(
                    self.client.send_file,
                    *target,
                    PurePosixPath(path).name + ".md",
                    data,
                    uuid=uuid,
                )
                await self.store.aput(namespace, delivery_id, {"sent": True})
                delivered += 1
            except Exception as error:  # noqa: BLE001 - keep going for the remaining files
                failed_to_send += 1
                _LOGGER.warning("[feishu] abstract delivery failed: %s", type(error).__name__)
        failed_to_prepare = len(rows) - ready
        if self.enable_progress or failed_to_prepare or failed_to_send:
            message = (
                f"附件处理结束。{ready} 项完成归一化和高层摘要，本次发送 {delivered} 份 Markdown。"
                if rows
                else "未找到可处理的附件，请重新上传。"
            )
            if failed_to_prepare:
                message += f" {failed_to_prepare} 项未能完成处理。"
            if failed_to_send:
                message += f" {failed_to_send} 份摘要未能发回，可以重新上传对应文件重试。"
            if thread in self._progress:
                message += " 已收到你的文字请求，接下来会继续处理。"
            await self.post(thread, message)

    async def _send_wiki_index(self, thread: str, target: tuple[str, str], path: str) -> None:
        try:
            data = await read_bytes(
                self.runtime.binding.sandbox, path, cap_mb=MAX_UPLOAD_BYTES // 1024**2
            )
        except Exception as error:  # noqa: BLE001 - index delivery must not block the report
            _LOGGER.warning("[feishu] wiki index read failed: %s", type(error).__name__)
            await self.post(thread, "无法读取材料索引文件，已导入的材料可以继续用于报告。")
            return
        try:
            uuid = hashlib.sha256(
                thread.encode() + path.encode() + hashlib.sha256(data).digest()
            ).hexdigest()[:32]
            await asyncio.to_thread(
                self.client.send_file, *target, "wiki-index.md", data, uuid=uuid
            )
        except Exception as error:  # noqa: BLE001 - index delivery must not block the report
            _LOGGER.warning("[feishu] wiki index delivery failed: %s", type(error).__name__)
            await self.post(thread, "材料索引附件发送失败，已导入的材料可以继续用于报告。")

    async def send_report_progress(self, thread: str, file_path: str) -> None:
        if (
            not self.enable_progress
            or self.runtime is None
            or not file_path.startswith("/workspace/projects/")
        ):
            return
        try:
            path = workspace_path(file_path)
            target = target_of(thread, self.tenant)
        except (WorkspaceError, FeishuError):
            return
        if target is None or not (
            path.startswith("/workspace/projects/") and path.endswith("/report-progress.md")
        ):
            return

        namespace = (*_REPORT_PROGRESS_DELIVERY, thread)
        async with self._progress_delivery_locks.setdefault(thread, asyncio.Lock()):
            try:
                token = var_child_runnable_config.set({"configurable": {"thread_id": thread}})
                try:
                    data = await read_bytes(self.runtime.binding.sandbox, path, cap_mb=1)
                finally:
                    var_child_runnable_config.reset(token)
                if not data.strip():
                    return
                saved = await self.store.aget(namespace, path)
                previous = saved.value if saved else {}
                digest = hashlib.sha256(data).hexdigest()
                if previous.get("digest") == digest:
                    return
                revision = previous.get("revision", 0) + 1
                preview = _file_preview(f"报告写作进度 · 第 {revision} 次更新", data)
                uuid = hashlib.sha256(
                    f"{thread}\0{path}\0{revision}\0{digest}".encode()
                ).hexdigest()[:31]
                await asyncio.to_thread(
                    self.client.send,
                    *target,
                    "post",
                    _markdown_post(preview),
                    uuid=f"{uuid}0",
                )
                await asyncio.to_thread(
                    self.client.send_file, *target, "report-progress.md", data, uuid=f"{uuid}1"
                )
                await self.store.aput(namespace, path, {"digest": digest, "revision": revision})
            except Exception as error:  # noqa: BLE001 - a notification cannot stop report writing
                _LOGGER.warning(
                    "[feishu] report progress delivery failed: %s", type(error).__name__
                )

    async def post(self, thread: str, text: str) -> None:
        target = target_of(thread, self.tenant)
        if target is None or not text or not notices.should_post(text):
            return
        for chunk in _markdown_chunks(text):
            await asyncio.to_thread(
                self.client.send,
                *target,
                "post",
                _markdown_post(chunk),
            )

    def post_soon(self, thread: str, text: str) -> None:
        self._task(self.post(thread, text))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class FeishuProgressMiddleware(AgentMiddleware):
    def __init__(self, adapter: FeishuAdapter) -> None:
        super().__init__()
        self.adapter = adapter

    async def abefore_agent(
        self,
        state: AgentState[Any],  # noqa: ARG002 - hook signature
        runtime: Any,
    ) -> None:
        if progress := self.adapter._progress.get(thread_id_from_config(runtime)):
            progress.begin_analysis()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        thread = thread_id_from_config(request.runtime)
        progress = self.adapter._progress.get(thread)
        name = str(request.tool_call.get("name") or "") if request.tool is not None else ""
        args = request.tool_call.get("args") or {}
        step = await progress.begin(name, args) if progress is not None else None
        try:
            result = await handler(request)
        except BaseException as error:
            if progress is not None and step is not None:
                await progress.end(step, failed=True, error=type(error).__name__)
            raise
        if progress is not None and step is not None:
            await progress.end(
                step,
                failed=isinstance(result, ToolMessage) and result.status == "error",
                error=result.content if isinstance(result, ToolMessage) else None,
                name=name,
                content=result.content if isinstance(result, ToolMessage) else None,
            )
        path = args.get("file_path")
        if (
            name in {"write_file", "edit_file"}
            and isinstance(path, str)
            and isinstance(result, ToolMessage)
            and result.status == "success"
        ):
            await self.adapter.send_report_progress(thread, path)
        return result


async def _worker(
    project_root: Path,
    settings: Settings,
    client: FeishuClient,
    ready: Future[tuple[FeishuAdapter, asyncio.AbstractEventLoop, asyncio.Event]],
) -> None:
    from insightful_report_agent.runtime import maintenance
    from insightful_report_agent.runtime.local import build_local_runtime
    from insightful_report_agent.runtime.persistence import open_persistence, workspace_of
    from insightful_report_agent.tools.mcp import load_mcp_tools, servers_for_runtime
    from insightful_report_agent.tools.paper import PaperPolicy, admit_paper_tools

    try:
        bot = await asyncio.to_thread(client.identity)
        adapter = FeishuAdapter(
            client,
            str(settings.feishu_app_id),
            str(settings.feishu_tenant_key),
            bot,
            enable_progress=settings.enable_progress,
        )
        servers = servers_for_runtime(project_root, settings, "self_hosted")
        loaded = await load_mcp_tools(servers)
        servers = [s for s in servers if s.name not in loaded.failed]
        tools = admit_paper_tools(loaded.tools, PaperPolicy.from_settings(settings))
        workspace = workspace_of(settings, project_root)
        async with open_persistence(settings, workspace) as (checkpointer, store):
            runtime = build_local_runtime(
                settings,
                project_root,
                checkpointer=checkpointer,
                store=store,
                replies=adapter.post_soon,
                mcp_servers=servers,
                mcp_tools=tools,
                surface="feishu",
                delivery=FeishuFileDelivery(
                    client, adapter.tenant, LocalOutputAdapter(workspace / "out")
                ),
                surface_middleware=(FeishuProgressMiddleware(adapter),)
                if settings.enable_progress
                else (),
                source_intake_notify=adapter.notify_source_intake,
            )
            adapter.runtime, adapter.store = runtime, store
            keeper = None
            try:
                prefix = f"feishu:{adapter.tenant}:"
                await maintenance.start(runtime, store, thread_prefix=prefix)
                keeper = asyncio.create_task(maintenance.run(runtime, store, thread_prefix=prefix))
                stop = asyncio.Event()
                if ready.cancelled():
                    return
                ready.set_result((adapter, asyncio.get_running_loop(), stop))
                await stop.wait()
            finally:
                if keeper is not None:
                    keeper.cancel()
                    await asyncio.gather(keeper, return_exceptions=True)
                await adapter.aclose()
                await runtime.aclose()
                if runtime.binding.runner is not None:
                    runtime.binding.runner.stop_all()
    except BaseException as error:
        if not ready.done():
            ready.set_exception(error)
        raise


def serve(project_root: Path, settings: Settings) -> None:
    for name in ("feishu_app_id", "feishu_app_secret", "feishu_tenant_key"):
        if not getattr(settings, name):
            raise SystemExit(f"{name.upper()} is not set; see docs/feishu.md")
    try:
        import lark_oapi as lark
    except ImportError:
        raise SystemExit("Install Feishu support: uv sync --extra feishu") from None

    if settings.feishu_app_secret is None:
        raise SystemExit("FEISHU_APP_SECRET is not set")
    secret = settings.feishu_app_secret.get_secret_value()
    client = FeishuClient(str(settings.feishu_app_id), secret)
    ready: Future[tuple[FeishuAdapter, asyncio.AbstractEventLoop, asyncio.Event]] = Future()
    sdk_logger = logging.getLogger("Lark")
    sdk_filter = FeishuSdkLogFilter()
    previous_level = sdk_logger.level
    sdk_logger.addFilter(sdk_filter)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="feishu-agent") as pool:
            worker = pool.submit(asyncio.run, _worker(project_root, settings, client, ready))
            try:
                adapter, loop, stop = ready.result()

                def receive(data: Any) -> None:
                    payload = json.loads(lark.JSON.marshal(data))
                    loop.call_soon_threadsafe(adapter.dispatch, payload)

                handler = (
                    lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(receive)
                    .build()
                )
                socket = lark.ws.Client(
                    str(settings.feishu_app_id),
                    secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.INFO,
                )
                _LOGGER.warning("[feishu] starting long connection; agent runtime ready")
                socket.start()
            finally:
                if not ready.cancel() and ready.exception() is None:
                    _, loop, stop = ready.result()
                    loop.call_soon_threadsafe(stop.set)
                worker.result()
    except KeyboardInterrupt:
        pass
    except ModelConfigurationError:
        raise SystemExit(
            "Feishu model configuration failed; run uv run insightful-report-agent doctor"
        ) from None
    except Exception as error:  # noqa: BLE001 - startup errors can contain credentials
        raise SystemExit(
            f"Feishu startup failed ({type(error).__name__}); check app settings"
        ) from None
    finally:
        client.close()
        sdk_logger.removeFilter(sdk_filter)
        sdk_logger.setLevel(previous_level)
