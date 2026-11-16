from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import SITE, InspectApi, index_result, resp
from gsc_url_inspector.cli import EXIT_OK, EXIT_USAGE, main
from gsc_url_inspector.client import INSPECT_ENDPOINT, InspectionClient, RetryPolicy
from gsc_url_inspector.models import parse_inspection
from gsc_url_inspector.planning import UrlHistory, order_urls, parse_age, parse_timestamp, split_fresh
from gsc_url_inspector.property import Property
from gsc_url_inspector.quota import QuotaLimiter
from gsc_url_inspector.runner import Runner
from gsc_url_inspector.state import SCHEMA_VERSION, StateStore

U = [f"https://www.example.com/p{i}" for i in range(4)]
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(store: StateStore, run_id: int, url: str, when: str, verdict: str = "PASS", error: str | None = None):
    data = None if error else index_result(verdict, "Submitted and indexed" if verdict == "PASS" else "Not indexed")
    store.save_result(run_id, parse_inspection(url, SITE, data, when, error=error))


def make_runner(transport, store):
    client = InspectionClient(
        type("Creds", (), {"access_token": lambda self: "tok", "invalidate": lambda self: None})(),
        transport=transport,
        sleep=lambda s: None,
        retry=RetryPolicy(max_attempts=1),
    )
    prop = Property.parse(SITE)
    return Runner(client, store, prop, QuotaLimiter(prop.site_url, store, sleep=lambda s: None))


# -- parsing ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("7d", timedelta(days=7)),
        ("2W", timedelta(weeks=2)),
        ("3", timedelta(days=3)),
        ("1.5d", timedelta(hours=36)),
    ],
)
def test_parse_age(value, expected):
    assert parse_age(value) == expected


@pytest.mark.parametrize("value", ["", "0d", "-1d", "7 days", "d", "1y"])
def test_parse_age_rejects(value):
    with pytest.raises(ValueError):
        parse_age(value)


def test_parse_timestamp():
    assert parse_timestamp("2026-11-14T08:00:00Z") == datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
    assert parse_timestamp("2026-11-14T08:00:00") == datetime(2026, 11, 14, 8, tzinfo=timezone.utc)
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


# -- pure planning ------------------------------------------------------------------------------------------------


def hist(url: str, ok_days: float | None, verdict: str = "PASS", error: str | None = None) -> UrlHistory:
    last = at(ok_days if ok_days is not None else 1)
    return UrlHistory(
        url=url,
        last_run_id=1,
        last_inspected_at=last,
        last_error=error,
        last_verdict=None if error else verdict,
        last_ok_run_id=None if ok_days is None else 1,
        last_ok_at=None if ok_days is None else at(ok_days),
    )


def test_split_fresh_uses_last_successful_inspection():
    history = {
        U[0]: hist(U[0], 1),
        U[1]: hist(U[1], 10),
        U[2]: hist(U[2], None, error="HTTP 500"),
    }
    inspect, fresh = split_fresh(U, history, timedelta(days=7), NOW)
    assert inspect == [U[1], U[2], U[3]]
    assert list(fresh) == [U[0]]


def test_split_fresh_ignores_future_timestamps():
    future = UrlHistory(U[0], 1, "2030-01-01T00:00:00Z", None, "PASS", 1, "2030-01-01T00:00:00Z")
    inspect, fresh = split_fresh([U[0]], {U[0]: future}, timedelta(days=7), NOW)
    assert inspect == [U[0]] and not fresh


def test_order_stale_first_and_not_indexed_first():
    history = {
        U[0]: hist(U[0], 2),  # indexed, recent
        U[1]: hist(U[1], 30),  # indexed, old
        U[2]: hist(U[2], 5, verdict="NEUTRAL"),  # not indexed
        # U[3]: never inspected
    }
    assert order_urls(U, history, "source") == U
    assert order_urls(U, history, "stale-first") == [U[3], U[1], U[2], U[0]]
    assert order_urls(U, history, "not-indexed-first") == [U[2], U[3], U[1], U[0]]
    with pytest.raises(ValueError):
        order_urls(U, history, "random")


def test_order_counts_failed_urls_as_problems():
    history = {U[0]: hist(U[0], 3), U[1]: hist(U[1], None, error="HTTP 500")}
    assert order_urls(U[:2], history, "not-indexed-first") == [U[1], U[0]]
    assert order_urls(U[:2], history, "stale-first") == [U[1], U[0]]


