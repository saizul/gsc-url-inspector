from __future__ import annotations

import csv
import io
import json

import pytest

from conftest import SITE, index_result
from gsc_url_inspector.diff import diff_records
from gsc_url_inspector.models import parse_inspection
from gsc_url_inspector.report import CSV_FIELDS, FORMATS, render, summarize


def records():
    return [
        parse_inspection("https://www.example.com/", SITE, index_result(rich_types=("Breadcrumbs",))),
        parse_inspection("https://www.example.com/a", SITE, index_result()),
        parse_inspection(
            "https://www.example.com/b|pipe",
            SITE,
            index_result(
                "NEUTRAL",
                "Duplicate, Google chose different canonical than user",
                google_canonical="https://www.example.com/a",
                user_canonical="https://www.example.com/b",
            ),
        ),
        parse_inspection(
            "https://www.example.com/<script>",
            SITE,
            index_result("NEUTRAL", "Crawled - currently not indexed", amp={"verdict": "FAIL", "issues": []}),
        ),
        parse_inspection("https://www.example.com/err", SITE, None, error="HTTP 500: Backend Error"),
    ]


def test_summary_counts_and_grouping():
    s = summarize(records(), SITE, 7)
    assert (s.total, s.indexed, s.not_indexed, s.errors, s.canonical_mismatches) == (5, 2, 2, 1, 1)
    assert s.indexed_pct == 50.0
    assert s.by_coverage[0] == ("Submitted and indexed", 2)
    assert s.by_coverage[-1] == ("Inspection error", 1)
    assert s.rich_result_types == {"Breadcrumbs": 1}
    assert (s.amp_pages, s.amp_failing) == (1, 1)
    assert s.by_verdict == {"PASS": 2, "NEUTRAL": 2}
    d = s.to_dict()
    assert d["byCoverageState"]["Crawled - currently not indexed"] == 1 and d["runId"] == 7


def test_summary_empty():
    s = summarize([], SITE)
    assert s.total == 0 and s.indexed_pct == 0.0


def test_csv_report():
    text = render("csv", records(), SITE, 1)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == CSV_FIELDS
    assert len(rows) == 5
    assert rows[2]["canonical_mismatch"] == "yes"
    assert rows[4]["error"].startswith("HTTP 500")
    assert rows[0]["rich_types"] == "Breadcrumbs"


def test_json_report_with_diff():
    old = records()
    new = records()
    new[1] = parse_inspection(
        "https://www.example.com/a", SITE, index_result("NEUTRAL", "Crawled - currently not indexed")
    )
    diff = diff_records(old, new, 1, 2)
    doc = json.loads(render("json", new, SITE, 2, diff))
    assert doc["tool"] == "gsc-url-inspector"
    assert doc["summary"]["notIndexed"] == 3
    assert doc["diff"]["counts"] == {"newly_not_indexed": 1}
    assert doc["results"][0]["inspectionResult"]["indexStatusResult"]["verdict"] == "PASS"
    assert doc["results"][4]["error"]


def test_markdown_report_escapes_and_truncates():
    text = render("markdown", records(), SITE, 3, max_urls_per_group=1)
    assert text.startswith(f"# URL Inspection report: {SITE}")
    assert "| Submitted and indexed | 2 |" in text
    assert "https://www.example.com/b\\|pipe" in text
    assert "## Canonical mismatches" in text
    assert "... and 1 more" in text
    assert "### Inspection error (1)" in text


def test_markdown_diff_section():
    diff = diff_records(records(), records(), 1, 2)
    assert "## Changes since run 1" in render("markdown", records(), SITE, 2, diff)
    assert "No changes." in render("markdown", records(), SITE, 2, diff)


def test_html_report_is_escaped_and_self_contained():
    old = records()
    new = records()
    new[0] = parse_inspection(
        "https://www.example.com/", SITE, index_result("NEUTRAL", "Crawled - currently not indexed")
    )
    text = render("html", new, SITE, 2, diff_records(old, new, 1, 2))
    assert text.startswith("<!doctype html>")
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "Changes since run 1" in text and 'class="bad"' in text
    assert "<details><summary>Submitted and indexed (1)</summary>" in text
    assert "http" not in text.split("<style>")[1].split("</style>")[0]  # no external assets


def test_text_report():
    diff = diff_records(records(), records(), 1, 2)
    text = render("text", records(), SITE, 1, diff)
    assert "5 URL(s), 2 indexed (50.0%)" in text
    assert "Changes since run 1: none" in text
    assert "different canonical" in text


def test_unknown_format():
    with pytest.raises(ValueError, match="unknown report format"):
        render("pdf", [], SITE)
    assert set(FORMATS) == {"text", "csv", "json", "markdown", "html"}
