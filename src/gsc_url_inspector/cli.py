"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from ._version import __version__
from .auth import (
    SCOPE_READONLY,
    AuthError,
    InstalledAppCredentials,
    StaticTokenCredentials,
    TokenCache,
    load_credentials,
)
from .client import InspectionClient
from .diff import RunDiff, diff_records
from .http import Transport, UrllibTransport
from .models import InspectionRecord
from .property import Property, PropertyError
from .quota import SITE_QPD, SITE_QPM, QuotaLimiter, next_reset
from .report import EXTENSIONS, FORMATS, render
from .runner import Runner
from .sources import SitemapReader, collect_urls
from .state import StateStore, default_db_path

__all__ = ["EXIT_FAIL_ON", "EXIT_FAILED", "EXIT_OK", "EXIT_PAUSED", "EXIT_USAGE", "build_parser", "main"]

EXIT_OK = 0
EXIT_FAIL_ON = 1
EXIT_USAGE = 2
EXIT_PAUSED = 3
EXIT_FAILED = 4

FAIL_ON_CHOICES = ("never", "errors", "not-indexed", "newly-not-indexed", "canonical-changed", "regressions")


class CliError(Exception):
    pass


# -- helpers ------------------------------------------------------------------------------------------------------


def _credentials_path(args: argparse.Namespace) -> str | None:
    return args.credentials or os.environ.get("GSC_CREDENTIALS") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")


def _token_cache(args: argparse.Namespace) -> TokenCache:
    return TokenCache(Path(args.token_cache) / "tokens.json" if args.token_cache else None)


def make_credentials(args: argparse.Namespace, transport: Transport):  # type: ignore[no-untyped-def]
    token = os.environ.get("GSC_ACCESS_TOKEN")
    path = _credentials_path(args)
    if not path and token:
        return StaticTokenCredentials(token)
    if not path:
        raise CliError(
            "no credentials: pass --credentials FILE (service account key or OAuth client secrets), "
            "or set GSC_CREDENTIALS / GOOGLE_APPLICATION_CREDENTIALS / GSC_ACCESS_TOKEN"
        )
    return load_credentials(
        path, scopes=(SCOPE_READONLY,), transport=transport, cache=_token_cache(args), subject=args.subject
    )


def _property(value: str | None) -> Property:
    value = value or os.environ.get("GSC_SITE_URL")
    if not value:
        raise CliError("missing --site (for example https://www.example.com/ or sc-domain:example.com)")
    try:
        return Property.parse(value)
    except PropertyError as exc:
        raise CliError(str(exc)) from exc


def _write_output(text: str, output: str | None, stdout: TextIO) -> None:
    if output and output != "-":
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        stdout.write(text)


