"""Spend the daily budget where it matters: reuse fresh results and inspect the most useful URLs first.

The URL Inspection API allows 2,000 inspections per property and day. On a site with more URLs than that, a run takes
several days, and a weekly ``--new-run`` would otherwise inspect pages again that were checked yesterday. This module
looks at the history of every URL across all stored runs of a property and

* finds URLs whose last successful inspection is younger than ``--reuse-within`` (those results are copied into the
  new run instead of being inspected again), and
* orders the pending URLs (``--order``) so that never-inspected, long-unchecked or problem URLs come first when the
  budget runs out before the run is complete.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "ORDERS",
    "UrlHistory",
    "order_urls",
    "parse_age",
    "parse_timestamp",
    "split_fresh",
]

ORDERS = ("source", "stale-first", "not-indexed-first")

_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw]?)\s*$", re.IGNORECASE)
_AGE_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks", "": "days"}


def parse_age(value: str) -> timedelta:
    """Parse ``30m``, ``12h``, ``7d``, ``2w`` or a bare number of days into a positive ``timedelta``."""
    match = _AGE_RE.match(value or "")
    if not match:
        raise ValueError(f"invalid age {value!r}: use a number with m, h, d or w (for example 7d)")
    amount = float(match.group(1))
    if amount <= 0:
        raise ValueError(f"invalid age {value!r}: must be greater than zero")
    return timedelta(**{_AGE_UNITS[match.group(2).lower()]: amount})


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse the ISO 8601 timestamps stored in the state database (``2026-11-14T08:00:00Z``)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class UrlHistory:
    """What the state database knows about one URL across the runs of a property."""

    url: str
    last_run_id: int
    last_inspected_at: str
    last_error: str | None
    last_verdict: str | None
    last_ok_run_id: int | None = None
    last_ok_at: str | None = None

    @property
    def last_ok(self) -> bool:
        return self.last_error is None

    @property
    def last_indexed(self) -> bool:
        return self.last_ok and self.last_verdict == "PASS"

    def fresh(self, max_age: timedelta, now: datetime) -> bool:
        """True when the last successful inspection is younger than ``max_age``."""
        ts = parse_timestamp(self.last_ok_at)
        return ts is not None and timedelta(0) <= now - ts < max_age


def split_fresh(
    urls: Iterable[str], history: Mapping[str, UrlHistory], max_age: timedelta, now: datetime | None = None
) -> tuple[list[str], dict[str, UrlHistory]]:
    """Return ``(urls_to_inspect, {url: history})`` where the second part holds the URLs that can be reused."""
    now = now or datetime.now(timezone.utc)
    inspect: list[str] = []
    fresh: dict[str, UrlHistory] = {}
    for url in urls:
        entry = history.get(url)
        if entry is not None and entry.last_ok_run_id is not None and entry.fresh(max_age, now):
            fresh[url] = entry
        else:
            inspect.append(url)
    return inspect, fresh


def _age_key(entry: UrlHistory | None) -> tuple[int, datetime]:
    # Never inspected first, then the oldest successful inspection; URLs that only ever failed count as never checked.
    if entry is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    ts = parse_timestamp(entry.last_ok_at)
    if ts is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    return (1, ts)


def _problem_rank(entry: UrlHistory | None) -> int:
    if entry is None:
        return 1  # never inspected
    if not entry.last_ok:
        return 0  # the last attempt failed
    if not entry.last_indexed:
        return 0  # known to be not indexed: the most interesting URLs to recheck
    return 2


def order_urls(urls: Sequence[str], history: Mapping[str, UrlHistory], order: str = "source") -> list[str]:
    """Sort ``urls`` for inspection. The sort is stable, so ties keep the source order.

    ``source``
        as given (sitemap or file order).
    ``stale-first``
        URLs never inspected successfully, then by the age of their last successful inspection, oldest first.
    ``not-indexed-first``
        URLs that were not indexed or failed last time, then never-inspected URLs, then indexed URLs; within each
        group the oldest inspection comes first.
    """
    if order not in ORDERS:
        raise ValueError(f"unknown order {order!r}; choose from {', '.join(ORDERS)}")
    if order == "source":
        return list(urls)
    if order == "stale-first":
        return sorted(urls, key=lambda u: _age_key(history.get(u)))
    return sorted(urls, key=lambda u: (_problem_rank(history.get(u)), _age_key(history.get(u))))
