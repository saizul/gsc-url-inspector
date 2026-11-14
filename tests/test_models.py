from __future__ import annotations

import json
from pathlib import Path

from conftest import SITE, index_result
from gsc_url_inspector.models import (
    INDEXING_STATES,
    PAGE_FETCH_STATES,
    VERDICTS,
    InspectionRecord,
    parse_inspection,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def test_full_example_response_is_parsed():
    payload = json.loads((EXAMPLES / "inspect-response.json").read_text())
    rec = parse_inspection("https://www.example.com/blog/post", SITE, payload, "2026-11-14T00:00:00Z")
    idx = rec.index
    assert rec.ok and rec.is_indexed
    assert idx.coverage_state == "Submitted and indexed"
    assert idx.robots_txt_state == "ALLOWED"
    assert idx.indexing_state == "INDEXING_ALLOWED"
    assert idx.page_fetch_state == "SUCCESSFUL"
    assert idx.crawled_as == "MOBILE"
    assert idx.last_crawl_time == "2026-11-10T08:15:42Z"
    assert idx.google_canonical == "https://www.example.com/blog/post"
    assert idx.user_canonical == "https://www.example.com/blog/post"
    assert idx.sitemaps == ["https://www.example.com/sitemap.xml"]
    assert not rec.canonical_mismatch
    assert rec.rich is not None
    assert rec.rich.types == ["Breadcrumbs", "FAQ"]
    assert rec.rich.issue_count("WARNING") == 1
    assert rec.rich.issue_count("ERROR") == 0
    assert rec.amp is not None and rec.amp.amp_url == "https://www.example.com/blog/post/amp"
    assert rec.amp.indexing_state == "AMP_INDEXING_ALLOWED"
    assert rec.amp.issues[0].severity == "WARNING"
    assert rec.mobile is not None and rec.mobile.verdict == "PASS"
    assert rec.inspection_link.startswith("https://search.google.com/")


def test_not_indexed_with_canonical_mismatch():
    payload = index_result(
        "NEUTRAL",
        "Duplicate, Google chose different canonical than user",
        google_canonical="https://www.example.com/b",
        user_canonical="https://www.example.com/a",
    )
    rec = parse_inspection("https://www.example.com/a", SITE, payload)
    assert not rec.is_indexed
    assert rec.canonical_mismatch
    assert rec.coverage.startswith("Duplicate")


def test_trailing_slash_is_not_a_mismatch():
    payload = index_result(google_canonical="https://www.example.com/a/", user_canonical="https://www.example.com/a")
    assert not parse_inspection("https://www.example.com/a", SITE, payload).canonical_mismatch


def test_inner_object_accepted_and_missing_sections():
    rec = parse_inspection("u", SITE, {"indexStatusResult": {"verdict": "FAIL"}})
    assert rec.index.verdict == "FAIL" and rec.amp is None and rec.mobile is None and rec.rich is None
    assert rec.coverage == "Unknown"


def test_error_record():
    rec = parse_inspection("u", SITE, None, error="HTTP 500: boom")
    assert not rec.ok and not rec.is_indexed and rec.coverage == "Inspection error"
    assert rec.to_dict()["error"] == "HTTP 500: boom"


def test_bad_types_are_tolerated():
    rec = parse_inspection(
        "u",
        SITE,
        {
            "inspectionResult": {
                "indexStatusResult": {"verdict": 3, "sitemap": "x"},
                "richResultsResult": {"detectedItems": "?"},
            }
        },
    )
    assert rec.index.verdict is None and rec.index.sitemaps == []
    assert rec.rich is not None and rec.rich.items == []
    assert parse_inspection("u", SITE, {"inspectionResult": []}).error


def test_mobile_issues_flatten():
    payload = index_result(
        mobile={
            "verdict": "FAIL",
            "issues": [{"issueType": "USE_LEGIBLE_FONT_SIZES", "message": "Text too small to read"}],
        }
    )
    rec = parse_inspection("u", SITE, payload)
    assert rec.flat()["mobile_verdict"] == "FAIL"
    assert rec.flat()["mobile_issues"] == "USE_LEGIBLE_FONT_SIZES"


def test_flat_has_stable_columns():
    cols = list(InspectionRecord("", "").flat())
    assert cols[:4] == ["url", "site_url", "inspected_at", "error"]
    assert "google_canonical" in cols and "amp_verdict" in cols and "rich_types" in cols


def test_documented_enums_present():
    assert "NEUTRAL" in VERDICTS and "PARTIAL" in VERDICTS
    assert "BLOCKED_BY_HTTP_HEADER" in INDEXING_STATES
    assert {"SOFT_404", "REDIRECT_ERROR", "INVALID_URL"} <= set(PAGE_FETCH_STATES)
