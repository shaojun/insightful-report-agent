"""Validate and render a self-contained HTML report in the thread sandbox."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from source_normalize import CACHE, ROOT, atomic

CITATION = re.compile(r"\[\[(s-[0-9a-f]{16})#(b\d{4,})\]\]")
VISUAL = re.compile(r"source:(s-[0-9a-f]{16})/(a\d{4,})")
MIN_REPORT_BODY_CHARS = 80
STYLE = """
:root{font-family:system-ui,"Noto Sans CJK SC",sans-serif;color:#172333;background:#f2f5f8;line-height:1.68}
main{max-width:960px;margin:32px auto;background:#fff;padding:clamp(20px,5vw,64px);box-sizing:border-box;border-top:6px solid #176760}
h1,h2,h3{line-height:1.25;color:#152b36}h1{font-size:clamp(2rem,5vw,3.25rem);margin-top:.25em}
h2{margin-top:1.8em;padding-bottom:.35em;border-bottom:1px solid #d6e1e8}
a{color:#14635d}.cite{white-space:nowrap;font-size:.82em;margin:0 .16em}
figure{margin:1.3em 0;padding:1em;border:1px solid #d9e4e8;border-radius:.5em}
img,svg{max-width:100%;height:auto;display:block;margin:auto}figcaption,.meta{color:#455962;font-size:.9em}
table{border-collapse:collapse;width:100%;display:block;overflow:auto}th,td{border:1px solid #d9e4e8;padding:.4em .65em;text-align:left}th{background:#e9f3f1}
blockquote{border-left:3px solid #176760;padding-left:1em;margin-left:0}pre{white-space:pre-wrap;word-break:break-word;background:#f1f5f7;padding:1em}
details{border-top:1px solid #dee6ea;padding:.55em 0;overflow-wrap:anywhere}
summary{cursor:pointer}section[id]{scroll-margin:1em}section[id]:target{outline:2px solid #176760}
@media print{main{margin:0;max-width:none;border:0;padding:0}figure{break-inside:avoid}}
"""


def text(value: object) -> str:
    return html.escape(str(value), quote=True)


def label(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Chart labels must be nonempty and within their length limit")
    return value


class Report:
    def __init__(self, documents: dict, limit: int):
        self.documents = documents
        self.limit = limit
        self.citations: dict[tuple[str, str], int] = {}
        self.visuals = 0
        self.source_figures = 0
        self.asset_bytes = 0
        self.markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
        self.markdown.renderer.rules["text"] = self.inline_text
        self.markdown.renderer.rules["image"] = self.image
        self.markdown.renderer.rules["fence"] = self.fence

    def ref(self, source: str, block: str) -> str:
        doc = self.documents.get(source)
        if not doc or block not in doc["blocks"]:
            raise ValueError(f"Unknown citation {source}#{block}")
        index = self.citations.setdefault((source, block), len(self.citations) + 1)
        return (
            f'<a class="cite" href="#{source}-{block}" aria-label="Evidence {index}">[{index}]</a>'
        )

    def inline_text(self, tokens, index, _options, _env):
        raw = tokens[index].content
        output = []
        start = 0
        for citation in CITATION.finditer(raw):
            output.extend((text(raw[start : citation.start()]), self.ref(*citation.groups())))
            start = citation.end()
        output.append(text(raw[start:]))
        return "".join(output)

    def image(self, tokens, index, _options, _env):
        token = tokens[index]
        match = VISUAL.fullmatch(token.attrGet("src") or "")
        if not match:
            raise ValueError(
                "Reference images with source:source-id/asset-id from the visual index"
            )
        source, asset_id = match.groups()
        doc = self.documents.get(source)
        asset = doc["assets"].get(asset_id) if doc else None
        if not asset:
            raise ValueError(f"Unknown source image {source}/{asset_id}")
        path = Path(asset["file"]).resolve()
        if not path.is_relative_to(CACHE.resolve()) or path.suffix != ".jpg":
            raise ValueError("Invalid source asset")
        size = path.stat().st_size
        self.asset_bytes += size
        if self.asset_bytes > self.limit * 3 // 4:
            raise ValueError("Embedded images exceed the report size limit. Use fewer previews.")
        data = path.read_bytes()
        if not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("Invalid JPEG asset")
        self.visuals += 1
        self.source_figures += 1
        refs = " ".join(self.ref(source, block) for block in asset["blocks"][:3])
        caption = text(token.content or asset["locator"])
        src = base64.b64encode(data).decode()
        return f'<img src="data:image/jpeg;base64,{src}" alt="{caption}" /><span class="meta">{caption} · {refs}</span>'

    def fence(self, tokens, index, options, env):
        token = tokens[index]
        if token.info.strip() != "chart":
            from markdown_it.renderer import RendererHTML

            return RendererHTML().fence(tokens, index, options, env)
        spec = json.loads(token.content)
        if not isinstance(spec, dict):
            raise ValueError("A chart requires a JSON object")
        title = label(spec.get("title"), 150)
        unit = label(spec.get("unit"), 40)
        data = spec.get("data")
        if not isinstance(data, list) or not 1 <= len(data) <= 16:
            raise ValueError("A chart requires 1–16 labeled values")
        rows = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("Each chart row requires label, value and evidence")
            name = label(item.get("label"), 52)
            value = item.get("value")
            if type(value) not in (int, float) or abs(value) > 1e100 or not math.isfinite(value):
                raise ValueError("Chart requires finite numeric values")
            match = CITATION.fullmatch(str(item.get("evidence", "")))
            if not match:
                raise ValueError("Every chart value requires an exact evidence citation")
            rows.append((name, value, self.ref(*match.groups())))
        lo = min(0, *(value for _, value, _ in rows))
        hi = max(0, *(value for _, value, _ in rows))
        span = hi - lo or 1
        baseline = 225 + 395 * -lo / span
        height = 30 + 38 * len(rows)
        shapes = [
            f'<line x1="{baseline:.2f}" y1="7" x2="{baseline:.2f}" y2="{height - 10}" stroke="#47606b"/>'
        ]
        for i, (name, value, _) in enumerate(rows):
            y = i * 38 + 14
            end = baseline + 395 * value / span
            shapes.append(
                f'<text x="220" y="{y + 15}" text-anchor="end" fill="#172333">{text(name[:25])}</text>'
                f'<rect x="{min(end, baseline):.2f}" y="{y}" width="{abs(end - baseline):.2f}" height="19" rx="2" fill="#176760"/>'
                f'<text x="630" y="{y + 15}" fill="#172333">{text(value)}</text>'
            )
        values = "".join(
            f'<tr><th scope="row">{text(name)}</th><td>{text(value)} {text(unit)}</td><td>{ref}</td></tr>'
            for name, value, ref in rows
        )
        self.visuals += 1
        return (
            f"<figure><figcaption><strong>{text(title)}</strong> · {text(unit)}</figcaption>"
            f'<svg viewBox="0 0 850 {height}" role="img" aria-label="{text(title)}" xmlns="http://www.w3.org/2000/svg">'
            + "".join(shapes)
            + "</svg><details><summary>Chart data and sources</summary><table>"
            "<thead><tr><th>Label</th><th>Value</th><th>Evidence</th></tr></thead>"
            f"<tbody>{values}</tbody></table></details></figure>"
        )

    def render(self, payload: dict) -> str:
        draft = payload["draft"]
        title = next(
            (line[2:].strip() for line in draft.splitlines() if line.startswith("# ")),
            "Insightful Report",
        )
        env = {}
        tokens = self.markdown.parse(draft, env)
        body_chars = sum(
            char.isalnum()
            for index, token in enumerate(tokens)
            if token.type == "inline" and tokens[index - 1].type != "heading_open"
            for child in token.children or ()
            if child.type in {"text", "code_inline"}
            for char in CITATION.sub("", child.content)
        )
        if body_chars < MIN_REPORT_BODY_CHARS:
            raise ValueError(
                f"The draft has only {body_chars} letters or digits in its body. "
                f"Write the requested analysis before publishing (minimum {MIN_REPORT_BODY_CHARS}, "
                "excluding headings, citations, figures and fenced blocks)."
            )
        content = self.markdown.renderer.render(tokens, self.markdown.options, env)
        if payload.get("require_source_figures") and not self.source_figures:
            raise ValueError(
                "Embed at least one relevant source figure with "
                "![caption](source:source-id/asset-id), then review the report again."
            )
        if not self.citations:
            raise ValueError("Use valid inline evidence citations in the report")
        evidence = []
        for (source, block_id), index in self.citations.items():
            doc = self.documents[source]
            block = doc["blocks"][block_id]
            evidence.append(
                f'<section id="{source}-{block_id}"><h3>[{index}] {text(doc.get("url", doc["name"]))}</h3>'
                f'<p class="meta">{text(block["locator"])} · {text(source)}#{text(block_id)}</p>'
                f'<pre>{text(block["text"])}</pre><p><a href="#catalog-{source}">Source record</a></p></section>'
            )
        coverage = []
        records = {row["id"]: row for row in payload["records"]}
        for row in payload["rows"]:
            key = row["source_id"]
            original = records[key]
            desc = (
                "Processing failed: " + row["error"]
                if "error" in row
                else "Reviewed. " + " ".join(row["warnings"])
            )
            document = self.documents.get(key, {})
            name = document.get("url", row.get("url", row["name"]))
            web = (
                name if isinstance(name, str) and name.startswith(("https://", "http://")) else None
            )
            if web and urlsplit(web).hostname:
                name_html = f'<a href="{text(web)}" rel="noreferrer noopener">{text(web)}</a>'
            else:
                name_html = text(name)
            lines = [
                name_html,
                text(desc),
                "Source ID " + text(key),
            ]
            if original.get("preprocessed"):
                if "member" in original:
                    lines.extend(
                        (
                            "Export path in ZIP " + text(original["member"]),
                            "Export SHA-256 " + text(original["sha256"]),
                        )
                    )
            else:
                lines.extend(
                    (
                        "Original workspace path " + text(original["path"]),
                        "Input SHA-256 " + text(original["sha256"]),
                    )
                )
            if original.get("container_sha256"):
                lines.append("ZIP SHA-256 " + text(original["container_sha256"]))
            if row.get("parser"):
                lines.append("Parser " + text(row["parser"]))
            if row.get("web_sha256"):
                lines.append("Web snapshot SHA-256 " + text(row["web_sha256"]))
            if row.get("resolved_url") and row["resolved_url"] != web:
                lines.append("Downloaded URL " + text(row["resolved_url"]))
            coverage.append(
                f'<details id="catalog-{key}"><summary>{text(name)} · {text(key)}</summary>'
                + "".join(f"<p>{line}</p>" for line in lines)
                + "</details>"
            )
        total = len(coverage)
        failed = sum("error" in row for row in payload["rows"])
        lead = f"Material coverage · {total - failed}/{total} reviewed · {failed} failed · Snapshot {text(payload['snapshot'])}"
        csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
        return (
            f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}"><title>{text(title)}</title>'
            f'<style>{STYLE}</style></head><body><main><p class="meta">{lead}</p>{content}'
            f"<h2>Evidence / 引用证据</h2>{''.join(evidence)}"
            f"<h2>Source coverage / 来源覆盖</h2>{''.join(coverage)}</main></body></html>"
        )


def main() -> None:
    payload = json.loads(Path(sys.argv[2]).read_text())
    report = Report(payload["documents"], payload["limit"])
    rendered = report.render(payload)
    encoded = rendered.encode()
    if len(encoded) > payload["limit"]:
        raise ValueError("Report exceeds the delivery limit. Use fewer or smaller visuals.")
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    target = ROOT / "projects" / "reports" / f"insightful-report-{digest}.html"
    atomic(target, rendered)
    print(
        json.dumps(
            {
                "path": str(target),
                "citations": len(report.citations),
                "visuals": report.visuals,
                "source_figures": report.source_figures,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, OverflowError) as error:
        print(
            json.dumps(
                {
                    "error": str(error)[:600]
                    if isinstance(error, ValueError)
                    else "Report could not be rendered"
                }
            )
        )
        sys.exit(1)
