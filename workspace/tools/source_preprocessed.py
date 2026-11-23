"""Read the Bulk Reference Files Preprocessor's generated archive artifacts."""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT_INDEX = "wiki-index.md"
INDEX_LINK = re.compile(r"^- \[.*\]\(([^()]+)\)$")
ARTIFACT_LINK = re.compile(r" · \[(摘要|正文)\]\(([^()]+)\)")
IMAGE = re.compile(r"!\[[^\]\n]*\]\(<([^>\n]+)>\)|!\[[^\]\n]*\]\(([^\s()]+)\)")
ESCAPE = re.compile(r"\\([\\`*_{}\[\]()<>#!|])")
TOTALS = re.compile(r"已完成 (\d+) 个文件 · 处理失败 (\d+) 个文件 · 未能接收 (\d+) 个来源")


def source_url(export: Path) -> str | None:
    with export.open("r", encoding="utf-8-sig", errors="replace") as file:
        header = file.readline(4096)
    if match := re.fullmatch(r"来源：<(https?://[^<>\s]+)>\s*", header):
        try:
            parsed = urlsplit(match[1])
            if parsed.hostname and not parsed.username and not parsed.password:
                return match[1]
        except ValueError:
            pass
    return None


def target(page: str, link: str) -> str:
    try:
        parsed = urlsplit(link)
    except ValueError:
        return ""
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    path = unquote(parsed.path)
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return ""
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
    return "" if relative == ".." or relative.startswith("../") else relative


def index_rows(members: dict[str, Path]) -> list[dict]:
    """Read the producer's root and linked pages without following external links."""
    root = members[ROOT_INDEX].read_text(encoding="utf-8-sig")
    totals = TOTALS.search(root)
    if not totals:
        raise ValueError("The preprocessor index is missing its coverage totals")
    ready, failed, rejected = map(int, totals.groups())
    pending = [(ROOT_INDEX, root)]
    visited = {ROOT_INDEX}
    rows = []
    identities = set()
    for page, content in pending:
        rejected_section = False
        for line in content.splitlines():
            if line.startswith("## "):
                rejected_section = page == ROOT_INDEX and line == "## 未能接收的来源"
            if link := INDEX_LINK.fullmatch(line):
                child = target(page, link[1])
                if (
                    not re.fullmatch(r"_preprocessor/index/\d+\.md", child)
                    or child in visited
                    or child not in members
                ):
                    raise ValueError("The preprocessor index has a missing or invalid linked page")
                visited.add(child)
                pending.append((child, members[child].read_text(encoding="utf-8-sig")))
                continue
            if not line.startswith("- ") or " — " not in line:
                continue
            links = list(ARTIFACT_LINK.finditer(line))
            artifacts = {match[1]: target(page, match[2]) for match in links}
            entry = ESCAPE.sub(r"\1", line[2 : links[0].start() if links else len(line)])
            name, _, synopsis = entry.partition(" — ")
            if artifacts:
                member = artifacts.get("正文") or artifacts.get("摘要")
                suffix = ".export.md" if artifacts.get("正文") else ".brief.md"
                if not member or not member.endswith(suffix):
                    raise ValueError("The preprocessor index has an invalid artifact link")
                name = member.removesuffix(suffix)
                synopsis = entry.removeprefix(name + " — ")
                for key, value in artifacts.items():
                    ending = ".export.md" if key == "正文" else ".brief.md"
                    if value != name + ending or value not in members:
                        raise ValueError("The preprocessor index references a missing artifact")
            state = "rejected" if rejected_section else "source"
            identity = state, name
            if not name or identity in identities:
                raise ValueError("The preprocessor index has a duplicate or empty source")
            identities.add(identity)
            row = {"name": name, "kind": state, "synopsis": synopsis}
            if state == "source" and all(key in artifacts for key in ("摘要", "正文")):
                row.update(export=artifacts["正文"], brief=artifacts["摘要"])
            else:
                row["error"] = row["synopsis"] or "Preprocessing did not produce both artifacts"
            rows.append(row)
    actual = (
        sum("export" in row for row in rows),
        sum(row["kind"] == "source" and "error" in row for row in rows),
        sum(row["kind"] == "rejected" for row in rows),
    )
    if actual != (ready, failed, rejected):
        raise ValueError("The preprocessor index does not match its artifact coverage totals")
    return rows


def images(record: dict, text: str):
    """Resolve only relative images in this archive's producer-assigned asset directory."""
    export = Path(record["path"])
    member = record["member"]
    root = export.parents[member.count("/")]
    allowed = (
        root / "_preprocessor" / "assets" / hashlib.sha256(record["name"].encode()).hexdigest()
    )
    for line, content in enumerate(text.splitlines(), 1):
        for match in IMAGE.finditer(content):
            relative = target(member, match[1] or match[2])
            if relative:
                path = root / relative
                if (
                    path.is_file()
                    and path.resolve().is_relative_to(allowed)
                    and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                ):
                    yield line, path
                    continue
            yield line, None
