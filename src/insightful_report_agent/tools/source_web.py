"""Public HTTP(S) evidence snapshots. Validate and pin the address on every redirect."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx

from insightful_report_agent.tools.fetch import FetchRefused, check_url, system_resolver

SOURCE_URL = re.compile(r"""https?://[^\s<>\"'｜，。；、）】]+""", re.IGNORECASE)
TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def urls_from_text(text: str) -> list[str]:
    return list(
        dict.fromkeys(match.group().rstrip(".,;:!?)]") for match in SOURCE_URL.finditer(text))
    )


async def fetch_source(url: str, *, max_bytes: int = 50 * 1024**2) -> tuple[bytes, str, str]:
    async with httpx.AsyncClient(
        timeout=25,
        trust_env=False,
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for _ in range(6):
            addresses: list[str] = []

            async def resolve(name: str, port: int, addresses: list[str] = addresses) -> list[str]:
                addresses.extend(await system_resolver(name, port))
                return addresses

            await check_url(url, resolve)
            original = httpx.URL(url)
            target = original.copy_with(host=addresses[0]) if addresses else original
            async with client.stream(
                "GET",
                target,
                headers={
                    "Host": original.netloc.decode("ascii"),
                    "User-Agent": "insightful-report-agent/0.1",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": original.host},
            ) as response:
                if response.is_redirect and (location := response.headers.get("location")):
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise FetchRefused(f"HTTP {response.status_code}")
                mime = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                suffix = TYPES.get(mime)
                if not suffix:
                    raise FetchRefused("Unsupported web content type")
                data = bytearray()
                async for piece in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(piece) > max_bytes:
                        raise FetchRefused("Web source exceeds the download limit")
                    data.extend(piece)
                return bytes(data), suffix, url
    raise FetchRefused("Too many redirects")