# -- state store --------------------------------------------------------------------------------------------------


def test_url_history_and_carry_result():
    store = StateStore()
    r1 = store.create_run(SITE, U)
    save(store, r1, U[0], at(9))
    save(store, r1, U[1], at(9), error="HTTP 500")
    r2 = store.create_run(SITE, U)
    save(store, r2, U[0], at(2), verdict="NEUTRAL")
    save(store, r2, U[1], at(2), error="HTTP 503")
    other = store.create_run("sc-domain:other.example", [U[2]])
    save(store, other, U[2], at(1))

    history = store.url_history(SITE)
    assert set(history) == {U[0], U[1]}
    assert (history[U[0]].last_run_id, history[U[0]].last_indexed) == (r2, False)
    assert history[U[1]].last_ok_run_id is None and not history[U[1]].last_ok
    assert set(store.url_history(SITE, exclude_run=r2)) == {U[0], U[1]}
    assert store.url_history(SITE, exclude_run=r2)[U[0]].last_ok_run_id == r1

    r3 = store.create_run(SITE, U)
    assert store.carry_result(r2, r3, U[0]) is True
    assert store.carry_result(r2, r3, U[0]) is False  # never overwrites
    assert store.carry_result(r2, r3, U[1]) is False  # errors are not reused
    assert store.carried_count(r3) == 1
    [record] = store.records(r3)
    assert (record.url, record.inspected_at, record.index.verdict) == (U[0], at(2), "NEUTRAL")
    # Carried rows never become the source of history.
    assert store.url_history(SITE, exclude_run=r2)[U[0]].last_run_id == r1
    # A real inspection replaces the copy.
    save(store, r3, U[0], at(0))
    assert store.carried_count(r3) == 0


