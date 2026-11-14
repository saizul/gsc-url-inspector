"""Typed view of the ``UrlInspectionResult`` returned by ``urlInspection/index:inspect``.

Field names follow the API reference (developers.google.com/webmaster-tools/v1/urlInspection.index/UrlInspectionResult).
Unknown fields are kept in ``raw`` so nothing the API returns is lost, and unknown enum values pass through as strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AMP_INDEXING_STATES",
    "CRAWLING_USER_AGENTS",
    "INDEXING_STATES",
    "PAGE_FETCH_STATES",
    "ROBOTS_TXT_STATES",
    "SEVERITIES",
    "VERDICTS",
    "AmpResult",
    "IndexStatus",
    "InspectionRecord",
    "Issue",
    "MobileUsability",
    "RichResultItem",
    "RichResults",
    "parse_inspection",
]

# Enum values as documented. PARTIAL and BLOCKED_BY_ROBOTS_TXT are marked "reserved, no longer in use".
VERDICTS = ("VERDICT_UNSPECIFIED", "PASS", "PARTIAL", "FAIL", "NEUTRAL")
ROBOTS_TXT_STATES = ("ROBOTS_TXT_STATE_UNSPECIFIED", "ALLOWED", "DISALLOWED")
INDEXING_STATES = (
    "INDEXING_STATE_UNSPECIFIED",
    "INDEXING_ALLOWED",
    "BLOCKED_BY_META_TAG",
    "BLOCKED_BY_HTTP_HEADER",
    "BLOCKED_BY_ROBOTS_TXT",
)
PAGE_FETCH_STATES = (
    "PAGE_FETCH_STATE_UNSPECIFIED",
    "SUCCESSFUL",
    "SOFT_404",
    "BLOCKED_ROBOTS_TXT",
    "NOT_FOUND",
    "ACCESS_DENIED",
    "SERVER_ERROR",
    "REDIRECT_ERROR",
    "ACCESS_FORBIDDEN",
    "BLOCKED_4XX",
    "INTERNAL_CRAWL_ERROR",
    "INVALID_URL",
)
CRAWLING_USER_AGENTS = ("CRAWLING_USER_AGENT_UNSPECIFIED", "DESKTOP", "MOBILE")
AMP_INDEXING_STATES = (
    "AMP_INDEXING_STATE_UNSPECIFIED",
    "AMP_INDEXING_ALLOWED",
    "BLOCKED_DUE_TO_NOINDEX",
    "BLOCKED_DUE_TO_EXPIRED_UNAVAILABLE_AFTER",
)
SEVERITIES = ("SEVERITY_UNSPECIFIED", "WARNING", "ERROR")


def _s(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _list_of_str(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass
class Issue:
    message: str
    severity: str | None = None
    issue_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "severity": self.severity, "issueType": self.issue_type}


@dataclass
class IndexStatus:
    verdict: str | None = None
    coverage_state: str | None = None
    robots_txt_state: str | None = None
    indexing_state: str | None = None
    last_crawl_time: str | None = None
    page_fetch_state: str | None = None
    google_canonical: str | None = None
    user_canonical: str | None = None
    crawled_as: str | None = None
    sitemaps: list[str] = field(default_factory=list)
    referring_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> IndexStatus:
        return cls(
            verdict=_s(data, "verdict"),
            coverage_state=_s(data, "coverageState"),
            robots_txt_state=_s(data, "robotsTxtState"),
            indexing_state=_s(data, "indexingState"),
            last_crawl_time=_s(data, "lastCrawlTime"),
            page_fetch_state=_s(data, "pageFetchState"),
            google_canonical=_s(data, "googleCanonical"),
            user_canonical=_s(data, "userCanonical"),
            crawled_as=_s(data, "crawledAs"),
            sitemaps=_list_of_str(data, "sitemap"),
            referring_urls=_list_of_str(data, "referringUrls"),
        )


@dataclass
class AmpResult:
    verdict: str | None = None
    amp_url: str | None = None
    robots_txt_state: str | None = None
    indexing_state: str | None = None
    amp_index_status_verdict: str | None = None
    last_crawl_time: str | None = None
    page_fetch_state: str | None = None
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> AmpResult:
        return cls(
            verdict=_s(data, "verdict"),
            amp_url=_s(data, "ampUrl"),
            robots_txt_state=_s(data, "robotsTxtState"),
            indexing_state=_s(data, "indexingState"),
            amp_index_status_verdict=_s(data, "ampIndexStatusVerdict"),
            last_crawl_time=_s(data, "lastCrawlTime"),
            page_fetch_state=_s(data, "pageFetchState"),
            issues=[Issue(str(i.get("issueMessage", "")), _s(i, "severity")) for i in _dicts(data.get("issues"))],
        )


@dataclass
class MobileUsability:
    """Deprecated by Google; still parsed when the API returns it."""

    verdict: str | None = None
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MobileUsability:
        return cls(
            verdict=_s(data, "verdict"),
            issues=[
                Issue(str(i.get("message", "")), _s(i, "severity"), _s(i, "issueType"))
                for i in _dicts(data.get("issues"))
            ],
        )


@dataclass
class RichResultItem:
    rich_result_type: str
    name: str
    issues: list[Issue] = field(default_factory=list)


@dataclass
class RichResults:
    verdict: str | None = None
    items: list[RichResultItem] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RichResults:
        items: list[RichResultItem] = []
        for detected in _dicts(data.get("detectedItems")):
            rtype = str(detected.get("richResultType", ""))
            for item in _dicts(detected.get("items")):
                issues = [Issue(str(i.get("issueMessage", "")), _s(i, "severity")) for i in _dicts(item.get("issues"))]
                items.append(RichResultItem(rtype, str(item.get("name", "")), issues))
        return cls(verdict=_s(data, "verdict"), items=items)

    @property
    def types(self) -> list[str]:
        return sorted({i.rich_result_type for i in self.items if i.rich_result_type})

    def issue_count(self, severity: str | None = None) -> int:
        return sum(1 for i in self.items for iss in i.issues if severity is None or iss.severity == severity)


@dataclass
class InspectionRecord:
    """One inspected URL, or an error for it."""

    url: str
    site_url: str
    inspected_at: str = ""
    error: str | None = None
    inspection_link: str | None = None
    index: IndexStatus = field(default_factory=IndexStatus)
    amp: AmpResult | None = None
    mobile: MobileUsability | None = None
    rich: RichResults | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived values used by the reports and the diff ----------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def is_indexed(self) -> bool:
        """True when Google reports the URL as on Google (index verdict ``PASS``)."""
        return self.ok and self.index.verdict == "PASS"

    @property
    def coverage(self) -> str:
        if self.error:
            return "Inspection error"
        return self.index.coverage_state or "Unknown"

    @property
    def canonical_mismatch(self) -> bool:
        g, u = self.index.google_canonical, self.index.user_canonical
        return bool(g and u and g.rstrip("/") != u.rstrip("/"))

    def flat(self) -> dict[str, Any]:
        """A flat row for CSV export."""
        rich = self.rich
        return {
            "url": self.url,
            "site_url": self.site_url,
            "inspected_at": self.inspected_at,
            "error": self.error or "",
            "verdict": self.index.verdict or "",
            "coverage_state": self.index.coverage_state or "",
            "indexing_state": self.index.indexing_state or "",
            "robots_txt_state": self.index.robots_txt_state or "",
            "page_fetch_state": self.index.page_fetch_state or "",
            "last_crawl_time": self.index.last_crawl_time or "",
            "crawled_as": self.index.crawled_as or "",
            "google_canonical": self.index.google_canonical or "",
            "user_canonical": self.index.user_canonical or "",
            "canonical_mismatch": "yes" if self.canonical_mismatch else "",
            "sitemaps": " ".join(self.index.sitemaps),
            "referring_urls": " ".join(self.index.referring_urls),
            "mobile_verdict": self.mobile.verdict or "" if self.mobile else "",
            "mobile_issues": "; ".join(i.issue_type or i.message for i in self.mobile.issues) if self.mobile else "",
            "rich_verdict": rich.verdict or "" if rich else "",
            "rich_types": " ".join(rich.types) if rich else "",
            "rich_errors": rich.issue_count("ERROR") if rich else 0,
            "rich_warnings": rich.issue_count("WARNING") if rich else 0,
            "amp_verdict": self.amp.verdict or "" if self.amp else "",
            "amp_url": self.amp.amp_url or "" if self.amp else "",
            "amp_indexing_state": self.amp.indexing_state or "" if self.amp else "",
            "amp_issues": len(self.amp.issues) if self.amp else 0,
            "inspection_link": self.inspection_link or "",
        }

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "url": self.url,
            "siteUrl": self.site_url,
            "inspectedAt": self.inspected_at,
            "indexed": self.is_indexed,
            "canonicalMismatch": self.canonical_mismatch,
        }
        if self.error:
            out["error"] = self.error
        if self.raw:
            out["inspectionResult"] = self.raw
        return out


def parse_inspection(
    url: str, site_url: str, payload: dict[str, Any] | None, inspected_at: str = "", error: str | None = None
) -> InspectionRecord:
    """Build a record from an API response body (``{"inspectionResult": {...}}``) or from the inner object."""
    if error is not None:
        return InspectionRecord(url, site_url, inspected_at, error=error)
    payload = payload or {}
    result = payload.get("inspectionResult", payload)
    if not isinstance(result, dict):
        return InspectionRecord(url, site_url, inspected_at, error="response has no inspectionResult")
    index = result.get("indexStatusResult")
    amp = result.get("ampResult")
    mobile = result.get("mobileUsabilityResult")
    rich = result.get("richResultsResult")
    return InspectionRecord(
        url=url,
        site_url=site_url,
        inspected_at=inspected_at,
        inspection_link=_s(result, "inspectionResultLink"),
        index=IndexStatus.from_api(index) if isinstance(index, dict) else IndexStatus(),
        amp=AmpResult.from_api(amp) if isinstance(amp, dict) else None,
        mobile=MobileUsability.from_api(mobile) if isinstance(mobile, dict) else None,
        rich=RichResults.from_api(rich) if isinstance(rich, dict) else None,
        raw=result,
    )
