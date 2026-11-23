"""MinerU handshake, signed transfer and ZIP response with a mock HTTP transport."""

from io import BytesIO
from zipfile import ZipFile

import httpx

from insightful_report_agent.tools.mineru import MinerU


async def test_signed_transfer_and_polling() -> None:
    calls = []
    buffer = BytesIO()
    with ZipFile(buffer, "w") as zip_file:
        zip_file.writestr("full.md", "# Extracted")

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host))
        if request.url.host == "mineru.net":
            assert request.headers["authorization"] == "Bearer fake-token"
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "batch_id": "sample-batch",
                            "file_urls": ["https://example.aliyuncs.com/upload?signature=xyz"],
                        },
                    },
                )
            state = "pending" if len(calls) == 3 else "done"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "state": state,
                                "data_id": "source-one",
                                "full_zip_url": "https://cdn-mineru.openxlab.org.cn/extracted.zip",
                            },
                        ]
                    },
                },
            )
        assert "authorization" not in request.headers
        if request.method == "PUT":
            assert request.content == b"source-pdf"
            assert "content-type" not in request.headers
            return httpx.Response(200)
        return httpx.Response(200, content=buffer.getvalue())

    client = MinerU("fake-token", wait=2, interval=0.001, transport=httpx.MockTransport(handle))
    batch, signed = await client.submit("example.pdf", "source-one", "vlm")
    await client.upload(signed, b"source-pdf")
    actual = await client.result(batch, "source-one")
    assert actual == buffer.getvalue()
    assert [item[0] for item in calls] == ["POST", "PUT", "GET", "GET", "GET"]