def test_schema_1_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, site_url TEXT NOT NULL, label TEXT,
            status TEXT NOT NULL DEFAULT 'running', started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            finished_at TEXT, note TEXT);
        CREATE TABLE run_urls (run_id INTEGER NOT NULL, position INTEGER NOT NULL, url TEXT NOT NULL,
            PRIMARY KEY (run_id, url));
        CREATE TABLE results (run_id INTEGER NOT NULL, url TEXT NOT NULL, inspected_at TEXT NOT NULL, error TEXT,
            verdict TEXT, coverage_state TEXT, indexing_state TEXT, google_canonical TEXT, user_canonical TEXT,
            last_crawl_time TEXT, raw_json TEXT, PRIMARY KEY (run_id, url));
        INSERT INTO runs VALUES (1, 'https://www.example.com/', NULL, 'completed', 'x', 'x', 'x', NULL);
        INSERT INTO run_urls VALUES (1, 0, 'https://www.example.com/p0');
        INSERT INTO results (run_id, url, inspected_at, verdict) VALUES
            (1, 'https://www.example.com/p0', '2026-11-15T00:00:00Z', 'PASS');
        """
    )
    conn.commit()
    conn.close()
    with StateStore(path) as store:
        assert store.url_history(SITE)[U[0]].last_ok_run_id == 1
        version = store.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        assert int(version) == SCHEMA_VERSION == 2


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "new.sqlite"
    with StateStore(path) as store:
        store.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        store.conn.commit()
    with pytest.raises(RuntimeError, match="newer version"):
        StateStore(path)


# -- runner -------------------------------------------------------------------------------------------------------


def test_runner_reuses_fresh_results_and_orders_pending(transport):
    store = StateStore()
    old = store.create_run(SITE, U)
    save(store, old, U[0], at(1))
    save(store, old, U[1], at(20))
    save(store, old, U[2], at(3), verdict="NEUTRAL")
    store.set_status(old, "completed")
    InspectApi(transport, {u: index_result() for u in U})

    runner = make_runner(transport, store)
    run_id, _ = runner.plan(U, resume=False, reuse_within=timedelta(days=7), now=NOW)
    assert runner.reused == 2  # p0 and p2 are younger than a week
    out = runner.run(run_id, order="stale-first")
    assert (out.status, out.inspected, out.reused, out.pending) == ("completed", 2, 2, 0)
    sent = [json.loads(r["body"])["inspectionUrl"] for r in transport.calls("POST", INSPECT_ENDPOINT)]
    assert sent == [U[3], U[1]]  # never inspected first, then the oldest
    records = store.records(run_id)
    assert [r.url for r in records] == U  # reports keep the source order
    assert records[0].inspected_at == at(1)


def test_runner_reuse_applies_to_resumed_runs(transport):
    store = StateStore()
    InspectApi(transport, {u: index_result() for u in U})
    paused = store.create_run(SITE, U)
    # Another run inspected p1 an hour ago, for example a manual check of a few URLs.
    other = store.create_run(SITE, [U[1]])
    save(store, other, U[1], (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    store.set_status(other, "completed")
    store.set_status(paused, "paused")
    runner = make_runner(transport, store)
    run_id, _ = runner.plan([], resume=True, reuse_within=timedelta(hours=12), now=NOW)
    assert (run_id, runner.reused) == (paused, 1)
    out = runner.run(run_id)
    assert (out.status, out.inspected, out.reused) == ("completed", 3, 1)
    assert len(transport.calls("POST", INSPECT_ENDPOINT)) == 3


def test_runner_without_reuse_inspects_everything(transport):
    store = StateStore()
    old = store.create_run(SITE, U)
    for u in U:
        save(store, old, u, at(0.1))
    store.set_status(old, "completed")
    InspectApi(transport, {u: index_result() for u in U})
    runner = make_runner(transport, store)
    run_id, _ = runner.plan(U, resume=False)
    assert runner.reused == 0
    assert runner.run(run_id).inspected == 4


# -- CLI ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def _token(monkeypatch):
    monkeypatch.setenv("GSC_ACCESS_TOKEN", "static-token")
    for var in ("GSC_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS", "GSC_SITE_URL", "GSC_URL_INSPECTOR_DB"):
        monkeypatch.delenv(var, raising=False)


def cli(transport, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), transport=transport, stdin=io.StringIO(""), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_reuse_within_and_order(transport, tmp_path, _token):
    db = str(tmp_path / "s.sqlite")
    api = InspectApi(transport, {u: index_result() for u in U})
    api.results[U[2]] = index_result("NEUTRAL", "Crawled - currently not indexed")
    code, _, err = cli(transport, "inspect", "-s", SITE, "--db", db, "-q", *U[:3])
    assert code == EXIT_OK, err
    assert len(transport.calls("POST", INSPECT_ENDPOINT)) == 3

    # Dry run: shows what a new run would inspect, in priority order, without requests.
    code, out, err = cli(
        transport, "inspect", "-s", SITE, "--db", db, "--dry-run", "--reuse-within", "1d", "--order", "stale-first", *U
    )
    assert code == EXIT_OK
    assert out.split() == [U[3]]
    assert "1 URL(s) inside" in err and "3 with a fresh result to reuse" in err

    code, out, err = cli(transport, "inspect", "-s", SITE, "--db", db, "--dry-run", "--order", "not-indexed-first", *U)
    assert out.split() == [U[2], U[3], U[0], U[1]]

    code, out, err = cli(
        transport, "inspect", "-s", SITE, "--db", db, "--new-run", "--reuse-within", "7d", "-f", "json", *U
    )
    assert code == EXIT_OK, err
    assert "Reused 3 result(s)" in err
    assert len(transport.calls("POST", INSPECT_ENDPOINT)) == 4  # only p3 was sent
    report = json.loads(out)
    assert len(report["results"]) == 4
    # Nothing changed between the runs, so the reused results produce no diff entries.
    assert not report["diff"]["changes"]


def test_cli_rejects_bad_age(transport, tmp_path, _token, capsys):
    code, _, err = cli(
        transport, "inspect", "-s", SITE, "--db", str(tmp_path / "s.sqlite"), "--reuse-within", "soon", U[0]
    )
    assert code == EXIT_USAGE
    assert "invalid age 'soon'" in err + capsys.readouterr().err


def test_cli_permission_denied_still_reports_reuse(transport, tmp_path, _token):
    db = str(tmp_path / "s.sqlite")
    InspectApi(transport, {U[0]: index_result()})
    assert cli(transport, "inspect", "-s", SITE, "--db", db, "-q", U[0])[0] == EXIT_OK
    InspectApi(
        transport, {U[1]: resp(403, {"error": {"code": 403, "message": "no access", "status": "PERMISSION_DENIED"}})}
    )
    code, _, err = cli(transport, "inspect", "-s", SITE, "--db", db, "--new-run", "--reuse-within", "2h", "-q", *U[:2])
    assert code != EXIT_OK
    assert "Reused" not in err  # --quiet
