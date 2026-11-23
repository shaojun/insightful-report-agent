import importlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup


def test_report_supports_many_evidence_blocks_including_charts_and_images(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "workspace" / "tools"))
    renderer = importlib.import_module("source_report")
    monkeypatch.setattr(renderer, "CACHE", tmp_path)
    source = "s-0000000000000001"
    asset = tmp_path / "preview.jpg"
    asset.write_bytes(b"\xff\xd8\xff")
    documents = {
        source: {
            "name": "source.md",
            "blocks": {
                f"b{i:04d}": {"text": f"Evidence {i}", "locator": f"Line {i}"} for i in range(1, 22)
            },
            "assets": {"a0001": {"file": str(asset), "locator": "Preview", "blocks": ["b0020"]}},
        }
    }
    chart = json.dumps(
        {
            "title": "Orders",
            "unit": "orders",
            "data": [{"label": "Q4", "value": 42, "evidence": f"[[{source}#b0019]]"}],
        }
    )
    draft = " ".join(f"[[{source}#b{i:04d}]]" for i in range(1, 19))
    draft += f" [[{source}#b0001]]\n\n![Preview](source:{source}/a0001)\n\n"
    draft += f"```chart\n{chart}\n```\n"
    payload = {
        "draft": draft,
        "records": [{"id": source, "path": "/workspace/attachments/source.md", "sha256": "abc"}],
        "rows": [{"source_id": source, "name": "source.md", "warnings": []}],
        "snapshot": "test",
    }
    with pytest.raises(ValueError, match="Write the requested analysis"):
        renderer.Report(documents, 1024**2).render({**payload, "draft": "# probe\n\nText " + draft})
    payload["draft"] = (
        "Orders are summarized for the supplied period only. The available observations are "
        "insufficient to infer a long-term trend. " + draft
    )
    report = renderer.Report(documents, 1024**2)
    payload["require_source_figures"] = True
    without_image = payload["draft"].replace(f"![Preview](source:{source}/a0001)", "")
    with pytest.raises(ValueError, match="Embed at least one relevant source figure"):
        renderer.Report(documents, 1024**2).render({**payload, "draft": without_image})
    html = BeautifulSoup(report.render(payload), "html.parser")
    assert report.source_figures == 1
    assert len(html.select("section[id]")) == 20
    assert len(html.select("a.cite")) == 21
    assert all(html.find(id=link["href"][1:]) for link in html.select("a.cite"))

    payload["draft"] += f"\n[[{source}#b0021]]"
    html = BeautifulSoup(renderer.Report(documents, 1024**2).render(payload), "html.parser")
    assert len(html.select("section[id]")) == 21
    assert all(html.find(id=link["href"][1:]) for link in html.select("a.cite"))
