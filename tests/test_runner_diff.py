from __future__ import annotations

from conftest import SITE, FakeCredentials, InspectApi, index_result, resp
from gsc_url_inspector.client import InspectionClient, RetryPolicy
from gsc_url_inspector.diff import diff_records
from gsc_url_inspector.models import parse_inspection
from gsc_url_inspector.property import Property
from gsc_url_inspector.quota import QuotaLimiter
from gsc_url_inspector.runner import Runner, StopRun
from gsc_url_inspector.state import StateStore

U = [f"https://www.example.com/p{i}" for i in range(5)]


def make_runner(transport, store, per_day=2000, progress=None, max_errors=25):
    client = InspectionClient(
        FakeCredentials(), transport=transport, sleep=lambda s: None, retry=RetryPolicy(max_attempts=1)
    )
    prop = Property.parse(SITE)
    limiter = QuotaLimiter(prop.site_url, store, per_day=per_day, sleep=lambda s: None)
    return Runner(client, store, prop, limiter, max_consecutive_errors=max_errors, progress=progress)


def test_full_run_completes(transport):
    InspectApi(transport, {u: index_result() for u in U})
    store = StateStore()
    runner = make_runner(transport, store)
    run_id, outside = runner.plan([*U, "https://other.example/x"])
    assert outside == ["https://other.example/x"]
    out = runner.run(run_id)
    assert (out.status, out.inspected, out.errors, out.pending) == ("completed", 5, 0, 0)
    assert all(r.is_indexed for r in store.records(run_id))
    assert store.get_run(run_id).status == "completed"


def test_daily_budget_pauses_and_resume_continues(transport):
    InspectApi(transport, {u: index_result() for u in U})
    store = StateStore()
    runner = make_runner(transport, store, per_day=3)
    run_id, _ = runner.plan(U)
    out = runner.run(run_id)
    assert (out.status, out.inspected, out.pending) == ("paused", 3, 2)
    assert "budget" in out.note
    # Same day: still exhausted, nothing is sent.
    sent = len(transport.requests)
    again = make_runner(transport, store, per_day=3)
    rid, _ = again.plan(U)
    assert rid == run_id
    assert again.run(rid).inspected == 0
    assert len(transport.requests) == sent
    # Bigger budget (e.g. the next day): the remaining two are inspected, nothing twice.
    later = make_runner(transport, store, per_day=10)
    rid, _ = later.plan(U)
    out = later.run(rid)
    assert (out.status, out.inspected) == ("completed", 2)
    assert len(transport.requests) == 5


def test_api_daily_quota_response_pauses_and_exhausts(transport):
    results = {u: index_result() for u in U}
    results[U[2]] = resp(
        429, {"error": {"code": 429, "message": "Quota exceeded: queries per day", "status": "RESOURCE_EXHAUSTED"}}
    )
    InspectApi(transport, results)
    store = StateStore()
    runner = make_runner(transport, store)
    run_id, _ = runner.plan(U)
    out = runner.run(run_id)
    assert out.status == "paused" and out.inspected == 2
    assert runner.limiter.remaining_today() == 0


def test_errors_are_stored_and_retryable(transport):
    results = {u: index_result() for u in U}
    results[U[1]] = resp(400, {"error": {"code": 400, "message": "Invalid", "status": "INVALID_ARGUMENT"}})
    api = InspectApi(transport, results)
    store = StateStore()
    runner = make_runner(transport, store)
    run_id, _ = runner.plan(U)
    out = runner.run(run_id)
    assert out.status == "completed" and out.errors == 1
    api.results[U[1]] = index_result()
    out = runner.run(run_id, retry_errors=True)
    assert out.inspected == 1 and out.errors == 0
    assert store.get_run(run_id).errors == 0


def test_permission_denied_fails_the_run(transport):
    results = {
        u: resp(403, {"error": {"code": 403, "message": "You do not own this site", "status": "PERMISSION_DENIED"}})
        for u in U
    }
    InspectApi(transport, results)
    store = StateStore()
    runner = make_runner(transport, store)
    run_id, _ = runner.plan(U)
    out = runner.run(run_id)
    assert out.status == "failed" and out.inspected == 1
    assert len(transport.requests) == 1


