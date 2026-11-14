"""Aggregate counts shared by every report format."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import InspectionRecord

__all__ = ["Summary", "summarize"]


@dataclass
class Summary:
    site_url: str
    run_id: int | None
    total: int
    indexed: int
    not_indexed: int
    errors: int
    canonical_mismatches: int
    by_coverage: list[tuple[str, int]] = field(default_factory=list)
    by_verdict: dict[str, int] = field(default_factory=dict)
    by_indexing_state: dict[str, int] = field(default_factory=dict)
    by_page_fetch: dict[str, int] = field(default_factory=dict)
    by_robots: dict[str, int] = field(default_factory=dict)
    by_crawled_as: dict[str, int] = field(default_factory=dict)
    rich_result_types: dict[str, int] = field(default_factory=dict)
    rich_errors: int = 0
    rich_warnings: int = 0
    amp_pages: int = 0
    amp_failing: int = 0
    mobile_reported: int = 0
    mobile_failing: int = 0
    groups: dict[str, list[InspectionRecord]] = field(default_factory=dict)

    @property
    def indexed_pct(self) -> float:
        ok = self.total - self.errors
        return round(100.0 * self.indexed / ok, 1) if ok else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "siteUrl": self.site_url,
            "runId": self.run_id,
            "total": self.total,
            "indexed": self.indexed,
            "notIndexed": self.not_indexed,
            "errors": self.errors,
            "indexedPercent": self.indexed_pct,
            "canonicalMismatches": self.canonical_mismatches,
            "byCoverageState": dict(self.by_coverage),
            "byVerdict": self.by_verdict,
            "byIndexingState": self.by_indexing_state,
            "byPageFetchState": self.by_page_fetch,
            "byRobotsTxtState": self.by_robots,
            "byCrawledAs": self.by_crawled_as,
            "richResults": {
                "types": self.rich_result_types,
                "errors": self.rich_errors,
                "warnings": self.rich_warnings,
            },
            "amp": {"pages": self.amp_pages, "failing": self.amp_failing},
            "mobileUsability": {"reported": self.mobile_reported, "failing": self.mobile_failing},
        }


def _count(values: Sequence[str | None]) -> dict[str, int]:
    return dict(Counter(v for v in values if v).most_common())


def summarize(records: Sequence[InspectionRecord], site_url: str = "", run_id: int | None = None) -> Summary:
    ok = [r for r in records if r.ok]
    groups: dict[str, list[InspectionRecord]] = {}
    for r in records:
        groups.setdefault(r.coverage, []).append(r)
    # Biggest groups first; errors always last.
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "Inspection error", -len(kv[1]), kv[0]))
    rich_types: Counter[str] = Counter()
    rich_errors = rich_warnings = 0
    for r in ok:
        if r.rich:
            rich_types.update(r.rich.types)
            rich_errors += r.rich.issue_count("ERROR")
            rich_warnings += r.rich.issue_count("WARNING")
    return Summary(
        site_url=site_url or (records[0].site_url if records else ""),
        run_id=run_id,
        total=len(records),
        indexed=sum(1 for r in ok if r.is_indexed),
        not_indexed=sum(1 for r in ok if not r.is_indexed),
        errors=len(records) - len(ok),
        canonical_mismatches=sum(1 for r in ok if r.canonical_mismatch),
        by_coverage=[(k, len(v)) for k, v in ordered],
        by_verdict=_count([r.index.verdict for r in ok]),
        by_indexing_state=_count([r.index.indexing_state for r in ok]),
        by_page_fetch=_count([r.index.page_fetch_state for r in ok]),
        by_robots=_count([r.index.robots_txt_state for r in ok]),
        by_crawled_as=_count([r.index.crawled_as for r in ok]),
        rich_result_types=dict(rich_types.most_common()),
        rich_errors=rich_errors,
        rich_warnings=rich_warnings,
        amp_pages=sum(1 for r in ok if r.amp),
        amp_failing=sum(1 for r in ok if r.amp and r.amp.verdict == "FAIL"),
        mobile_reported=sum(1 for r in ok if r.mobile),
        mobile_failing=sum(1 for r in ok if r.mobile and r.mobile.verdict == "FAIL"),
        groups=dict(ordered),
    )