def _write_report_dir(
    directory: str, records: list[InspectionRecord], site: str, run_id: int, diff: RunDiff | None
) -> list[Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in ("csv", "json", "markdown", "html"):
        path = out / f"run-{run_id}.{EXTENSIONS[fmt]}"
        path.write_text(render(fmt, records, site, run_id, diff), encoding="utf-8")
        written.append(path)
    return written


def _previous_diff(store: StateStore, site: str, run_id: int, include_membership: bool = False) -> RunDiff | None:
    prev = store.latest_run(site, ("completed", "paused"), before=run_id)
    if prev is None:
        return None
    return diff_records(store.records(prev), store.records(run_id), prev, run_id, include_membership)


def _fail_on_triggered(choices: Sequence[str], records: list[InspectionRecord], diff: RunDiff | None) -> list[str]:
    hits: list[str] = []
    for choice in choices:
        if choice == "errors" and any(not r.ok for r in records):
            hits.append("inspection errors")
        elif choice == "not-indexed" and any(r.ok and not r.is_indexed for r in records):
            hits.append("URLs not indexed")
        elif choice in ("newly-not-indexed", "regressions") and diff and diff.by_kind("newly_not_indexed"):
            hits.append("URLs newly not indexed")
        elif choice in ("canonical-changed", "regressions") and diff and diff.by_kind("google_canonical_changed"):
            hits.append("Google canonical changed")
    return hits


def _parse_fail_on(value: str) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    for item in items:
        if item not in FAIL_ON_CHOICES:
            raise argparse.ArgumentTypeError(f"invalid --fail-on value {item!r}; choose from {FAIL_ON_CHOICES}")
    return [i for i in items if i != "never"]


# -- commands -----------------------------------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace, transport: Transport, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    prop = _property(args.site)
    read_stdin = not args.input and not args.sitemap and not args.urls and not stdin.isatty()
    files = list(args.input or [])
    urls, warnings = collect_urls(
        files=files,
        sitemaps=args.sitemap or [],
        urls=args.urls or [],
        stdin=stdin if (read_stdin or "-" in files) else None,
        reader=SitemapReader(transport=transport),
        limit=args.limit,
    )
    for w in warnings:
        print(f"warning: {w}", file=stderr)
    with StateStore(args.db) as store:
        resumable = None if args.new_run else store.resumable_run(prop.site_url)
        if not urls and resumable is None:
            raise CliError("no URLs given: use URL arguments, -i FILE, --sitemap URL or pipe URLs on stdin")
        if args.dry_run:
            inside = [u for u in urls if prop.contains(u)]
            for u in inside:
                stdout.write(u + "\n")
            print(f"{len(inside)} URL(s) inside {prop}, {len(urls) - len(inside)} outside", file=stderr)
            return EXIT_OK
        creds = make_credentials(args, transport)
        client = InspectionClient(creds, transport=transport, language_code=args.language, timeout=args.timeout)
        limiter = QuotaLimiter(prop.site_url, store, per_minute=args.per_minute, per_day=args.daily_limit)

        def progress(pos: int, total: int, record: InspectionRecord) -> None:
            if not args.quiet:
                state = record.error or record.coverage
                print(f"[{pos}/{total}] {record.url}  {state}", file=stderr)

        runner = Runner(client, store, prop, limiter, progress=progress)
        run_id, outside = runner.plan(urls, resume=not args.new_run, label=args.label)
        for u in outside:
            print(f"warning: skipped, not inside {prop}: {u}", file=stderr)
        info = store.get_run(run_id)
        if info and info.done and not args.quiet:
            print(f"Resuming run {run_id}: {info.done}/{info.total} already inspected", file=stderr)
        try:
            outcome = runner.run(run_id, max_requests=args.max_requests, retry_errors=args.retry_errors)
        except KeyboardInterrupt:
            store.set_status(run_id, "paused", "interrupted")
            print(f"\nInterrupted; resume with the same command (run {run_id}).", file=stderr)
            return EXIT_PAUSED
        records = store.records(run_id)
        diff = None if args.no_diff else _previous_diff(store, prop.site_url, run_id)
        text = render(args.format, records, prop.site_url, run_id, diff)
        _write_output(text, args.output, stdout)
        if args.report_dir:
            for path in _write_report_dir(args.report_dir, records, prop.site_url, run_id, diff):
                print(f"wrote {path}", file=stderr)
        if outcome.note:
            print(f"Run {run_id} {outcome.status}: {outcome.note}", file=stderr)
        if outcome.status == "failed":
            return EXIT_FAILED
        if outcome.status == "paused":
            print(
                f"{outcome.pending} URL(s) still pending; rerun the same command to resume "
                f"(daily budget resets {next_reset():%Y-%m-%d %H:%M %Z}).",
                file=stderr,
            )
            return EXIT_PAUSED
        hits = _fail_on_triggered(args.fail_on, records, diff)
        if hits:
            print("fail-on: " + ", ".join(hits), file=stderr)
            return EXIT_FAIL_ON
    return EXIT_OK


def cmd_report(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    with StateStore(args.db) as store:
        run_id = args.run
        if run_id is None:
            prop = _property(args.site)
            run_id = store.latest_run(prop.site_url)
            if run_id is None:
                raise CliError(f"no runs for {prop} in {args.db}")
        info = store.get_run(run_id)
        if info is None:
            raise CliError(f"no run {run_id} in {args.db}")
        records = store.records(run_id)
        if args.compare is not None:
            diff = diff_records(store.records(args.compare), records, args.compare, run_id)
        elif args.no_diff:
            diff = None
        else:
            diff = _previous_diff(store, info.site_url, run_id)
        _write_output(render(args.format, records, info.site_url, run_id, diff), args.output, stdout)
        if args.report_dir:
            for path in _write_report_dir(args.report_dir, records, info.site_url, run_id, diff):
                print(f"wrote {path}", file=stderr)
    return EXIT_OK


def cmd_diff(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    with StateStore(args.db) as store:
        new = args.new
        if new is None:
            prop = _property(args.site)
            new = store.latest_run(prop.site_url)
        if new is None:
            raise CliError("no run to compare")
        info = store.get_run(new)
        if info is None:
            raise CliError(f"no run {new}")
        old = args.old if args.old is not None else store.latest_run(info.site_url, ("completed", "paused"), new)
        if old is None:
            raise CliError(f"run {new} has no earlier run to compare with")
        diff = diff_records(store.records(old), store.records(new), old, new, args.include_membership)
        if args.format == "json":
            import json

            text = json.dumps(diff.to_dict(), indent=2) + "\n"
        elif args.format == "markdown":
            lines = [f"# Changes from run {old} to run {new}", ""]
            if not diff.changes:
                lines.append("No changes.")
            else:
                lines += ["| Change | URL | Before | After |", "| --- | --- | --- | --- |"]
                lines += [f"| {c.label} | {c.url} | {c.before or ''} | {c.after or ''} |" for c in diff.changes]
            text = "\n".join(lines) + "\n"
        else:
            lines = [f"Run {old} -> run {new}: {len(diff.changes)} change(s)"]
            lines += [f"  {c.label}: {c.url}  [{c.before or '-'} -> {c.after or '-'}]" for c in diff.changes]
            text = "\n".join(lines) + "\n"
        _write_output(text, args.output, stdout)
        hits = _fail_on_triggered(args.fail_on, [], diff)
        if hits:
            print("fail-on: " + ", ".join(hits), file=stderr)
            return EXIT_FAIL_ON
    return EXIT_OK


def cmd_runs(args: argparse.Namespace, stdout: TextIO) -> int:
    with StateStore(args.db) as store:
        site = Property.parse(args.site).site_url if args.site else None
        runs = store.list_runs(site, limit=args.limit)
        if not runs:
            stdout.write("No runs.\n")
            return EXIT_OK
        stdout.write(f"{'ID':>5}  {'STATUS':<10} {'DONE':>11}  {'ERR':>5}  {'STARTED':<20}  SITE\n")
        for r in runs:
            done = f"{r.done}/{r.total}"
            stdout.write(f"{r.id:>5}  {r.status:<10} {done:>11}  {r.errors:>5}  {r.started_at:<20}  {r.site_url}\n")
    return EXIT_OK


def cmd_quota(args: argparse.Namespace, stdout: TextIO) -> int:
    prop = _property(args.site)
    with StateStore(args.db) as store:
        limiter = QuotaLimiter(prop.site_url, store, per_day=args.daily_limit)
        used = limiter.used_today()
        stdout.write(
            f"{prop}: {used} of {limiter.per_day} inspections used today, {limiter.remaining_today()} left "
            f"(resets {next_reset():%Y-%m-%d %H:%M %Z})\n"
        )
    return EXIT_OK


def cmd_auth(args: argparse.Namespace, transport: Transport, stdout: TextIO, stderr: TextIO) -> int:
    creds = make_credentials(args, transport)
    if args.auth_command == "login":
        if not isinstance(creds, InstalledAppCredentials):
            stdout.write("Service account credentials need no interactive login.\n")
            creds.access_token()
            stdout.write("Token obtained.\n")
            return EXIT_OK
        creds.login(open_browser=not args.no_browser, printer=lambda s: print(s, file=stderr))
        stdout.write(f"Logged in; token cached in {creds.cache.path}\n")
        return EXIT_OK
    if args.auth_command == "logout":
        if isinstance(creds, InstalledAppCredentials):
            removed = creds.logout()
            stdout.write("Cached token removed.\n" if removed else "No cached token.\n")
        else:
            creds.invalidate()
            stdout.write("Cached token removed.\n")
        return EXIT_OK
    # status
    creds.access_token()
    stdout.write(f"OK: {type(creds).__name__} can obtain an access token.\n")
    return EXIT_OK


# -- parser -------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(default_db_path()), help="SQLite state file (default: %(default)s)")

    auth_opts = argparse.ArgumentParser(add_help=False)
    auth_opts.add_argument("--credentials", "-c", help="service account key or OAuth client secrets JSON")
    auth_opts.add_argument("--subject", help="user to impersonate (domain-wide delegation, service accounts only)")
    auth_opts.add_argument("--token-cache", help="directory for the token cache (default: ~/.cache/gsc-url-inspector)")

    fail_on = argparse.ArgumentParser(add_help=False)
    fail_on.add_argument(
        "--fail-on",
        type=_parse_fail_on,
        default=[],
        help="comma-separated: errors, not-indexed, newly-not-indexed, canonical-changed, regressions, never",
    )

    parser = argparse.ArgumentParser(
        prog="gsc-url-inspector",
        description="Bulk Google Search Console URL Inspection with quota handling, resumable runs and reports.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", parents=[common, auth_opts, fail_on], help="inspect URLs (resumes unfinished runs)")
    p.add_argument("urls", nargs="*", help="URLs to inspect")
    p.add_argument("--site", "-s", help="Search Console property: https://www.example.com/ or sc-domain:example.com")
    p.add_argument("--input", "-i", action="append", help="file with one URL per line or a CSV; '-' for stdin")
    p.add_argument("--sitemap", action="append", help="sitemap or sitemap index URL or path (repeatable)")
    p.add_argument("--limit", type=int, help="inspect at most this many URLs from the sources")
    p.add_argument("--new-run", action="store_true", help="start a new run instead of resuming an unfinished one")
    p.add_argument("--retry-errors", action="store_true", help="inspect URLs that failed in this run again")
    p.add_argument("--max-requests", type=int, help="stop after inspecting this many URLs in this invocation")
    p.add_argument("--daily-limit", type=int, default=SITE_QPD, help="per-property daily budget (default %(default)s)")
    p.add_argument("--per-minute", type=int, default=SITE_QPM, help="per-property rate (default %(default)s/min)")
    p.add_argument("--language", help="BCP-47 language for issue messages, e.g. en-US (API default)")
    p.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    p.add_argument("--label", help="free-text label stored with a new run")
    p.add_argument("--format", "-f", choices=sorted(FORMATS), default="text", help="report format for stdout/--output")
    p.add_argument("--output", "-o", help="write the report to a file instead of stdout")
    p.add_argument("--report-dir", help="also write CSV, JSON, Markdown and HTML reports to this directory")
    p.add_argument("--no-diff", action="store_true", help="do not compare with the previous run")
    p.add_argument("--dry-run", action="store_true", help="print the URLs that would be inspected and exit")
    p.add_argument("--quiet", "-q", action="store_true", help="no per-URL progress on stderr")

    p = sub.add_parser("report", parents=[common], help="render a report for a stored run")
    p.add_argument("--run", type=int, help="run id (default: latest run of --site)")
    p.add_argument("--site", "-s", help="property, used when --run is not given")
    p.add_argument("--compare", type=int, help="diff against this run instead of the previous one")
    p.add_argument("--no-diff", action="store_true")
    p.add_argument("--format", "-f", choices=sorted(FORMATS), default="markdown")
    p.add_argument("--output", "-o")
    p.add_argument("--report-dir", help="write CSV, JSON, Markdown and HTML reports to this directory")

    p = sub.add_parser("diff", parents=[common, fail_on], help="compare two runs")
    p.add_argument("--site", "-s", help="property, used when --new is not given")
    p.add_argument("--old", type=int, help="older run id (default: the run before --new)")
    p.add_argument("--new", type=int, help="newer run id (default: latest run of --site)")
    p.add_argument("--include-membership", action="store_true", help="also list URLs added or missing")
    p.add_argument("--format", "-f", choices=("text", "json", "markdown"), default="text")
    p.add_argument("--output", "-o")

    p = sub.add_parser("runs", parents=[common], help="list stored runs")
    p.add_argument("--site", "-s")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("quota", parents=[common], help="show today's per-property usage")
    p.add_argument("--site", "-s")
    p.add_argument("--daily-limit", type=int, default=SITE_QPD)

    p = sub.add_parser("auth", help="manage OAuth login and the token cache")
    auth_sub = p.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", parents=[auth_opts], help="run the OAuth installed-app flow")
    login.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    auth_sub.add_parser("status", parents=[auth_opts], help="check that a token can be obtained")
    auth_sub.add_parser("logout", parents=[auth_opts], help="remove the cached token")
    return parser


def main(
    argv: Sequence[str] | None = None,
    transport: Transport | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    transport = transport or UrllibTransport()
    try:
        if args.command == "inspect":
            return cmd_inspect(args, transport, stdin, stdout, stderr)
        if args.command == "report":
            return cmd_report(args, stdout, stderr)
        if args.command == "diff":
            return cmd_diff(args, stdout, stderr)
        if args.command == "runs":
            return cmd_runs(args, stdout)
        if args.command == "quota":
            return cmd_quota(args, stdout)
        if args.command == "auth":
            return cmd_auth(args, transport, stdout, stderr)
    except (CliError, AuthError, PropertyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=stderr)
        return EXIT_USAGE
    parser.error("unknown command")  # pragma: no cover
    return EXIT_USAGE  # pragma: no cover
