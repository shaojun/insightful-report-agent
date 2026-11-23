"""Retained tool steps in collapsed Feishu cards; presentation failures never stop a turn."""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from insightful_report_agent.middleware.look import MAX_IMAGES
from insightful_report_agent.surfaces.feishu.client import FeishuClient, FeishuError
from insightful_report_agent.surfaces.slack.phrasebook import phrase

_LOGGER = logging.getLogger(__name__)
MIN_UPDATE_SECONDS = 3.0
STEPS_PER_GROUP = 25
MAX_CARD_BYTES = 27_000  # Leave room for the API envelope and later status changes.

SOURCE_STEPS = {
    "list_sources": ("正在查看材料清单", "已查看材料清单"),
    "prepare_source_overview": ("正在核对材料预处理结果", "已核对材料预处理结果"),
    "synthesize_source_overview": ("正在综合本轮材料的高层摘要", "已生成材料综合总览"),
    "read_source_evidence": ("正在核对来源证据", "已读取原文证据"),
    "search_web": ("正在搜索公开资料", "已搜索公开资料"),
    "add_source_urls": ("正在收录补充网页", "已收录补充网页"),
    "publish_report": ("正在校验和发送报告", "已发送正式报告"),
    "ls": ("正在查看工作区目录", "已查看工作区目录"),
}
ABSTRACT_LEVELS = {"high": "高层", "middle": "中层", "low": "低层"}


def step_phrase(name: str, args: Mapping[str, Any]) -> tuple[str, str]:
    if name == "read_source_file_abstract":
        level = ABSTRACT_LEVELS.get(str(args.get("level")), "")
        if level == "高层":
            return "正在读取已备好的高层摘要", "已读取高层摘要"
        return f"正在获取{level}摘要（首次需生成）", f"已获取{level}摘要"
    if name == "look":
        paths = args.get("paths")
        images = (
            [p.strip() for p in paths if isinstance(p, str) and p.strip()]
            if isinstance(paths, list)
            else []
        )
        selected = images[:MAX_IMAGES]
        source_images = selected and all(
            path.startswith("/workspace/source-library/") and "/assets/" in path
            for path in (posixpath.normpath(posixpath.join("/workspace", p)) for p in selected)
        )
        kind = "来源附图" if source_images else "图片"
        count = f"（本批 {len(selected)} 张）" if selected else ""
        return f"正在用视觉模型检查{kind}{count}", f"已完成{kind}检查{count}"
    if name == "read_file":
        path = posixpath.normpath(str(args.get("file_path") or ""))
        if path in {
            "/skills/evidence-report/SKILL.md",
            "/workspace/skills/evidence-report/SKILL.md",
        }:
            return "正在读取报告撰写指南", "已读取报告撰写指南"
        if (
            path.startswith("/workspace/source-library/")
            and posixpath.basename(path) == "source.md"
        ):
            return "正在阅读归一化原文", "已阅读归一化原文"
        if path.startswith("/workspace/source-library/overviews/") and path.endswith("/catalog.md"):
            return "正在阅读来源覆盖清单", "已阅读来源覆盖清单"
        if path == "/workspace/tools/source_report.py":
            return "正在阅读报告渲染脚本", "已阅读报告渲染脚本"
    if name == "execute":
        command = str(args.get("command") or "")
        if "/workspace/source-library/" in command and re.match(r"\s*(?:ls|wc)\s", command):
            return "正在查看材料文件", "已查看材料文件"
        if re.match(r"\s*ls(?:\s|$)", command):
            if re.search(r"/workspace/(?:tools|references|brand)(?:/|\s|$)", command):
                return "正在查看报告工具和参考资料", "已查看报告工具和参考资料"
            return "正在查看工作区目录", "已查看工作区目录"
    if name == "publish_report" and args.get("validate_only") is True:
        return "正在校验报告排版与引用", "已校验报告排版与引用"
    return SOURCE_STEPS.get(name) or phrase(name, args)


