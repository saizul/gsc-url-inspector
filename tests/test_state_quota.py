from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import SITE, index_result
from gsc_url_inspector.models import parse_inspection
from gsc_url_inspector.quota import (
    PROJECT_QPD,
    PROJECT_QPM,
    SITE_QPD,
    SITE_QPM,
    DailyQuotaExhausted,
    QuotaLimiter,
    next_reset,
    quota_day,
)
from gsc_url_inspector.state import StateStore


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# 2026-11-14 12:00 UTC == 04:00 Pacific (PST)
NOON_UTC = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc).timestamp()


def test_documented_limits():
    assert (SITE_QPD, SITE_QPM, PROJECT_QPD, PROJECT_QPM) == (2000, 600, 10_000_000, 15_000)


def test_quota_day_uses_pacific_time():
    assert quota_day(NOON_UTC) == "2026-11-14"
    # 07:30 UTC is still the previous day in California.
    assert quota_day(datetime(2026, 11, 14, 7, 30, tzinfo=timezone.utc).timestamp()) == "2026-11-13"
    reset = next_reset(NOON_UTC)
    assert (reset.year, reset.month, reset.day, reset.hour) == (2026, 11, 15, 0)


def test_store_runs_results_and_pending(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        run = store.create_run(SITE, ["https://www.example.com/a", "https://www.example.com/b"], "first")
        assert store.pending_urls(run) == ["https://www.example.com/a", "https://www.example.com/b"]
        store.save_result(run, parse_inspection("https://www.example.com/a", SITE, index_result(), "t1"))
        store.save_result(run, parse_inspection("https://www.example.com/b", SITE, None, "t2", error="HTTP 500"))
        assert store.pending_urls(run) == []
        assert store.pending_urls(run, retry_errors=True) == ["https://www.example.com/b"]
        info = store.get_run(run)
        assert (info.total, info.done, info.errors, info.label, info.status) == (2, 2, 1, "first", "running")
        recs = store.records(run)
        assert [r.url for r in recs] == ["https://www.example.com/a", "https://www.example.com/b"]
        assert recs[0].is_indexed and recs[1].error == "HTTP 500"
        assert store.add_urls(run, ["https://www.example.com/a", "https://www.example.com/c"]) == 1
        assert store.pending_urls(run) == ["https://www.example.com/c"]


def test_store_status_resumable_and_latest(tmp_path):
    store = StateStore(tmp_path / "s.sqlite")
    r1 = store.create_run(SITE, [])
    store.set_status(r1, "completed")
    r2 = store.create_run(SITE, [])
    store.set_status(r2, "paused", "quota")
    assert store.resumable_run(SITE) == r2
    assert store.latest_run(SITE, ("completed",)) == r1
    assert store.latest_run(SITE, before=r2) == r1
    assert store.get_run(r2).note == "quota"
    assert store.get_run(r1).finished_at
    assert [r.id for r in store.list_runs()] == [r2, r1]
    assert store.list_runs("sc-domain:other.example") == []
    with pytest.raises(ValueError):
        store.set_status(r1, "bogus")
    assert store.delete_run(r1) and store.get_run(r1) is None
    with pytest.raises(KeyError):
        store.records(r1)
    store.close()


def test_store_persists_across_connections(tmp_path):
    path = tmp_path / "s.sqlite"
    with StateStore(path) as s:
        run = s.create_run(SITE, ["https://www.example.com/a"])
        s.add_quota(SITE, "2026-11-14", 5)
    with StateStore(path) as s:
        assert s.run_urls(run) == ["https://www.example.com/a"]
        assert s.quota_used(SITE, "2026-11-14") == 5


def test_store_rejects_newer_schema(tmp_path):
    path = tmp_path / "s.sqlite"
    with StateStore(path) as s:
        s.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        s.conn.commit()
    with pytest.raises(RuntimeError, match="newer version"):
        StateStore(path)


def test_limiter_daily_budget_is_shared_through_store():
    store = StateStore()
    clock = Clock(NOON_UTC)
    a = QuotaLimiter(SITE, store, per_day=3, clock=clock, sleep=clock.sleep)
    a.acquire()
    a.acquire()
    b = QuotaLimiter(SITE, store, per_day=3, clock=clock, sleep=clock.sleep)
    assert b.remaining_today() == 1
    b.acquire()
    with pytest.raises(DailyQuotaExhausted) as info:
        a.acquire()
    assert info.value.used == 3 and info.value.resets_at.day == 15
    # Another property has its own budget.
    assert QuotaLimiter("sc-domain:other.example", store, per_day=3, clock=clock).remaining_today() == 3
    # The next Pacific day starts fresh.
    clock.t += 24 * 3600
    assert a.remaining_today() == 3


def test_limiter_spaces_requests_per_minute():
    clock = Clock(NOON_UTC)
    lim = QuotaLimiter(SITE, None, per_minute=2, per_day=100, clock=clock, sleep=clock.sleep)
    lim.acquire()
    clock.t += 1
    lim.acquire()
    lim.acquire()  # third call within 60 s must wait for the first slot to expire
    assert clock.slept and abs(sum(clock.slept) - 59) < 0.5
    assert lim.used_today() == 3


def test_limiter_exhaust_today():
    store = StateStore()
    lim = QuotaLimiter(SITE, store, clock=lambda: NOON_UTC)
    lim.exhaust_today()
    assert lim.remaining_today() == 0
    mem = QuotaLimiter(SITE, None, clock=lambda: NOON_UTC)
    mem.exhaust_today()
    with pytest.raises(DailyQuotaExhausted):
        mem.acquire()


def test_limiter_rejects_bad_limits():
    with pytest.raises(ValueError):
        QuotaLimiter(SITE, per_minute=0)
