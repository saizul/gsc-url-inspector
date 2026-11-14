"""Report rendering."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..diff import RunDiff
from ..models import InspectionRecord
from .renderers import CSV_FIELDS, render_csv, render_html, render_json, render_markdown, render_text
from .summary import Summary, summarize

__all__ = [
    "CSV_FIELDS",
    "FORMATS",
    "Summary",
    "render",
    "render_csv",
    "render_html",
    "render_json",
    "render_markdown",
    "render_text",
    "summarize",
]

FORMATS: dict[str, Callable[..., str]] = {
    "text": render_text,
    "csv": render_csv,
    "json": render_json,
    "markdown": render_markdown,
    "html": render_html,
}

EXTENSIONS = {"csv": "csv", "json": "json", "markdown": "md", "html": "html", "text": "txt"}


def render(
    fmt: str,
    records: Sequence[InspectionRecord],
    site_url: str = "",
    run_id: int | None = None,
    diff: RunDiff | None = None,
    **kwargs: object,
) -> str:
    try:
        renderer = FORMATS[fmt]
    except KeyError:
        raise ValueError(f"unknown report format {fmt!r}; choose from {', '.join(FORMATS)}") from None
    summary = summarize(records, site_url, run_id)
    return renderer(records, summary=summary, diff=diff, **kwargs)
