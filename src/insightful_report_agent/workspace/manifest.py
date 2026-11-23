"""What the stager puts into a sandbox's /workspace, with sha256 for every file.

Generated from the checkout on each process start (and by `scripts/build_manifest.py` for
inspection); never hand-edited. Sources are confined to an allowlist of roots, symlinks that leave
the tree are refused, and nothing named `.env*` is ever read.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

WORKSPACE = PurePosixPath("/workspace")
#: Where a deployed project keeps skills' binary files: MDA syncs `skills/` to Context Hub, which
#: takes only UTF-8 text (`deploy.stage` moves them here).
SKILL_BINARIES = "skill-binaries"
SKIPPED_NAMES = frozenset({".DS_Store", "__pycache__", "node_modules", ".git"})


class ManifestError(ValueError):
    """A source the stager refuses to read. The message names the path, never its content."""


@dataclass(frozen=True)
class Asset:
    source: Path
    destination: PurePosixPath
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    assets: tuple[Asset, ...]
    required_families: tuple[str, ...]
    generated: tuple[tuple[PurePosixPath, bytes], ...] = ()
    """Files produced in memory (the brand's tokens.css when the pack ships none)."""

    @property
    def digest(self) -> str:
        """One hash over every destination, checksum and required family."""
        lines = [f"{a.destination}\t{a.sha256}" for a in self.assets]
        lines += [f"{p}\t{hashlib.sha256(c).hexdigest()}" for p, c in self.generated]
        lines += [f"font\t{f}" for f in self.required_families]
        return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()

    def to_json(self) -> str:
        return json.dumps(
            {
                "digest": self.digest,
                "required_families": list(self.required_families),
                "files": [
                    {"destination": str(a.destination), "sha256": a.sha256, "bytes": a.size}
                    for a in self.assets
                ],
                "generated": [str(p) for p, _ in self.generated],
            },
            indent=2,
        )

    def checklist(self) -> bytes:
        """`sha256sum -c` input for every staged file."""
        rows = [(a.destination, a.sha256) for a in self.assets]
        rows += [(p, hashlib.sha256(c).hexdigest()) for p, c in self.generated]
        return "".join(f"{sha}  {dest}\n" for dest, sha in sorted(rows)).encode()

    def archive(self) -> bytes:
        """A deterministic gzipped tar relative to /workspace, parent directories first.

        Explicit directory members make tar replace a planted directory symlink instead of
        writing through it; fixed modes and mtimes make one manifest produce one set of bytes.
        """
        members: list[tuple[str, bytes]] = [
            (str(a.destination.relative_to(WORKSPACE)), a.source.read_bytes()) for a in self.assets
        ]
        members += [(str(p.relative_to(WORKSPACE)), c) for p, c in self.generated]
        members.sort()
        directories: set[str] = set()
        for name, _ in members:
            parts = name.split("/")[:-1]
            directories.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for directory in sorted(directories):
                    entry = tarfile.TarInfo(f"{directory}/")
                    entry.type, entry.mode, entry.mtime = tarfile.DIRTYPE, 0o755, 0
                    tar.addfile(entry)
                for name, content in members:
                    entry = tarfile.TarInfo(name)
                    entry.size, entry.mode, entry.mtime = len(content), 0o644, 0
                    tar.addfile(entry, io.BytesIO(content))
        return buffer.getvalue()


def _files(root: Path, base: Path) -> Iterator[Path]:
    for path in sorted(base.rglob("*")):
        if any(part in SKIPPED_NAMES for part in path.relative_to(base).parts):
            continue
        if path.name.startswith(".env"):
            raise ManifestError(f"refusing to stage an environment file: {path.relative_to(root)}")
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            raise ManifestError(f"symlink leaves the checkout: {path.relative_to(root)}")
        if path.is_file():
            yield path


def _asset(source: Path, destination: PurePosixPath) -> Asset:
    data = source.read_bytes()
    return Asset(source, destination, hashlib.sha256(data).hexdigest(), len(data))


def build_manifest(project_root: Path) -> Manifest:
    """Read-only runtime skills and evidence processing scripts."""
    root = project_root.resolve()
    assets: list[Asset] = []
    skills = root / "skills"
    if skills.is_dir():
        for skill in sorted(
            p for p in skills.iterdir() if p.is_dir() and p.name not in SKIPPED_NAMES
        ):
            for path in _files(root, skill):
                rel = path.relative_to(skill).as_posix()
                assets.append(_asset(path, WORKSPACE / "skills" / skill.name / rel))
    # Skills' binary files live here, because Context Hub takes only UTF-8 text (skills_lock).
    binaries = root / SKILL_BINARIES
    if binaries.is_dir():
        for path in _files(root, binaries):
            assets.append(
                _asset(path, WORKSPACE / "skills" / path.relative_to(binaries).as_posix())
            )
    tools = root / "workspace" / "tools"
    if tools.is_dir():
        for path in _files(root, tools):
            assets.append(_asset(path, WORKSPACE / "tools" / path.relative_to(tools).as_posix()))
    destinations = [item.destination for item in assets]
    if len(destinations) != len(set(destinations)):
        raise ManifestError("two sources stage to the same destination")
    return Manifest(assets=tuple(assets), required_families=())