def test_consecutive_errors_pause(transport):
    InspectApi(transport, {u: resp(400, "bad") for u in U})
    store = StateStore()
    runner = make_runner(transport, store, max_errors=2)
    run_id, _ = runner.plan(U)
    out = runner.run(run_id)
    assert out.status == "paused" and out.inspected == 2


def test_max_requests_and_progress_stop(transport):
    InspectApi(transport, {u: index_result() for u in U})
    store = StateStore()
    seen = []
    runner = make_runner(transport, store, progress=lambda pos, total, rec: seen.append((pos, total)))
    run_id, _ = runner.plan(U)
    out = runner.run(run_id, max_requests=2)
    assert out.status == "paused" and out.inspected == 2 and seen == [(1, 5), (2, 5)]

    def stopper(pos, total, rec):
        raise StopRun("user stop")

    runner.progress = stopper
    out = runner.run(run_id)
    assert out.status == "paused" and out.note == "user stop" and out.inspected == 1


def test_new_run_when_resume_disabled(transport):
    InspectApi(transport, {})
    store = StateStore()
    runner = make_runner(transport, store)
    a, _ = runner.plan(U[:1])
    b, _ = runner.plan(U[:1], resume=False)
    assert a != b


# -- diff ---------------------------------------------------------------------------------------------------------


def rec(url, **kw):
    return parse_inspection(url, SITE, index_result(**kw))


def test_diff_detects_changes():
    old = [
        rec("https://www.example.com/a"),
        rec("https://www.example.com/b", verdict="NEUTRAL", coverage="Discovered - currently not indexed"),
        rec(
            "https://www.example.com/c",
            google_canonical="https://www.example.com/c",
            user_canonical="https://www.example.com/c",
        ),
        rec("https://www.example.com/d", verdict="NEUTRAL", coverage="Crawled - currently not indexed"),
        rec("https://www.example.com/gone"),
        parse_inspection("https://www.example.com/err", SITE, None, error="boom"),
    ]
    new = [
        rec(
            "https://www.example.com/a",
            verdict="NEUTRAL",
            coverage="Crawled - currently not indexed",
            indexing_state="BLOCKED_BY_META_TAG",
        ),
        rec("https://www.example.com/b"),
        rec(
            "https://www.example.com/c",
            google_canonical="https://www.example.com/c2",
            user_canonical="https://www.example.com/c/",
            rich_types=("FAQ",),
        ),
        rec(
            "https://www.example.com/d",
            verdict="NEUTRAL",
            coverage="Discovered - currently not indexed",
            robots="DISALLOWED",
        ),
        rec("https://www.example.com/new"),
        rec("https://www.example.com/err", verdict="NEUTRAL", coverage="Crawled - currently not indexed"),
    ]
    d = diff_records(old, new, 1, 2, include_membership=True)
    kinds = {(c.kind, c.url.rsplit("/", 1)[1]) for c in d.changes}
    assert ("newly_not_indexed", "a") in kinds
    assert ("indexing_state_changed", "a") in kinds
    assert ("newly_indexed", "b") in kinds
    assert ("google_canonical_changed", "c") in kinds
    assert ("user_canonical_changed", "c") not in kinds  # trailing slash only
    assert ("rich_results_changed", "c") in kinds
    assert ("coverage_changed", "d") in kinds
    assert ("robots_changed", "d") in kinds
    assert ("new_url", "new") in kinds and ("removed_url", "gone") in kinds
    assert not any(u == "err" for _, u in kinds)  # errors are not compared
    assert {c.kind for c in d.regressions} == {"newly_not_indexed", "google_canonical_changed"}
    assert d.counts()["newly_not_indexed"] == 1
    assert d.to_dict()["oldRun"] == 1
    # Ordered by kind: regressions first.
    assert d.changes[0].kind == "newly_not_indexed"


def test_diff_without_membership_and_no_changes():
    same = [rec("https://www.example.com/a")]
    assert diff_records(same, same).changes == []
    assert diff_records([], same).changes == []


def test_diff_dropped_canonical_is_not_a_canonical_change():
    old = [rec("https://www.example.com/a", google_canonical="https://www.example.com/a")]
    new = [rec("https://www.example.com/a", verdict="NEUTRAL", coverage="Excluded by 'noindex' tag")]
    kinds = [c.kind for c in diff_records(old, new).changes]
    assert kinds == ["newly_not_indexed"]
