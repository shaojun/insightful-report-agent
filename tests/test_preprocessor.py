from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from insightful_report_agent.config import Settings
from insightful_report_agent.runtime.observability import Observability
from insightful_report_agent.runtime.preprocessor import is_prepared, preprocess

JOB = "4a94cb4007104223b0b4cd88e3fe50d4"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["partial", "failed"])
async def test_preprocessor_terminal_state(monkeypatch, state):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("wiki-index.md", "example")
    calls = []
    settings = Settings(
        _env_file=None,
        preprocessor_api_url="https://preprocessor.example",
        preprocessor_api_key="test-key",
        langfuse_tracing_enabled=False,
    )

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        path = request.url.path
        calls.append((request.method, path.rsplit("/", 1)[-1]))
        if path.endswith("/jobs"):
            return httpx.Response(201, json={"id": JOB})
        if path.endswith("/files"):
            assert request.url.params["path"] == "资料.md"
            assert request.content == b"source"
            return httpx.Response(200)
        if path.endswith("/commit"):
            return httpx.Response(200)
        if path.endswith("/download"):
            return httpx.Response(200, content=buffer.getvalue())
        assert path.endswith(JOB)
        return httpx.Response(200, json={"state": state, "download_ready": state == "partial"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "insightful_report_agent.runtime.preprocessor.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    if state == "partial":
        name, data, status = await preprocess(
            settings, Observability(settings), {"资料.md": b"source"}
        )
        assert status == "partial"
        assert is_prepared(name, data)
        assert data == buffer.getvalue()
        assert calls[-1] == ("GET", "download")
    else:
        with pytest.raises(RuntimeError, match="no usable ZIP"):
            await preprocess(settings, Observability(settings), {"资料.md": b"source"})
        assert calls[-1] == ("GET", JOB)
    assert calls[:3] == [("POST", "jobs"), ("PUT", "files"), ("POST", "commit")]
