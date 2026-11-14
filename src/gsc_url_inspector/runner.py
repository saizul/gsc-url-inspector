"""Run a bulk inspection: plan, inspect within quota, store every result, pause cleanly and resume later."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .client import ApiError, InspectionClient, PermissionDenied, QuotaExceeded
from .models import InspectionRecord, parse_inspection
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

    def plan(self, urls: Sequence[str], resume: bool = True, label: str | None = None) -> tuple[int, list[str]]:
        """Return ``(run_id, outside_property_urls)``. With ``resume`` the latest unfinished run is continued
        and any new URLs are appended to it."""
        inside = [u for u in urls if self.prop.contains(u)]
        outside = [u for u in urls if not self.prop.contains(u)]
        run_id = self.store.resumable_run(self.prop.site_url) if resume else None
        if run_id is None:
            run_id = self.store.create_run(self.prop.site_url, inside, label)
        elif inside:
            self.store.add_urls(run_id, inside)
        return run_id, outside

    def run(self, run_id: int, max_requests: int | None = None, retry_errors: bool = False) -> RunOutcome:
        pending = self.store.pending_urls(run_id, retry_errors=retry_errors)
        outcome = RunOutcome(run_id, "running")
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
