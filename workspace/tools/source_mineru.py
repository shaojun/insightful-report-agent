"""Import MinerU Markdown, position metadata and visual assets."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from source_normalize import Document, atomic, decode

IMAGE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)|<img\b[^>]*\bsrc=["\x27]([^"\x27]+)', re.IGNORECASE)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_ARCHIVE = 800 * 1024**2
MAX_MARKDOWN = 30 * 1024**2


def image_paths(markdown: str) -> list[str]:
    return [match.group(1) or match.group(2) for match in IMAGE.finditer(markdown)]


def item_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [line for item in value for line in item_text(item)]
    if isinstance(value, dict):
        return [
            line
            for key, item in value.items()
            if key not in {"type", "page_idx", "img_path", "bbox", "id", "text_level"}
            for line in item_text(item)
        ]
    return []


def read_mineru(doc: Document, path: Path) -> None:
    try:
        with ZipFile(path) as archive, tempfile.TemporaryDirectory(dir=doc.bundle) as temporary:
            consume(doc, archive, Path(temporary))
    except (BadZipFile, RuntimeError, NotImplementedError):
        raise ValueError("MinerU result archive is invalid") from None


def consume(doc: Document, archive: ZipFile, temporary: Path) -> None:
    members = {item.filename: item for item in archive.infolist() if not item.is_dir()}
    if len(members) > 5000 or sum(item.file_size for item in members.values()) > MAX_ARCHIVE:
        raise ValueError("MinerU result archive exceeds the expanded size or file count limit")

    def read(name: str, cap: int) -> bytes:
        member = members[name]
        if member.file_size > cap:
            raise ValueError("MinerU result component exceeds its size limit")
        with archive.open(member) as handle:
            payload = handle.read(cap + 1)
        if len(payload) > cap:
            raise ValueError("MinerU result component exceeds its size limit")
        return payload

    md = next((name for name in members if PurePosixPath(name).name == "full.md"), None)
    if md is None:
        md = next((name for name in members if name.lower().endswith(".md")), None)
    if md is None:
        raise ValueError("MinerU result has no Markdown")
    markdown = decode(read(md, MAX_MARKDOWN))
    index = next(
        (
            name
            for name in members
            if name.endswith(("_content_list.json", "/content_list.json"))
            or name == "content_list.json"
        ),
        None,
    )
    base = PurePosixPath(md).parent
    assets = {name for name in members if PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS}
    installed = set()

    def add_image(reference: str, locator: str, *, overwrite_refs: bool = False) -> None:
        name = unquote(reference.strip().strip("<>"))
        if urlsplit(name).scheme or name.startswith("//"):
            doc.warnings.append("A MinerU visual refers to an external URL and was not embedded.")
            return
        preferred = str(base / name)
        path = next((candidate for candidate in (preferred, name) if candidate in assets), None)
        if path is None:
            matches = [
                candidate
                for candidate in assets
                if PurePosixPath(candidate).name == PurePosixPath(name).name
            ]
            path = matches[0] if len(matches) == 1 else None
        if path is None or path in installed:
            return
        asset = temporary / ("image" + PurePosixPath(path).suffix)
        try:
            asset.write_bytes(read(path, 30 * 1024**2))
            doc.image(asset, locator, block=overwrite_refs)
            installed.add(path)
        except (OSError, ValueError):
            doc.warnings.append(f"MinerU visual could not be decoded at {locator}")

    rows = json.loads(read(index, MAX_MARKDOWN)) if index else None
    if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
        for offset, row in enumerate(rows, start=1):
            idx = row.get("page_idx")
            label = f"Page {idx + 1}" if isinstance(idx, int) and idx >= 0 else "MinerU extraction"
            kind = str(row.get("type", "block"))
            locator = f"{label} · {kind} block {offset}"
            value = "\n\n".join(item_text(row))
            doc.block(locator, value)
            if isinstance(row.get("img_path"), str):
                add_image(row["img_path"], locator)
            for reference in image_paths(value):
                add_image(reference, locator)
    if not doc.blocks or not any("Page " in row["locator"] for row in doc.blocks):
        doc.block("MinerU Markdown", markdown, lines=True)
        doc.warnings.append(
            "This MinerU result uses extracted Markdown line positions where source page metadata is unavailable."
        )
    for image in image_paths(markdown):
        add_image(image, f"MinerU Markdown asset {PurePosixPath(image).name}", overwrite_refs=True)
    for item in sorted(assets - installed):
        raw = PurePosixPath(item).name
        match = re.match(r"page_(\d+)_", raw)
        locator = (
            f"Page {int(match[1]) + 1} · extracted visual"
            if match
            else f"MinerU extracted visual {raw}"
        )
        add_image(item, locator, overwrite_refs=True)
    # Keep the provider Markdown and its relative assets independently viewable.
    original_dir = doc.bundle / "mineru"
    for key in installed:
        member = PurePosixPath(key)
        try:
            relative = member.relative_to(base)
        except ValueError:
            continue
        if relative.is_absolute() or ".." in relative.parts or "\\" in key:
            continue
        destination = original_dir / str(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(read(key, 30 * 1024**2))
    atomic(original_dir / "full.md", markdown)
