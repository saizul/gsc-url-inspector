"""Compare two runs of the same property."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import InspectionRecord

__all__ = ["CHANGE_KINDS", "Change", "RunDiff", "diff_records"]

CHANGE_KINDS = (
    "newly_not_indexed",
    "newly_indexed",
    "coverage_changed",
    "google_canonical_changed",
    "user_canonical_changed",
    "indexing_state_changed",
    "robots_changed",
    "rich_results_changed",
    "new_url",
    "removed_url",
)

_LABELS = {
    "newly_not_indexed": "Newly not indexed",
    "newly_indexed": "Newly indexed",
    "coverage_changed": "Coverage state changed",
    "google_canonical_changed": "Google-selected canonical changed",
    "user_canonical_changed": "User-declared canonical changed",
    "indexing_state_changed": "Indexing state changed",
    "robots_changed": "robots.txt state changed",
    "rich_results_changed": "Rich result types changed",
    "new_url": "New URL (not in the previous run)",
    "removed_url": "URL missing from this run",
}


@dataclass(frozen=True)
class Change:
    kind: str
    url: str
    before: str | None = None
    after: str | None = None

    @property
    def label(self) -> str:
        return _LABELS[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "before": self.before, "after": self.after}


@dataclass
class RunDiff:
    old_run: int | None
    new_run: int | None
    changes: list[Change] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Change]:
        return [c for c in self.changes if c.kind == kind]

    def counts(self) -> dict[str, int]:
        return {k: len(self.by_kind(k)) for k in CHANGE_KINDS if self.by_kind(k)}

    @property
    def regressions(self) -> list[Change]:
        """Changes that usually need attention: lost indexing and a different Google canonical."""
        return [c for c in self.changes if c.kind in ("newly_not_indexed", "google_canonical_changed")]

    def to_dict(self) -> dict[str, Any]:
        return {
            "oldRun": self.old_run,
            "newRun": self.new_run,
            "counts": self.counts(),
            "changes": [c.to_dict() for c in self.changes],
        }

    @staticmethod
    def label(kind: str) -> str:
        return _LABELS[kind]


def _norm(url: str | None) -> str | None:
    return url.rstrip("/") if url else url


def diff_records(
    old: Iterable[InspectionRecord],
    new: Iterable[InspectionRecord],
    old_run: int | None = None,
    new_run: int | None = None,
    include_membership: bool = False,
) -> RunDiff:
    """Compare results URL by URL. Records that failed to inspect in either run are not compared, so a transient
    API error does not show up as "newly not indexed"."""
    before = {r.url: r for r in old}
    after = {r.url: r for r in new}
    diff = RunDiff(old_run, new_run)
    for url, cur in after.items():
        prev = before.get(url)
        if prev is None:
            if include_membership:
                diff.changes.append(Change("new_url", url, None, cur.coverage))
            continue
        if not prev.ok or not cur.ok:
            continue
        if prev.is_indexed and not cur.is_indexed:
            diff.changes.append(Change("newly_not_indexed", url, prev.coverage, cur.coverage))
        elif not prev.is_indexed and cur.is_indexed:
            diff.changes.append(Change("newly_indexed", url, prev.coverage, cur.coverage))
        elif prev.coverage != cur.coverage:
            diff.changes.append(Change("coverage_changed", url, prev.coverage, cur.coverage))
        pi, ci = prev.index, cur.index
        # A canonical that disappears because the page dropped out of the index is reported as not-indexed only.
        if pi.google_canonical and ci.google_canonical and _norm(pi.google_canonical) != _norm(ci.google_canonical):
            diff.changes.append(Change("google_canonical_changed", url, pi.google_canonical, ci.google_canonical))
        if _norm(pi.user_canonical) != _norm(ci.user_canonical):
            diff.changes.append(Change("user_canonical_changed", url, pi.user_canonical, ci.user_canonical))
        if pi.indexing_state and ci.indexing_state and pi.indexing_state != ci.indexing_state:
            diff.changes.append(Change("indexing_state_changed", url, pi.indexing_state, ci.indexing_state))
        if pi.robots_txt_state and ci.robots_txt_state and pi.robots_txt_state != ci.robots_txt_state:
            diff.changes.append(Change("robots_changed", url, pi.robots_txt_state, ci.robots_txt_state))
        prev_rich = " ".join(prev.rich.types) if prev.rich else ""
        cur_rich = " ".join(cur.rich.types) if cur.rich else ""
        if prev_rich != cur_rich:
            diff.changes.append(Change("rich_results_changed", url, prev_rich or None, cur_rich or None))
    if include_membership:
        for url, prev in before.items():
            if url not in after:
                diff.changes.append(Change("removed_url", url, prev.coverage, None))
    order = {k: i for i, k in enumerate(CHANGE_KINDS)}
    diff.changes.sort(key=lambda c: (order[c.kind], c.url))
    return diff
