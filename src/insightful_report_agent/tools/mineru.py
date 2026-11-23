"""MinerU Precision Extract client. Signed transfers never carry the API credential."""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

import httpx

API = "https://mineru.net/api/v4"
MAX_INPUT = 200_000_000
MAX_RESULT = 200 * 1024**2
SUPPORTED = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
    ".html",
    ".htm",
}


class MinerUError(RuntimeError):
    pass


def signed_url(value: object, *, upload: bool) -> str:
    if not isinstance(value, str):
        raise MinerUError("MinerU did not return a signed URL")
    url = urlsplit(value)
    host = url.hostname or ""
    scopes = (".aliyuncs.com",) if upload else (".openxlab.org.cn", ".mineru.net", ".aliyuncs.com")
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in (None, 443)
        or not host.endswith(scopes)
    ):
        raise MinerUError("MinerU returned an unexpected storage host")
    return value


class MinerU:
    def __init__(
        self,
        token: str,
        *,
        wait: float = 900,
        interval: float = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.wait = wait
        self.interval = interval
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=120, follow_redirects=False, transport=self.transport)

    async def api(self, method: str, endpoint: str, **kwargs) -> dict:
        try:
            async with self.client() as client:
                response = await client.request(
                    method, API + endpoint, headers=self.headers, **kwargs
                )
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "0":
                    code = str(payload.get("code", ""))
                    safe_code = code if re.fullmatch(r"[A-Za-z0-9-]{1,24}", code) else "unknown"
                    raise MinerUError(f"MinerU API error {safe_code}")
                if not isinstance(payload.get("data"), dict):
                    raise MinerUError("MinerU returned an invalid API response")
                return payload["data"]
        except httpx.HTTPStatusError as error:
            raise MinerUError(f"MinerU API HTTP {error.response.status_code}") from None
        except (httpx.RequestError, ValueError, AttributeError):
            raise MinerUError("MinerU API request failed") from None

    async def submit(self, filename: str, source_id: str, model: str) -> tuple[str, str]:
        batch = await self.api(
            "POST",
            "/file-urls/batch",
            json={
                "files": [{"name": filename, "data_id": source_id}],
                "model_version": model,
                **(
                    {"enable_formula": True, "enable_table": True} if model != "MinerU-HTML" else {}
                ),
            },
        )
        batch_id = str(batch.get("batch_id", ""))
        urls = batch.get("file_urls")
        if (
            not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", batch_id)
            or not isinstance(urls, list)
            or len(urls) != 1
        ):
            raise MinerUError("MinerU returned an incomplete upload ticket")
        return batch_id, signed_url(urls[0], upload=True)

    async def upload(self, url: str, source: bytes) -> None:
        if not source or len(source) > MAX_INPUT:
            raise MinerUError("MinerU accepts source files between 1 byte and 200 MB")
        try:
            async with self.client() as client:
                result = await client.put(signed_url(url, upload=True), content=source)
                result.raise_for_status()
        except httpx.HTTPError:
            raise MinerUError("MinerU storage upload failed") from None

    async def result(self, batch_id: str, source_id: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", batch_id):
            raise MinerUError("Invalid MinerU batch id")
        deadline = time.monotonic() + self.wait
        while time.monotonic() < deadline:
            data = await self.api("GET", f"/extract-results/batch/{batch_id}")
            rows = data.get("extract_result", [])
            row = next((row for row in rows if row.get("data_id") == source_id), None)
            if row is None and len(rows) == 1 and not rows[0].get("data_id"):
                row = rows[0]
            if row:
                state = row.get("state")
                if state == "failed":
                    raise MinerUError("MinerU could not parse this file")
                if state == "done":
                    return await self.download(signed_url(row.get("full_zip_url"), upload=False))
            await asyncio.sleep(min(self.interval, max(0, deadline - time.monotonic())))
        raise MinerUError(
            "MinerU is still processing. Retry the preparation batch to resume polling."
        )

    async def download(self, url: str) -> bytes:
        try:
            async with self.client() as client, client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_RESULT:
                        raise MinerUError("MinerU result archive exceeds the download limit")
                    body.extend(chunk)
                if not body.startswith(b"PK"):
                    raise MinerUError("MinerU result is not a ZIP archive")
                return bytes(body)
        except httpx.HTTPError:
            raise MinerUError("MinerU result download failed") from None
