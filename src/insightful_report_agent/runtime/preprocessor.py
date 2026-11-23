"""Host client for Bulk Reference Files Preprocessor. The sandbox imports its result ZIP."""

from __future__ import annotations

import asyncio
from io import BytesIO
from uuid import UUID
from zipfile import BadZipFile, ZipFile

import httpx

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.observability import Observability

MAX_RESULT_BYTES = 200 * 1024**2


def is_prepared(name: str, content: bytes) -> bool:
    if not name.lower().endswith(".zip"):
        return False
    try:
        with ZipFile(BytesIO(content)) as archive:
            return "wiki-index.md" in archive.namelist()
    except BadZipFile:
        return False


async def preprocess(
    settings: Settings, trace: Observability, files: dict[str, bytes]
) -> tuple[str, bytes]:
    transport = httpx.AsyncClient(
        base_url=settings.preprocessor_api_url,
        headers={"Authorization": f"Bearer {settings.preprocessor_api_key.get_secret_value()}"},
        timeout=90,
        trust_env=False,
    )
    with trace.span("preprocessor.job", files=len(files)) as span:
        try:
            async with transport, asyncio.timeout(settings.preprocessor_timeout_seconds):
                job_id = (await transport.post("/api/v1/jobs")).raise_for_status().json()["id"]
                if job_id not in {UUID(job_id).hex, str(UUID(job_id))}:
                    raise ValueError("Invalid job ID")
                if span:
                    span.update(metadata={"job_id": str(job_id), "files": len(files)})
                path = f"/api/v1/jobs/{job_id}"
                with trace.span("preprocessor.upload", files=len(files)):
                    for name, data in files.items():
                        response = await transport.put(
                            f"{path}/files",
                            params={"path": name},
                            headers={"Content-Type": "application/octet-stream"},
                            content=data,
                        )
                        response.raise_for_status()
                    (await transport.post(f"{path}/commit")).raise_for_status()
                with trace.span("preprocessor.wait") as wait:
                    while True:
                        job = (await transport.get(path)).raise_for_status().json()
                        if job["state"] in {"succeeded", "partial", "failed"}:
                            break
                        await asyncio.sleep(2)
                    if wait:
                        wait.update(output={"state": job["state"]})
                    if not job["download_ready"] or job["state"] == "failed":
                        raise RuntimeError("Preprocessing failed; no usable ZIP was returned")
                with trace.span("preprocessor.download") as download:
                    result = bytearray()
                    async with transport.stream("GET", f"{path}/download") as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if len(result) + len(chunk) > MAX_RESULT_BYTES:
                                raise RuntimeError("Preprocessed ZIP exceeds 200 MiB")
                            result.extend(chunk)
                    name = f"preprocessed-{job_id}.zip"
                    content = bytes(result)
                    if not is_prepared(name, content):
                        raise RuntimeError("Preprocessor returned an invalid result ZIP")
                    if download:
                        download.update(output={"bytes": len(content)})
        except httpx.HTTPStatusError as error:
            raise RuntimeError(f"Preprocessor HTTP {error.response.status_code}") from None
        except (httpx.HTTPError, KeyError, ValueError, TypeError, TimeoutError):
            raise RuntimeError("Preprocessor unavailable or returned an invalid response") from None
        if span:
            span.update(output={"state": job["state"], "result_bytes": len(content)})
        return name, content