@dataclass
class Step:
    doing: str
    done: str
    started: float
    ended: float | None = None
    failed: bool = False
    reason: str = ""


def failure_reason(error: object) -> str:
    """Classify evidence into fixed text; tool output may contain secrets or user data."""
    text = str(error).lower()
    for needles, reason in (
        (("cancellederror",), "任务被取消或应用停止"),
        (("timeout", "timed out"), "操作超时"),
        (
            ("permissionerror", "permission denied", "access denied", "forbidden"),
            "权限不足，操作被拒绝",
        ),
        (("filenotfounderror", "no such file", "not found"), "所需文件或资源不存在"),
        (
            ("connectionerror", "connecterror", "connection refused", "connection reset"),
            "连接服务失败",
        ),
        (("rate limit", "ratelimit", "too many requests"), "请求过于频繁，服务限流"),
        (("validationerror", "invalid argument", "invalid parameter"), "工具参数不符合要求"),
    ):
        if any(needle in text for needle in needles):
            return reason
    return "工具报告失败，未提供可安全展示的具体原因"


def duration(seconds: float) -> str:
    if seconds < 1:
        return "<1秒"
    seconds = int(seconds)
    return f"{seconds // 60}分{seconds % 60}秒" if seconds >= 60 else f"{seconds}秒"


