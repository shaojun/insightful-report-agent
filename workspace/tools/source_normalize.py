"""Thread-local source inventory and parsers. Only this script sees the original bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from source_preprocessed import ROOT_INDEX, images, index_rows, source_url

ROOT = Path("/workspace")
CACHE = ROOT / "source-library"
ATTACHMENTS = ROOT / "attachments"
MAX_BYTES = 200 * 1024**2
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_ARCHIVE_FILES = 750
MAX_VISUAL_PAGES = 150
BLOCK_SIZE = 3200
# Keep existing raw-source cache paths available when operators opt into legacy mode.
LEGACY_RECIPE = "5157007596547ca6edb26608"
PREPROCESSED_RECIPE = "preprocessed-v2"
HASHES: dict[str, list] = {}
NEW_HASHES: dict[str, list] = {}


def atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as writer:
        writer.write(data)
    os.replace(writer.name, path)


def digest(path: Path) -> str:
    key = str(path)
    props = path.stat()
    signature = [props.st_size, props.st_mtime_ns, props.st_ctime_ns, props.st_ino]
    saved = HASHES.get(key)
    if saved and saved[:4] == signature:
        result = saved[4]
    else:
        with path.open("rb") as handle:
            result = hashlib.file_digest(handle, "sha256").hexdigest()
    NEW_HASHES[key] = [*signature, result]
    return result


def visible_name(relative: Path) -> str:
    # Direct uploads include a content-hash directory to preserve same-named files.
    return (
        str(Path(*relative.parts[1:]))
        if re.fullmatch(r"[a-f0-9]{64}", relative.parts[0])
        else str(relative)
    )


def source_record(path: Path, identity: str, name: str, recipe: str = LEGACY_RECIPE) -> dict:
    sha = digest(path)
    source_id = "s-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    key = hashlib.sha256(f"{recipe}:{sha}".encode()).hexdigest()[:20]
    return {
        "id": source_id,
        "name": name,
        "path": str(path),
        "sha256": sha,
        "bundle": str(CACHE / source_id / key),
        "bytes": path.stat().st_size,
    }


def expand(path: Path, identity: str, name: str, sha: str, *, preprocessed: bool = False):
    destination = CACHE / "archives" / hashlib.sha256(f"{identity}:{sha}".encode()).hexdigest()
    with ZipFile(path) as archive:
        items = [item for item in archive.infolist() if not item.is_dir()]
        files, size = (
            (25_000, 8 * 1024**3) if preprocessed else (MAX_ARCHIVE_FILES, MAX_ARCHIVE_BYTES)
        )
        if preprocessed and ROOT_INDEX not in {item.filename for item in items}:
            raise ValueError("Upload the generated ZIP containing wiki-index.md")
        if len(items) > files or sum(item.file_size for item in items) > size:
            raise ValueError("ZIP exceeds the expanded file or size limit")
        names = set()
        for item in items:
            parts = PurePosixPath(item.filename)
            if (
                parts.is_absolute()
                or ".." in parts.parts
                or "\\" in item.filename
                or re.match(r"^[A-Za-z]:", item.filename)
                or stat.S_ISLNK(item.external_attr >> 16)
                or parts in names
            ):
                raise ValueError("ZIP has an unsafe or duplicate entry")
            names.add(parts)
        for item in items:
            member = destination / item.filename
            if not member.resolve().is_relative_to(destination.resolve()):
                raise ValueError("ZIP entry leaves the extraction directory")
            record_name = f"{name} › {item.filename}"
            if item.file_size > MAX_BYTES or item.flag_bits & 1 or member.suffix.lower() == ".zip":
                yield dict(
                    source_record(path, f"{identity}!{item.filename}", record_name),
                    container_path=str(path),
                    error="ZIP entry is encrypted, oversized, or itself a ZIP",
                )
                continue
            if not member.is_file() or member.stat().st_size != item.file_size:
                member.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.open(item) as source,
                    tempfile.NamedTemporaryFile(dir=member.parent, delete=False) as output,
                ):
                    temp = Path(output.name)
                    try:
                        remaining = item.file_size
                        while chunk := source.read(min(1024**2, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ValueError("ZIP member exceeded its declared size")
                            output.write(chunk)
                        if remaining:
                            raise ValueError("ZIP member was truncated")
                        output.close()
                        os.replace(temp, member)
                    finally:
                        temp.unlink(missing_ok=True)
            record = source_record(member, f"{identity}!{item.filename}", record_name)
            record.update(container_path=str(path), container_sha256=sha, member=item.filename)
            yield record


def prepared_archive(path: Path, identity: str, name: str, sha: str) -> list[dict]:
    cache_key = hashlib.sha256(f"{PREPROCESSED_RECIPE}:{identity}:{sha}".encode()).hexdigest()
    cached = CACHE / "prepared-archives" / f"{cache_key}.json"
    if cached.is_file():
        return json.loads(cached.read_text(encoding="utf-8"))
    expanded = list(expand(path, identity, name, sha, preprocessed=True))
    if any("error" in row for row in expanded):
        raise ValueError("Preprocessor ZIP contains unsupported entries")
    members = {row["member"]: Path(row["path"]) for row in expanded}
    records = []
    for row in index_rows(members):
        original = row["name"]
        record = source_record(
            members[row["export"]] if "export" in row else members[ROOT_INDEX],
            f"{identity}!{row['kind']}:{original}",
            original,
            f"{PREPROCESSED_RECIPE}:{sha}",
        )
        record.update(
            preprocessed=True,
            container_path=str(path),
            container_sha256=sha,
            wiki_index=str(members[ROOT_INDEX]),
            synopsis=row["synopsis"],
        )
        if "error" in row:
            record["error"] = row["error"]
        else:
            brief = members[row["brief"]]
            if not record["bytes"] or not 0 < brief.stat().st_size <= 5 * 1024**2:
                record["error"] = "The supplied export or brief is empty or exceeds its limit"
            else:
                record.update(member=row["export"], brief=str(brief))
                if url := source_url(members[row["export"]]):
                    record["url"] = url
        records.append(record)
    atomic(cached, json.dumps(records, ensure_ascii=False))
    return records


def inventory(*, preprocessed: bool = False) -> list[dict]:
    records = []
    for path in sorted(ATTACHMENTS.rglob("*")):
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(ATTACHMENTS)
        ):
            continue
        relative = path.relative_to(ATTACHMENTS)
        name = visible_name(relative)
        if path.suffix == ".insightful-url" and path.stat().st_size <= 4096:
            try:
                name = path.read_text(encoding="utf-8").strip()
            except UnicodeError:
                pass
        record = source_record(path, relative.as_posix(), name)
        if path.suffix.lower() == ".zip":
            try:
                args = (path, relative.as_posix(), record["name"], record["sha256"])
                members = prepared_archive(*args) if preprocessed else list(expand(*args))
                records.extend(members or [dict(record, error="ZIP contains no sources")])
            except ValueError as error:
                reason = str(error) if type(error) is ValueError else "Invalid archive encoding"
                records.append(dict(record, error=reason))
            except (OSError, RuntimeError, BadZipFile):
                records.append(dict(record, error="ZIP cannot be safely expanded or is corrupt"))
        elif preprocessed:
            records.append(
                dict(record, error="Process the raw source and upload the generated ZIP")
            )
        else:
            records.append(record)
    return records


def command(args: list[str], timeout: int = 180, *, data: bytes | None = None) -> bytes:
    return subprocess.run(args, check=True, capture_output=True, timeout=timeout, input=data).stdout


def decode(data: bytes) -> str:
    from charset_normalizer import from_bytes

    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        match = from_bytes(data).best()
        if match is None or "\x00" in str(match):
            raise ValueError("Unknown text encoding") from None
        return str(match)


class Document:
    def __init__(self, bundle: Path):
        self.bundle = bundle
        self.blocks: list[dict] = []
        self.assets: list[dict] = []
        self.warnings: list[str] = []

    def block(self, locator: str, text: str, *, lines: bool = False) -> None:
        text = text.replace("\x00", "")
        offset = 0
        first = 1
        while offset < len(text):
            end = min(len(text), offset + BLOCK_SIZE)
            if end < len(text):
                split = text.rfind("\n", offset + BLOCK_SIZE // 2, end)
                if split >= 0:
                    end = split + 1
            portion = text[offset:end].strip()
            if portion:
                location = locator
                if lines:
                    last = first + text[offset:end].rstrip("\n").count("\n")
                    location = f"{locator} lines {first}–{last}"
                self.blocks.append(
                    {"id": f"b{len(self.blocks) + 1:04d}", "locator": location, "text": portion}
                )
            first += text.count("\n", offset, end)
            offset = end

    def image(self, path: Path, locator: str, *, block: bool = True, ocr: bool = False) -> None:
        from PIL import Image, ImageOps

        asset_id = f"a{len(self.assets) + 1:04d}"
        destination = self.bundle / "assets" / f"{asset_id}.jpg"
        destination.parent.mkdir(exist_ok=True)
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1800, 1800))
            image.convert("RGB").save(destination, quality=88)
        entry = ""
        if ocr:
            try:
                entry = decode(
                    command(["tesseract", str(destination), "stdout", "-l", "eng+chi_sim"], 40)
                ).strip()
            except (OSError, subprocess.SubprocessError):
                self.warnings.append(f"OCR unavailable at {locator}")
        refs = [item["id"] for item in self.blocks if item["locator"] == locator]
        if block or entry or not refs:
            self.block(locator, f"Visual asset {asset_id}." + (f"\nOCR:\n{entry}" if entry else ""))
            refs.append(self.blocks[-1]["id"])
        self.assets.append(
            {"id": asset_id, "file": str(destination), "locator": locator, "blocks": refs}
        )

    def pdf(self, path: Path, *, visuals_only: bool = False) -> None:
        text = decode(command(["pdftotext", "-layout", str(path), "-"]))
        pages = text.split("\f")
        if pages[-1] == "":
            pages.pop()
        with tempfile.TemporaryDirectory(dir=self.bundle) as folder:
            prefix = Path(folder) / "page"
            command(
                [
                    "pdftoppm",
                    "-l",
                    str(MAX_VISUAL_PAGES),
                    "-r",
                    "120",
                    "-jpeg",
                    str(path),
                    str(prefix),
                ],
                480,
            )
            rendered = {
                int(item.stem.split("-")[-1]): item for item in Path(folder).glob("page-*.jpg")
            }
            for number, body in enumerate(pages, start=1):
                locator = f"Page {number}" if not visuals_only else f"Rendered page {number}"
                if not visuals_only:
                    self.block(locator, body)
                if number in rendered:
                    self.image(rendered[number], locator, block=False, ocr=not body.strip())
        if len(pages) > MAX_VISUAL_PAGES:
            self.warnings.append(
                f"Page previews and OCR cover pages 1–{MAX_VISUAL_PAGES} of {len(pages)}"
            )

    def office(self, path: Path) -> None:
        with tempfile.TemporaryDirectory(dir=self.bundle) as folder:
            target = Path(folder)
            command(
                [
                    "libreoffice",
                    f"-env:UserInstallation={target.as_uri()}/profile",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    folder,
                    str(path),
                ],
                180,
            )
            if path.suffix.lower() == ".docx":
                try:
                    markdown = decode(
                        command(["pandoc", "-f", "docx", "-t", "gfm", "--wrap=none", str(path)])
                    )
                    self.block("Converted Word paragraphs and tables", markdown, lines=True)
                except (OSError, subprocess.SubprocessError):
                    self.warnings.append(
                        "Word structure conversion unavailable. Used rendered page text."
                    )
            self.pdf(target / f"{path.stem}.pdf")

    def video(self, path: Path) -> None:
        duration = float(
            command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ]
            )
        )
        count = max(1, min(16, int(duration // 10) + 1))
        self.block("Video metadata", f"Duration {duration:.1f}s. {count} sampled frames.")
        self.warnings.append("Video frames are sampled. Events between frames may be missed.")
        with tempfile.TemporaryDirectory(dir=self.bundle) as folder:
            frame = Path(folder) / "frame.jpg"
            for i in range(count):
                at = duration * (i + 0.5) / count
                command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{at:.2f}",
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=1280:-2",
                        str(frame),
                    ],
                    45,
                )
                if frame.exists():
                    self.image(frame, f"Time {at:.2f}s", ocr=True)
                    frame.unlink()
                else:
                    self.warnings.append(f"Frame unavailable at {at:.2f}s")
            audio = command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "csv=p=0",
                    str(path),
                ]
            ).strip()
            if audio:
                self.warnings.append(
                    "Audio requires transcription. Frame analysis cannot recover speech."
                )
                # The host may send these audio-only segments to the configured transcription provider.
                audio_root = self.bundle / "audio"
                audio_root.mkdir(exist_ok=True)
                command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-map",
                        "0:a:0",
                        "-t",
                        "10800",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        "32k",
                        "-f",
                        "segment",
                        "-segment_time",
                        "600",
                        str(audio_root / "%03d.mp3"),
                    ],
                    240,
                )
                if duration > 10800:
                    self.warnings.append("Audio segment preparation covers the first three hours.")

    def write(self, record: dict) -> dict:
        if not self.blocks:
            raise ValueError("No readable text or visual content")
        assets = {}
        for item in self.assets:
            for block_id in item["blocks"]:
                assets.setdefault(block_id, []).append(item)
        rows = [f"# {record['name']}"]
        for item in self.blocks:
            rows.extend([f"## {item['id']} · {item['locator']}", item["text"]])
            rows.extend(
                f"![{asset['id']}](assets/{Path(asset['file']).name})"
                for asset in assets.get(item["id"], [])
            )
        atomic(self.bundle / "source.md", "\n\n".join(rows) + "\n")
        details = dict(
            record,
            blocks=self.blocks,
            assets=self.assets,
            warnings=list(dict.fromkeys(self.warnings)),
            audio=[str(path) for path in sorted((self.bundle / "audio").glob("*.mp3"))],
        )
        atomic(self.bundle / "document.json", json.dumps(details, ensure_ascii=False))
        return details


def normalize(record: dict, web: dict | None = None, mineru: Path | None = None) -> dict:
    if record["bytes"] > MAX_BYTES:
        raise ValueError("Source exceeds the file size limit")
    if record.get("error"):
        raise ValueError(record["error"])
    bundle = Path(record["bundle"])
    metadata = bundle / "document.json"
    if metadata.is_file():
        return json.loads(metadata.read_text())
    path = Path(web["path"] if web else record["path"])
    bundle.mkdir(parents=True, exist_ok=True)
    document = Document(bundle)
    kind = path.suffix.lower()
    if record.get("preprocessed"):
        from PIL import Image

        data = path.read_text(encoding="utf-8-sig")
        document.block("Export Markdown", data, lines=True)
        for line, asset in dict.fromkeys(images(record, data)):
            try:
                if asset is not None:
                    document.image(asset, f"Export Markdown line {line} (linked source figure)")
                    continue
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
            document.warnings.append(f"Linked source figure unavailable at export line {line}")
        record = dict(record, parser="Bulk Reference Files Preprocessor")
    elif mineru is not None:
        from source_mineru import read_mineru

        read_mineru(document, mineru)
        record = dict(record, parser="MinerU")
    elif kind in {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".srt", ".vtt"}:
        data = decode(path.read_bytes())
        if "\x00" in data:
            raise ValueError("Text contains binary data")
        document.block("Original", data, lines=True)
        if kind in {".md", ".markdown"}:
            for image_name in re.findall(r"!\[[^\]\n]*\]\(([^\s()]+)\)", data)[:100]:
                asset = (path.parent / image_name).resolve()
                if (
                    asset.is_relative_to(path.parent.resolve())
                    and asset.is_file()
                    and asset.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                ):
                    try:
                        document.image(asset, f"Linked image {image_name}", ocr=True)
                    except (OSError, ValueError):
                        document.warnings.append(f"Linked image unavailable: {image_name}")
                else:
                    document.warnings.append(f"Linked image unavailable: {image_name}")
    elif kind in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        document.image(path, "Image", ocr=True)
    elif kind == ".pdf":
        document.pdf(path)
    elif kind in {".doc", ".docx", ".odt", ".rtf"}:
        document.office(path)
    elif kind in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        document.video(path)
    elif kind in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(decode(path.read_bytes()), "html.parser")
        for element in soup.select("script,style,nav,footer,noscript,iframe,header,form"):
            element.decompose()
        section = soup.find("main") or soup.find("article") or soup.body or soup
        document.block(
            "Extracted web text",
            decode(
                command(
                    ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
                    45,
                    data=str(section).encode(),
                )
            ),
            lines=True,
        )
        if section.find("img"):
            document.warnings.append(
                "Web image references are recorded; only successfully downloaded images are embedded."
            )
        for index, tag in enumerate(section.find_all("img")[:40], 1):
            image_url = str(tag.get("src") or "")
            if image_url:
                locator = f"Web image {index}"
                document.block(locator, f"{tag.get('alt') or ''}\nImage reference: {image_url}")
                if web and (local := web.get("images", {}).get(image_url)):
                    document.image(Path(local), locator, block=False, ocr=True)
    else:
        raise ValueError(f"Unsupported source format {kind or '(no extension)'}")
    if web:
        record = dict(
            record, url=web["url"], resolved_url=web["resolved_url"], web_sha256=digest(path)
        )
    return document.write(record)


def main() -> None:
    action = sys.argv[1]
    if action in {"inventory", "inventory-preprocessed"}:
        target = CACHE / "inventory.json"
        hashes = CACHE / "input-hashes.json"
        try:
            HASHES.update(json.loads(hashes.read_text()))
        except (OSError, ValueError):
            pass
        atomic(
            target,
            json.dumps(
                inventory(preprocessed=action == "inventory-preprocessed"), ensure_ascii=False
            ),
        )
        atomic(hashes, json.dumps(NEW_HASHES, ensure_ascii=False))
        print(str(target))
    elif action == "normalize":
        payload = json.loads(Path(sys.argv[2]).read_text())
        result = normalize(
            payload["record"],
            payload.get("web"),
            Path(payload["mineru"]) if payload.get("mineru") else None,
        )
        print(json.dumps({"file": str(Path(result["bundle"]) / "document.json")}))
    else:
        raise ValueError("Unknown source command")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        safe = (
            str(error)
            if type(error) is ValueError
            else f"{type(error).__name__}: parser could not finish"
        )
        print(json.dumps({"error": safe[:600]}))
        sys.exit(1)
