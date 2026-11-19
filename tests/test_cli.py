from __future__ import annotations

import io
import json

import pytest

from conftest import SITE, TOKEN_URI, InspectApi, index_result, resp, token_ok
from gsc_url_inspector import __version__
from gsc_url_inspector.cli import EXIT_FAIL_ON, EXIT_FAILED, EXIT_OK, EXIT_PAUSED, EXIT_USAGE, main
from gsc_url_inspector.client import INSPECT_ENDPOINT

URLS = ["https://www.example.com/", "https://www.example.com/a", "https://www.example.com/b"]


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("GSC_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS", "GSC_SITE_URL", "GSC_URL_INSPECTOR_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GSC_ACCESS_TOKEN", "static-token")
    monkeypatch.setenv("GSC_URL_INSPECTOR_CACHE", str(tmp_path / "cache"))


def cli(transport, *args: str, stdin: str = "") -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), transport=transport, stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return Run(code, out.getvalue(), err.getvalue())


def test_version(transport, capsys):
    assert main(["--version"], transport=transport) == 0
    assert __version__ in capsys.readouterr().out


def test_inspect_text_report_and_reports_dir(transport, tmp_path):
    InspectApi(transport, {u: index_result() for u in URLS})
    db = str(tmp_path / "state.sqlite")
    r = cli(transport, "inspect", "--site", SITE, "--db", db, "--report-dir", str(tmp_path / "out"), *URLS)
    assert r.code == EXIT_OK, r.err
    assert "3 URL(s), 3 indexed (100.0%)" in r.out
    assert "[1/3] https://www.example.com/  Submitted and indexed" in r.err
    for ext in ("csv", "json", "md", "html"):
        assert (tmp_path / "out" / f"run-1.{ext}").exists()
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer static-token"


def test_inspect_reads_stdin_and_skips_outside_urls(transport, tmp_path):
    InspectApi(transport, {})
    r = cli(
        transport,
        "inspect",
        "-s",
        SITE,
        "--db",
        str(tmp_path / "s.sqlite"),
        "-q",
        "-f",
        "json",
        stdin="https://www.example.com/x\nhttps://elsewhere.example/y\n",
    )
    assert r.code == EXIT_OK
    doc = json.loads(r.out)
    assert doc["summary"]["total"] == 1
    assert "not inside" in r.err


def test_inspect_from_sitemap_and_dry_run(transport, tmp_path):
    transport.add(
        "GET",
        "https://www.example.com/sitemap.xml",
        200,
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.example.com/s</loc></url>'
        "<url><loc>https://cdn.example.net/z</loc></url></urlset>",
    )
    r = cli(
        transport,
        "inspect",
        "-s",
        SITE,
        "--db",
        str(tmp_path / "s.sqlite"),
        "--sitemap",
        "https://www.example.com/sitemap.xml",
        "--dry-run",
    )
    assert r.code == EXIT_OK
    assert r.out == "https://www.example.com/s\n"
    assert "1 URL(s) inside" in r.err
    assert not transport.calls("POST", INSPECT_ENDPOINT)


def test_inspect_include_exclude_dry_run(transport, tmp_path):
    urls = "https://www.example.com/blog/a\nhttps://www.example.com/blog/tag/x\nhttps://www.example.com/shop\n"
    r = cli(
        transport,
        "inspect",
        "-s",
        SITE,
        "--db",
        str(tmp_path / "f.sqlite"),
        "--include",
        "/blog/",
        "--exclude",
        "/tag/",
        "--dry-run",
        stdin=urls,
    )
    assert r.code == EXIT_OK
    assert r.out == "https://www.example.com/blog/a\n"
    assert "2 URL(s) skipped by --include/--exclude patterns" in r.err
    bad = cli(transport, "inspect", "-s", SITE, "--db", str(tmp_path / "f.sqlite"), "--include", "(", "--dry-run")
    assert bad.code == EXIT_USAGE


def test_pause_resume_and_diff_with_fail_on(transport, tmp_path):
    db = str(tmp_path / "s.sqlite")
    api = InspectApi(transport, {u: index_result() for u in URLS})
    r = cli(transport, "inspect", "-s", SITE, "--db", db, "-q", "--daily-limit", "2", *URLS)
    assert r.code == EXIT_PAUSED
    assert "1 URL(s) still pending" in r.err
    r = cli(transport, "inspect", "-s", SITE, "--db", db, "-q", "--daily-limit", "10")
    assert r.code == EXIT_OK, r.err
    assert "Resuming run 1" not in r.err  # quiet
    assert len(transport.calls("POST", INSPECT_ENDPOINT)) == 3

    # Second run: one page dropped out of the index, another got a new Google canonical.
    api.results["https://www.example.com/a"] = index_result("NEUTRAL", "Crawled - currently not indexed")
    api.results["https://www.example.com/b"] = index_result(
        google_canonical="https://www.example.com/b2", user_canonical="https://www.example.com/b"
    )
    api.results["https://www.example.com/"] = index_result()
    cli(transport, "report", "--db", db, "--run", "1")  # no-op render
    # Give run 1 a canonical so the change is detectable.
    r = cli(
        transport,
        "inspect",
        "-s",
        SITE,
        "--db",
        db,
        "-q",
        "--new-run",
        "--daily-limit",
        "100",
        "--fail-on",
        "regressions",
        *URLS,
    )
    assert r.code == EXIT_FAIL_ON
    assert "newly not indexed 1" in r.out
    assert "fail-on: URLs newly not indexed" in r.err

    r = cli(transport, "diff", "--db", db, "-s", SITE, "-f", "json")
    assert r.code == EXIT_OK
    kinds = [c["kind"] for c in json.loads(r.out)["changes"]]
    assert "newly_not_indexed" in kinds and "user_canonical_changed" in kinds

    r = cli(transport, "diff", "--db", db, "--old", "1", "--new", "2", "--fail-on", "newly-not-indexed")
    assert r.code == EXIT_FAIL_ON and "Newly not indexed" in r.out
    r = cli(transport, "diff", "--db", db, "--new", "2", "-f", "markdown")
    assert r.out.startswith("# Changes from run 1 to run 2")

    r = cli(transport, "runs", "--db", db)
    assert "completed" in r.out and r.out.count(SITE) == 2

    out = tmp_path / "r.html"
    r = cli(transport, "report", "--db", db, "-s", SITE, "-f", "html", "-o", str(out))
    assert r.code == EXIT_OK and "Changes since run 1" in out.read_text()
    r = cli(transport, "report", "--db", db, "--run", "2", "--compare", "1", "-f", "markdown")
    assert "Newly not indexed" in r.out

    r = cli(transport, "quota", "--db", db, "-s", SITE)
    assert "6 of 2000 inspections used today" in r.out