class FeishuProgress:
    def __init__(
        self, client: FeishuClient, target: tuple[str, str], *, enabled: bool = True
    ) -> None:
        self.client, self.target = client, target
        self.started = time.monotonic()
        self.steps: list[Step] = []
        self.messages: list[str] = []
        self._sent: list[dict[str, Any]] = []
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task[None] | None = None
        self._closed = False
        self._disabled = not enabled
        self._revision = 0
        self.outcome = ""
        self.phase = "正在准备本轮材料"
        self._page_starts = [0]

    async def start(self) -> None:
        await self._flush()

    def begin_analysis(self) -> None:
        self.phase = "正在分析材料并处理请求"
        self._schedule()

    async def begin(self, name: str, args: Mapping[str, Any] | None = None) -> Step:
        doing, done = step_phrase(name, args or {})
        step = Step(doing[:100], done[:100], time.monotonic())
        self.steps.append(step)
        self._schedule()
        return step

    async def end(
        self,
        step: Step,
        *,
        failed: bool = False,
        error: object = None,
        name: str = "",
        content: object = None,
    ) -> None:
        step.ended, step.failed = time.monotonic(), failed
        if failed:
            step.reason = failure_reason(error)
        elif name in {"read_source_file_abstract", "synthesize_source_overview"}:
            try:
                result = json.loads(content) if isinstance(content, str) else content
            except ValueError:
                result = None
            if isinstance(result, dict):
                if name == "read_source_file_abstract":
                    level = ABSTRACT_LEVELS.get(str(result.get("level")), "")
                    if level and isinstance(result.get("generated"), bool):
                        verb = "生成" if result["generated"] else "读取"
                        step.done = f"已{verb}{level}摘要"
                else:
                    reviewed = result.get("reviewed")
                    if type(reviewed) is int and 0 < reviewed <= 1_000_000:
                        step.done = f"已综合 {reviewed} 份材料的高层摘要"
        self._schedule()

    def _schedule(self) -> None:
        self._revision += 1
        if self._closed or self._disabled or self._pending is not None:
            return
        self._pending = asyncio.create_task(self._later())

    async def _later(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(0, self._last + MIN_UPDATE_SECONDS - time.monotonic()))
                revision = self._revision
                await self._flush()
                if self._closed or self._disabled or revision == self._revision:
                    break
        finally:
            self._pending = None

    @staticmethod
    def _wire_size(card: dict[str, Any]) -> int:
        content = json.dumps(card, ensure_ascii=False)
        return len(json.dumps({"content": content}, ensure_ascii=False).encode())

    def _paginate(self) -> None:
        # Boundaries only move forward: a sent step never migrates to another message.
        while True:
            start = self._page_starts[-1]
            end = min(start + STEPS_PER_GROUP, len(self.steps))
            if end <= start:
                return
            accepted = end
            while end <= len(self.steps):
                candidate = self._card(len(self._page_starts) - 1, start, end, reserve=True)
                if self._wire_size(candidate) > MAX_CARD_BYTES:
                    break
                accepted = end
                if end == len(self.steps):
                    return
                end = min(end + STEPS_PER_GROUP, len(self.steps))
            self._page_starts.append(accepted)

    def card(self, page: int) -> dict[str, Any]:
        self._paginate()
        start = self._page_starts[page]
        end = self._page_starts[page + 1] if page + 1 < len(self._page_starts) else len(self.steps)
        return self._card(page, start, end)

    def _card(self, page: int, start: int, end: int, *, reserve: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        completed = sum(s.ended is not None and not s.failed for s in self.steps)
        failed = sum(s.failed for s in self.steps)
        summary = (
            f"{self.outcome} · 已完成 {completed} 步"
            if self.outcome
            else f"已完成 {completed} 步 · {self.phase}…"
        )
        if failed:
            summary += f" · {failed} 步失败"
        summary += f" · 本轮总耗时 {duration(now - self.started)}"
        if page:
            summary = f"处理步骤续页 {page + 1} · 步骤 {start + 1}–{end}"
        panels = []
        for group_start in range(start, max(start + 1, end), STEPS_PER_GROUP):
            group_end = min(group_start + STEPS_PER_GROUP, end)
            rows = []
            for index in range(group_start, group_end):
                step = self.steps[index]
                icon = "❌" if step.failed else "✅" if step.ended is not None else "⏳"
                text = step.done if step.ended is not None and not step.failed else step.doing
                suffix = " · 失败" if step.failed else ""
                if reserve:
                    text = max((step.doing, step.done), key=lambda x: len(x.encode()))
                    suffix = " · 失败"
                rows.append(
                    f"{index + 1}. {icon} {text}{suffix} · t "
                    f"{duration((step.ended if step.ended is not None else now) - step.started)}"
                )
                if step.failed or reserve:
                    reason = step.reason or failure_reason(None)
                    rows.append(f"   原因：{reason}")
            panels.append(
                {
                    "tag": "collapsible_panel",
                    "element_id": f"progress_steps_{group_start}",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"处理步骤 {group_start + 1}–{group_end}（点击展开）"
                            if end
                            else "处理步骤（点击展开）",
                        },
                        "icon": {
                            "tag": "standard_icon",
                            "token": "down-small-ccm_outlined",
                            "size": "16px 16px",
                        },
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "elements": [
                        {"tag": "markdown", "content": "\n".join(rows) or self.phase + "…"}
                    ],
                }
            )
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": summary}},
            "body": {"elements": panels},
        }

    async def _flush(self) -> None:
        async with self._lock:
            if self._disabled:
                return
            self._last = time.monotonic()
            try:
                self._paginate()
                pages = len(self._page_starts)
                for page in range(pages):
                    card = self.card(page)
                    if page == len(self.messages):
                        message = await asyncio.to_thread(
                            self.client.send, *self.target, "interactive", card
                        )
                        self.messages.append(message)
                        self._sent.append(card)
                    elif card != self._sent[page]:
                        await asyncio.to_thread(self.client.edit_card, self.messages[page], card)
                        self._sent[page] = card
            except FeishuError as error:
                self._disabled = True
                _LOGGER.warning("[feishu] progress card stopped: %s", error)

    async def close(self, *, failed: bool = False) -> None:
        self._closed = True
        if self._pending:
            # Let an in-flight HTTP update finish before the final update.
            await self._pending
        self.outcome = "本轮处理失败" if failed else "本轮处理完成"
        for step in self.steps:
            if step.ended is None:
                step.ended, step.failed = time.monotonic(), True
                step.reason = "本轮已结束，该步骤未返回结果"
        if not failed and any(s.failed for s in self.steps):
            self.outcome = "本轮处理完成（含失败步骤）"
        await self._flush()
