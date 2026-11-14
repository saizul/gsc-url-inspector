"""SQLite state store: runs, the URL list of each run, results and per-property quota usage.

A run is created with its full URL list, so an interrupted or quota-paused run can be resumed later and only the URLs
without a stored result are inspected again. Every result is committed as soon as it arrives.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import InspectionRecord, parse_inspection

__all__ = ["RUN_STATUSES", "RunInfo", "StateStore", "default_db_path", "utcnow"]

RUN_STATUSES = ("running", "paused", "completed", "failed")
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url TEXT NOT NULL,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS run_urls (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS results (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    error TEXT,
    verdict TEXT,
    coverage_state TEXT,
    indexing_state TEXT,
    google_canonical TEXT,
    user_canonical TEXT,
    last_crawl_time TEXT,
    raw_json TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE INDEX IF NOT EXISTS idx_runs_site ON runs(site_url, id);
CREATE TABLE IF NOT EXISTS quota_usage (
    site_url TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site_url, day)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_db_path() -> Path:
    env = os.environ.get("GSC_URL_INSPECTOR_DB")
    return Path(env).expanduser() if env else Path("gsc-inspections.sqlite")


@dataclass(frozen=True)
class RunInfo:
    id: int
    site_url: str
    label: str | None
    status: str
    started_at: str
    updated_at: str
    finished_at: str | None
    total: int
    done: int
    errors: int
    note: str | None = None

    @property
    def pending(self) -> int:
        return self.total - self.done


class StateStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} was written by a newer version (schema {row['value']})")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # -- runs ----------------------------------------------------------------------------------------------------

    def create_run(self, site_url: str, urls: Iterable[str], label: str | None = None) -> int:
        now = utcnow()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs (site_url, label, status, started_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
                (site_url, label, now, now),
            )
            run_id = int(cur.lastrowid or 0)
            c.executemany(
                "INSERT OR IGNORE INTO run_urls (run_id, position, url) VALUES (?, ?, ?)",
                ((run_id, i, u) for i, u in enumerate(urls)),
            )
        return run_id

    def add_urls(self, run_id: int, urls: Iterable[str]) -> int:
        with self._tx() as c:
            start = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM run_urls WHERE run_id = ?", (run_id,))
            pos = int(start.fetchone()[0])
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO run_urls (run_id, position, url) VALUES (?, ?, ?)",
                ((run_id, pos + i, u) for i, u in enumerate(urls)),
            )
            return c.total_changes - before

    def set_status(self, run_id: int, status: str, note: str | None = None) -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        now = utcnow()
        finished = now if status in ("completed", "failed") else None
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET status = ?, updated_at = ?, finished_at = ?, note = COALESCE(?, note) WHERE id = ?",
                (status, now, finished, note, run_id),
            )

    def get_run(self, run_id: int) -> RunInfo | None:
        rows = self._run_rows("WHERE r.id = ?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self, site_url: str | None = None, limit: int = 50) -> list[RunInfo]:
        if site_url:
            return self._run_rows("WHERE r.site_url = ? ORDER BY r.id DESC LIMIT ?", (site_url, limit))
        return self._run_rows("ORDER BY r.id DESC LIMIT ?", (limit,))

    def latest_run(
        self, site_url: str, statuses: Iterable[str] = RUN_STATUSES, before: int | None = None
    ) -> int | None:
        statuses = tuple(statuses)
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT id FROM runs WHERE site_url = ? AND status IN ({marks})"
        params: list[object] = [site_url, *statuses]
        if before is not None:
            sql += " AND id < ?"
            params.append(before)
        row = self.conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return int(row["id"]) if row else None

    def resumable_run(self, site_url: str) -> int | None:
        return self.latest_run(site_url, ("running", "paused"))

    def _run_rows(self, where: str, params: tuple) -> list[RunInfo]:
        sql = f"""
            SELECT r.*,
                   (SELECT COUNT(*) FROM run_urls u WHERE u.run_id = r.id) AS total,
                   (SELECT COUNT(*) FROM results s WHERE s.run_id = r.id) AS done,
                   (SELECT COUNT(*) FROM results s WHERE s.run_id = r.id AND s.error IS NOT NULL) AS errors
            FROM runs r {where}
        """
        return [
            RunInfo(
                id=row["id"],
                site_url=row["site_url"],
                label=row["label"],
                status=row["status"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                finished_at=row["finished_at"],
                total=row["total"],
                done=row["done"],
                errors=row["errors"],
                note=row["note"],
            )
            for row in self.conn.execute(sql, params)
        ]

    def delete_run(self, run_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount > 0

    # -- URLs and results ----------------------------------------------------------------------------------------

    def run_urls(self, run_id: int) -> list[str]:
        rows = self.conn.execute("SELECT url FROM run_urls WHERE run_id = ? ORDER BY position", (run_id,))
        return [r["url"] for r in rows]

    def pending_urls(self, run_id: int, retry_errors: bool = False) -> list[str]:
        cond = "s.url IS NULL" + (" OR s.error IS NOT NULL" if retry_errors else "")
        rows = self.conn.execute(
            f"""SELECT u.url FROM run_urls u
                LEFT JOIN results s ON s.run_id = u.run_id AND s.url = u.url
                WHERE u.run_id = ? AND ({cond}) ORDER BY u.position""",
            (run_id,),
        )
        return [r["url"] for r in rows]

    def save_result(self, run_id: int, record: InspectionRecord) -> None:
        idx = record.index
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO results
                   (run_id, url, inspected_at, error, verdict, coverage_state, indexing_state,
                    google_canonical, user_canonical, last_crawl_time, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    record.url,
                    record.inspected_at or utcnow(),
                    record.error,
                    idx.verdict,
                    idx.coverage_state,
                    idx.indexing_state,
                    idx.google_canonical,
                    idx.user_canonical,
                    idx.last_crawl_time,
                    json.dumps(record.raw, sort_keys=True) if record.raw else None,
                ),
            )
            c.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (utcnow(), run_id))

    def records(self, run_id: int) -> list[InspectionRecord]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"no run {run_id}")
        rows = self.conn.execute(
            """SELECT s.* FROM results s JOIN run_urls u ON u.run_id = s.run_id AND u.url = s.url
               WHERE s.run_id = ? ORDER BY u.position""",
            (run_id,),
        )
        out: list[InspectionRecord] = []
        for row in rows:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else None
            out.append(parse_inspection(row["url"], run.site_url, raw, row["inspected_at"], error=row["error"]))
        return out

    # -- quota -----------------------------------------------------------------------------------------------------

    def quota_used(self, site_url: str, day: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM quota_usage WHERE site_url = ? AND day = ?", (site_url, day)
        ).fetchone()
        return int(row["count"]) if row else 0

    def add_quota(self, site_url: str, day: str, n: int = 1) -> int:
        with self._tx() as c:
            c.execute(
                """INSERT INTO quota_usage (site_url, day, count) VALUES (?, ?, ?)
                   ON CONFLICT(site_url, day) DO UPDATE SET count = count + excluded.count""",
                (site_url, day, n),
            )
        return self.quota_used(site_url, day)

    def set_quota(self, site_url: str, day: str, count: int) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT INTO quota_usage (site_url, day, count) VALUES (?, ?, ?)
                   ON CONFLICT(site_url, day) DO UPDATE SET count = excluded.count""",
                (site_url, day, count),
            )
