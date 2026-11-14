"""CSV, JSON, Markdown, HTML and plain-text renderers."""

from __future__ import annotations

import csv
import html
import io
import json
from collections.abc import Sequence
from datetime import datetime, timezone

from .._version import __version__
from ..diff import RunDiff
from ..models import InspectionRecord
from .summary import Summary, summarize

__all__ = ["CSV_FIELDS", "render_csv", "render_html", "render_json", "render_markdown", "render_text"]

CSV_FIELDS = list(InspectionRecord("", "").flat().keys())


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def render_csv(records: Sequence[InspectionRecord], **_: object) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for r in records:
        writer.writerow(r.flat())
    return buf.getvalue()


def render_json(
    records: Sequence[InspectionRecord],
    summary: Summary | None = None,
    diff: RunDiff | None = None,
    generated_at: str | None = None,
    **_: object,
) -> str:
    summary = summary or summarize(records)
    doc = {
        "tool": "gsc-url-inspector",
        "version": __version__,
        "generatedAt": generated_at or _now(),
        "summary": summary.to_dict(),
        "diff": diff.to_dict() if diff else None,
        "results": [r.to_dict() for r in records],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(
    records: Sequence[InspectionRecord],
    summary: Summary | None = None,
    diff: RunDiff | None = None,
    max_urls_per_group: int = 50,
    generated_at: str | None = None,
    **_: object,
) -> str:
    s = summary or summarize(records)
    out: list[str] = [f"# URL Inspection report: {s.site_url}", ""]
    meta = f"Run {s.run_id}, " if s.run_id is not None else ""
    out += [f"{meta}generated {generated_at or _now()} by gsc-url-inspector {__version__}.", ""]
    out += [
        "| Metric | Value |",
        "| --- | ---: |",
        f"| URLs inspected | {s.total} |",
        f"| Indexed (verdict PASS) | {s.indexed} ({s.indexed_pct}%) |",
        f"| Not indexed | {s.not_indexed} |",
        f"| Inspection errors | {s.errors} |",
        f"| Google canonical differs from user canonical | {s.canonical_mismatches} |",
        f"| Rich result errors / warnings | {s.rich_errors} / {s.rich_warnings} |",
        f"| AMP pages (failing) | {s.amp_pages} ({s.amp_failing}) |",
        "",
        "## Coverage state",
        "",
        "| Coverage state | URLs |",
        "| --- | ---: |",
    ]
    out += [f"| {_md(k)} | {v} |" for k, v in s.by_coverage]
    out.append("")
    if diff is not None:
        out += [f"## Changes since run {diff.old_run}", ""]
        if not diff.changes:
            out += ["No changes.", ""]
        else:
            out += ["| Change | URL | Before | After |", "| --- | --- | --- | --- |"]
            out += [
                f"| {_md(c.label)} | {_md(c.url)} | {_md(c.before or '')} | {_md(c.after or '')} |"
                for c in diff.changes
            ]
            out.append("")
    mismatches = [r for r in records if r.canonical_mismatch]
    if mismatches:
        out += ["## Canonical mismatches", "", "| URL | User canonical | Google canonical |", "| --- | --- | --- |"]
        out += [
            f"| {_md(r.url)} | {_md(r.index.user_canonical)} | {_md(r.index.google_canonical)} |" for r in mismatches
        ]
        out.append("")
    out += ["## URLs by coverage state", ""]
    for state, group in s.groups.items():
        out += [f"### {state} ({len(group)})", ""]
        for r in group[:max_urls_per_group]:
            extra = f" ({r.error})" if r.error else ""
            if not r.error and r.index.last_crawl_time:
                extra = f" (last crawl {r.index.last_crawl_time})"
            out.append(f"- {r.url}{extra}")
        if len(group) > max_urls_per_group:
            out.append(f"- ... and {len(group) - max_urls_per_group} more (see the CSV or JSON report)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


_CSS = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem auto;max-width:1100px;
padding:0 1rem;color:#1f2328}
h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:2rem}table{border-collapse:collapse;width:100%;margin:.5rem 0}
th,td{border:1px solid #d0d7de;padding:.35rem .6rem;text-align:left;vertical-align:top;word-break:break-all}
th{background:#f6f8fa}td.n{text-align:right;white-space:nowrap}.cards{display:flex;gap:1rem;flex-wrap:wrap}
.card{border:1px solid #d0d7de;border-radius:8px;padding:.75rem 1rem;min-width:150px}
.card b{display:block;font-size:1.6rem}.bar{background:#eaeef2;border-radius:4px;height:8px;min-width:80px}
.bar span{display:block;height:8px;border-radius:4px;background:#1f883d}.bad{color:#cf222e}
details{margin:.4rem 0}summary{cursor:pointer;font-weight:600}small{color:#57606a}
""".strip()


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_html(
    records: Sequence[InspectionRecord],
    summary: Summary | None = None,
    diff: RunDiff | None = None,
    generated_at: str | None = None,
    **_: object,
) -> str:
    s = summary or summarize(records)
    total = max(1, s.total)
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>URL Inspection report: {_e(s.site_url)}</title><style>{_CSS}</style></head><body>",
        f"<h1>URL Inspection report: {_e(s.site_url)}</h1>",
        f"<p><small>{'Run ' + _e(s.run_id) + ', ' if s.run_id is not None else ''}generated "
        f"{_e(generated_at or _now())} by gsc-url-inspector {_e(__version__)}</small></p>",
        '<div class="cards">',
        f'<div class="card"><b>{s.total}</b>URLs inspected</div>',
        f'<div class="card"><b>{s.indexed}</b>indexed ({s.indexed_pct}%)</div>',
        f'<div class="card"><b>{s.not_indexed}</b>not indexed</div>',
        f'<div class="card"><b>{s.canonical_mismatches}</b>canonical mismatches</div>',
        f'<div class="card"><b class="{"bad" if s.errors else ""}">{s.errors}</b>inspection errors</div>',
        "</div>",
        "<h2>Coverage state</h2><table><tr><th>Coverage state</th><th>URLs</th><th>Share</th></tr>",
    ]
    for state, count in s.by_coverage:
        pct = round(100 * count / total, 1)
        parts.append(
            f'<tr><td>{_e(state)}</td><td class="n">{count}</td>'
            f'<td><div class="bar"><span style="width:{pct}%"></span></div><small>{pct}%</small></td></tr>'
        )
    parts.append("</table>")
    if diff is not None:
        parts.append(f"<h2>Changes since run {_e(diff.old_run)}</h2>")
        if not diff.changes:
            parts.append("<p>No changes.</p>")
        else:
            parts.append("<table><tr><th>Change</th><th>URL</th><th>Before</th><th>After</th></tr>")
            for c in diff.changes:
                cls = ' class="bad"' if c in diff.regressions else ""
                parts.append(
                    f"<tr><td{cls}>{_e(c.label)}</td><td>{_e(c.url)}</td>"
                    f"<td>{_e(c.before)}</td><td>{_e(c.after)}</td></tr>"
                )
            parts.append("</table>")
    mismatches = [r for r in records if r.canonical_mismatch]
    if mismatches:
        parts.append("<h2>Canonical mismatches</h2><table><tr><th>URL</th><th>User canonical</th>")
        parts.append("<th>Google canonical</th></tr>")
        for r in mismatches:
            parts.append(
                f"<tr><td>{_e(r.url)}</td><td>{_e(r.index.user_canonical)}</td>"
                f"<td>{_e(r.index.google_canonical)}</td></tr>"
            )
        parts.append("</table>")
    parts.append("<h2>URLs by coverage state</h2>")
    for state, group in s.groups.items():
        parts.append(f"<details><summary>{_e(state)} ({len(group)})</summary><table>")
        parts.append("<tr><th>URL</th><th>Indexing</th><th>Fetch</th><th>Last crawl</th><th>Rich results</th></tr>")
        for r in group:
            link = f'<a href="{_e(r.inspection_link)}">{_e(r.url)}</a>' if r.inspection_link else _e(r.url)
            if r.error:
                parts.append(f'<tr><td>{link}</td><td colspan="4" class="bad">{_e(r.error)}</td></tr>')
                continue
            rich = " ".join(r.rich.types) if r.rich else ""
            parts.append(
                f"<tr><td>{link}</td><td>{_e(r.index.indexing_state)}</td><td>{_e(r.index.page_fetch_state)}</td>"
                f"<td>{_e(r.index.last_crawl_time)}</td><td>{_e(rich)}</td></tr>"
            )
        parts.append("</table></details>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_text(
    records: Sequence[InspectionRecord], summary: Summary | None = None, diff: RunDiff | None = None, **_: object
) -> str:
    s = summary or summarize(records)
    width = max([len(k) for k, _ in s.by_coverage] + [10])
    lines = [
        f"{s.site_url}: {s.total} URL(s), {s.indexed} indexed ({s.indexed_pct}%), "
        f"{s.not_indexed} not indexed, {s.errors} error(s)",
        "",
    ]
    lines += [f"  {k.ljust(width)}  {v:>6}" for k, v in s.by_coverage]
    if s.canonical_mismatches:
        lines += ["", f"  {s.canonical_mismatches} URL(s) where Google chose a different canonical"]
    if diff is not None:
        lines += [
            "",
            f"Changes since run {diff.old_run}: "
            + (", ".join(f"{DIFF_SHORT.get(k, k)} {n}" for k, n in diff.counts().items()) or "none"),
        ]
        for c in diff.regressions[:20]:
            lines.append(f"  {c.label}: {c.url}  [{c.before} -> {c.after}]")
    return "\n".join(lines) + "\n"


DIFF_SHORT = {
    "newly_not_indexed": "newly not indexed",
    "newly_indexed": "newly indexed",
    "coverage_changed": "coverage changed",
    "google_canonical_changed": "Google canonical changed",
    "user_canonical_changed": "user canonical changed",
    "indexing_state_changed": "indexing state changed",
    "robots_changed": "robots changed",
    "rich_results_changed": "rich results changed",
    "new_url": "new",
    "removed_url": "removed",
}
