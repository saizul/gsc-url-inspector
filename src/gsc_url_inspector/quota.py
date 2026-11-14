"""Client-side quota keeping for the URL Inspection API.

Published limits (developers.google.com/webmaster-tools/limits, "URL inspection"):

* per site (property): 2,000 queries per day and 600 queries per minute
* per project: 10,000,000 queries per day and 15,000 queries per minute

The per-site limits are the ones a bulk run hits. The limiter spaces requests to stay under the per-minute limit and
keeps a per-property daily counter in the state store, so separate runs on the same day share one budget. The day
boundary is midnight in the Pacific time zone, which is when Google resets daily API quotas.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .state import StateStore

__all__ = [
    "PROJECT_QPD",
    "PROJECT_QPM",
    "SITE_QPD",
    "SITE_QPM",
    "DailyQuotaExhausted",
    "QuotaLimiter",
    "next_reset",
    "quota_day",
]

SITE_QPD = 2000
SITE_QPM = 600
PROJECT_QPD = 10_000_000
PROJECT_QPM = 15_000

try:
    from zoneinfo import ZoneInfo

    _PACIFIC: timezone | ZoneInfo = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - no tz database on this system
    _PACIFIC = timezone(timedelta(hours=-8), "PST")


class DailyQuotaExhausted(Exception):
    def __init__(self, site_url: str, used: int, limit: int, resets_at: datetime) -> None:
        super().__init__(f"daily inspection budget for {site_url} used up ({used}/{limit}); resets at {resets_at}")
        self.site_url = site_url
        self.used = used
        self.limit = limit
        self.resets_at = resets_at


def quota_day(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if now is None else now, tz=_PACIFIC)
    return moment.strftime("%Y-%m-%d")


def next_reset(now: float | None = None) -> datetime:
    moment = datetime.fromtimestamp(time.time() if now is None else now, tz=_PACIFIC)
    tomorrow = (moment + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_PACIFIC)


class QuotaLimiter:
    """Enforce a per-minute rate (sliding 60 s window) and a persistent per-property daily budget."""

    def __init__(
        self,
        site_url: str,
        store: StateStore | None = None,
        per_minute: int = SITE_QPM,
        per_day: int = SITE_QPD,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1 or per_day < 1:
            raise ValueError("quota limits must be positive")
        self.site_url = site_url
        self.store = store
        self.per_minute = per_minute
        self.per_day = per_day
        self.clock = clock
        self.sleep = sleep
        self._window: deque[float] = deque()
        self._local_count: dict[str, int] = {}

    def used_today(self) -> int:
        day = quota_day(self.clock())
        if self.store is not None:
            return self.store.quota_used(self.site_url, day)
        return self._local_count.get(day, 0)

    def remaining_today(self) -> int:
        return max(0, self.per_day - self.used_today())

    def acquire(self) -> None:
        """Block until one request may be sent; raise ``DailyQuotaExhausted`` when today's budget is spent."""
        now = self.clock()
        used = self.used_today()
        if used >= self.per_day:
            raise DailyQuotaExhausted(self.site_url, used, self.per_day, next_reset(now))
        while True:
            now = self.clock()
            while self._window and now - self._window[0] >= 60.0:
                self._window.popleft()
            if len(self._window) < self.per_minute:
                break
            self.sleep(max(0.01, 60.0 - (now - self._window[0])))
        self._window.append(now)
        day = quota_day(now)
        if self.store is not None:
            self.store.add_quota(self.site_url, day)
        else:
            self._local_count[day] = self._local_count.get(day, 0) + 1

    def exhaust_today(self) -> None:
        """Mark today's budget as spent (the API said so, whatever our counter thinks)."""
        day = quota_day(self.clock())
        if self.store is not None:
            self.store.set_quota(self.site_url, day, self.per_day)
        else:
            self._local_count[day] = self.per_day
