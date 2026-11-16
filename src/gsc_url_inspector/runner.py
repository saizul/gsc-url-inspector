"""Run a bulk inspection: plan, inspect within quota, store every result, pause cleanly and resume later."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .client import ApiError, InspectionClient, PermissionDenied, QuotaExceeded
from .models import InspectionRecord, parse_inspection
from .planning import order_urls, split_fresh
from .property import Property
from .quota import DailyQuotaExhausted, QuotaLimiter
from .state import StateStore, utcnow

__all__ = ["RunOutcome", "Runner", "StopRun"]

Progress = Callable[[int, int, InspectionRecord], None]


class StopRun(Exception):
    """Raised by a progress callback (or on too many consecutive errors) to stop a run early."""


@dataclass
class RunOutcome:
    run_id: int
    status: str
    inspected: int = 0
    errors: int = 0
    pending: int = 0
    reused: int = 0
    note: str | None = None


class Runner:
    def __init__(
        self,
        client: InspectionClient,
        store: StateStore,
        prop: Property,
        limiter: QuotaLimiter | None = None,
        max_consecutive_errors: int = 25,
        progress: Progress | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.prop = prop
        self.limiter = limiter or QuotaLimiter(prop.site_url, store)
        self.max_consecutive_errors = max_consecutive_errors
        self.progress = progress
        self.reused = 0

    def plan(
        self,
        urls: Sequence[str],
        resume: bool = True,
        label: str | None = None,
        reuse_within: timedelta | None = None,
        now: datetime | None = None,
    ) -> tuple[int, list[str]]:
        """Return ``(run_id, outside_property_urls)``. With ``resume`` the latest unfinished run is continued
        and any new URLs are appended to it.

        With ``reuse_within``, every pending URL of the run whose last successful inspection (in any earlier run of
        the property) is younger than that age gets the stored result copied into this run instead of being
        inspected again. The number of copied results is kept in :attr:`reused`.
        """
        inside = [u for u in urls if self.prop.contains(u)]
        outside = [u for u in urls if not self.prop.contains(u)]
        run_id = self.store.resumable_run(self.prop.site_url) if resume else None
        if run_id is None:
            run_id = self.store.create_run(self.prop.site_url, inside, label)
        elif inside:
            self.store.add_urls(run_id, inside)
        self.reused = 0
        if reuse_within is not None:
            history = self.store.url_history(self.prop.site_url, exclude_run=run_id)
            _, fresh = split_fresh(self.store.pending_urls(run_id), history, reuse_within, now)
            for url, entry in fresh.items():
                if entry.last_ok_run_id is not None and self.store.carry_result(entry.last_ok_run_id, run_id, url):
                    self.reused += 1
        return run_id, outside

    def run(
        self, run_id: int, max_requests: int | None = None, retry_errors: bool = False, order: str = "source"
    ) -> RunOutcome:
        """Inspect the pending URLs of ``run_id``. ``order`` is one of :data:`~gsc_url_inspector.planning.ORDERS`
        and only changes the inspection order; reports keep the source order."""
        pending = self.store.pending_urls(run_id, retry_errors=retry_errors)
        if order != "source":
            pending = order_urls(pending, self.store.url_history(self.prop.site_url, exclude_run=run_id), order)
        outcome = RunOutcome(run_id, "running", reused=self.reused)
        self.store.set_status(run_id, "running")
        total = len(pending)
        consecutive_errors = 0
        status, note = "completed", None
        for position, url in enumerate(pending, 1):
            if max_requests is not None and outcome.inspected >= max_requests:
                status, note = "paused", f"stopped after --max-requests {max_requests}"
                break
            try:
                self.limiter.acquire()
            except DailyQuotaExhausted as exc:
                status, note = "paused", str(exc)
                break
            try:
                data = self.client.inspect(url, self.prop.site_url)
                record = parse_inspection(url, self.prop.site_url, data, utcnow())
                consecutive_errors = 0
            except QuotaExceeded as exc:
                if exc.daily:
                    self.limiter.exhaust_today()
                status, note = "paused", f"quota exceeded: {exc.message}"
                break
            except PermissionDenied as exc:
                # Every following URL would fail the same way, so stop instead of burning the budget.
                record = parse_inspection(url, self.prop.site_url, None, utcnow(), error=str(exc))
                self.store.save_result(run_id, record)
                outcome.inspected += 1
                outcome.errors += 1
                status, note = "failed", f"permission denied for {self.prop.site_url}: {exc.message}"
                break
            except ApiError as exc:
                record = parse_inspection(url, self.prop.site_url, None, utcnow(), error=str(exc))
                consecutive_errors += 1
            self.store.save_result(run_id, record)
            outcome.inspected += 1
            if record.error:
                outcome.errors += 1
            if self.progress is not None:
                try:
                    self.progress(position, total, record)
                except StopRun as exc:
                    status, note = "paused", str(exc) or "stopped"
                    break
            if consecutive_errors >= self.max_consecutive_errors:
                status, note = "paused", f"{consecutive_errors} consecutive errors; run paused"
                break
        outcome.pending = len(self.store.pending_urls(run_id))
        if status == "completed" and outcome.pending:
            status = "paused"
        outcome.status, outcome.note = status, note
        self.store.set_status(run_id, status, note)
        return outcome