def test_permission_denied_exit_code(transport, tmp_path):
    transport.add(
        "POST",
        INSPECT_ENDPOINT,
        403,
        {"error": {"code": 403, "message": "You do not own this site", "status": "PERMISSION_DENIED"}},
    )
    r = cli(transport, "inspect", "-s", SITE, "--db", str(tmp_path / "s.sqlite"), "-q", URLS[0])
    assert r.code == EXIT_FAILED
    assert "permission denied" in r.err


def test_fail_on_errors_and_not_indexed(transport, tmp_path):
    InspectApi(
        transport, {URLS[0]: resp(400, "bad"), URLS[1]: index_result("NEUTRAL", "Discovered - currently not indexed")}
    )
    db = str(tmp_path / "s.sqlite")
    r = cli(transport, "inspect", "-s", SITE, "--db", db, "-q", "--fail-on", "errors", URLS[0])
    assert r.code == EXIT_FAIL_ON
    r = cli(transport, "inspect", "-s", SITE, "--db", db, "-q", "--new-run", "--fail-on", "not-indexed", URLS[1])
    assert r.code == EXIT_FAIL_ON


def test_usage_errors(transport, tmp_path, monkeypatch):
    db = str(tmp_path / "s.sqlite")
    assert cli(transport, "inspect", "--db", db, URLS[0]).code == EXIT_USAGE  # no --site
    r = cli(transport, "inspect", "-s", "example.com", "--db", db, URLS[0])
    assert r.code == EXIT_USAGE and "URL-prefix" in r.err
    r = cli(transport, "inspect", "-s", SITE, "--db", db)
    assert r.code == EXIT_USAGE and "no URLs" in r.err
    monkeypatch.delenv("GSC_ACCESS_TOKEN")
    r = cli(transport, "inspect", "-s", SITE, "--db", db, URLS[0])
    assert r.code == EXIT_USAGE and "no credentials" in r.err
    assert cli(transport, "inspect", "-s", SITE, "--fail-on", "bogus", URLS[0]).code == 2
    assert cli(transport, "report", "--db", db, "--run", "42").code == EXIT_USAGE
    assert cli(transport, "diff", "--db", db, "-s", SITE).code == EXIT_USAGE
    assert cli(transport, "runs", "--db", db).out == "No runs.\n"


def test_service_account_file_via_env(transport, tmp_path, monkeypatch, sa_info):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(sa_info))
    monkeypatch.delenv("GSC_ACCESS_TOKEN")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    transport.queue("POST", TOKEN_URI, token_ok("sa-token"))
    InspectApi(transport, {})
    r = cli(transport, "inspect", "-s", "sc-domain:example.com", "--db", str(tmp_path / "s.sqlite"), "-q", URLS[0])
    assert r.code == EXIT_OK, r.err
    assert transport.calls("POST", INSPECT_ENDPOINT)[0]["headers"]["Authorization"] == "Bearer sa-token"
    body = json.loads(transport.calls("POST", INSPECT_ENDPOINT)[0]["body"])
    assert body["siteUrl"] == "sc-domain:example.com"
    r = cli(transport, "auth", "status")
    assert r.code == EXIT_OK and "ServiceAccountCredentials" in r.out
    r = cli(transport, "auth", "login")
    assert "need no interactive login" in r.out
    assert cli(transport, "auth", "logout").code == EXIT_OK


def test_auth_status_oauth_without_login(transport, tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}))
    monkeypatch.delenv("GSC_ACCESS_TOKEN")
    r = cli(transport, "auth", "status", "--credentials", str(path), "--token-cache", str(tmp_path / "c"))
    assert r.code == EXIT_USAGE and "auth login" in r.err
    r = cli(transport, "auth", "logout", "--credentials", str(path), "--token-cache", str(tmp_path / "c"))
    assert r.out == "No cached token.\n"


def test_main_module_entry():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "gsc_url_inspector", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and __version__ in out.stdout
