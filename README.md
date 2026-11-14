# gsc-url-inspector

[![CI](https://github.com/saizul/gsc-url-inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/gsc-url-inspector/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Inspect thousands of URLs with the Google Search Console
[URL Inspection API](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect), stay inside its
quota, pick up where you left off the next day, and see what changed since the last run.

The Search Console UI inspects one URL at a time. The API can do 2,000 per property per day, but a bulk run needs
quota bookkeeping, retries, a place to store results and something readable at the end. `gsc-url-inspector` does that:
it reads URLs from files, sitemaps or stdin, sends one `urlInspection/index:inspect` request per URL, stores every
result in SQLite as it arrives, pauses cleanly when the daily budget is used up, and writes CSV, JSON, Markdown and HTML
reports grouped by coverage state, with a diff against the previous run.

```console
$ gsc-url-inspector inspect --site sc-domain:example.com --sitemap https://www.example.com/sitemap.xml -q
sc-domain:example.com: 1840 URL(s), 1512 indexed (82.3%), 321 not indexed, 7 error(s)

  Submitted and indexed                                      1498
  Crawled - currently not indexed                             174
  Duplicate, Google chose different canonical than user        61
  Discovered - currently not indexed                           48
  Excluded by 'noindex' tag                                    38
  Indexed, not submitted in sitemap                            14
  Inspection error                                              7

  61 URL(s) where Google chose a different canonical

Changes since run 4: newly not indexed 12, newly indexed 30, Google canonical changed 3
  Newly not indexed: https://www.example.com/guides/setup  [Submitted and indexed -> Crawled - currently not indexed]
  ...
```

(The numbers above are illustrative.)

## Contents

- [Features](#features)
- [Installation](#installation)
- [Docker image](#docker-image)
- [Authentication](#authentication)
- [Usage](#usage)
- [Quota and resumable runs](#quota-and-resumable-runs)
- [What is parsed](#what-is-parsed)
- [Reports](#reports)
- [Diffs](#diffs)
- [Exit codes](#exit-codes)
- [Configuration](#configuration)
- [Library use](#library-use)
- [Examples](#examples)
- [Testing](#testing)
- [Security notes](#security-notes)
- [Limitations](#limitations)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Two ways to authenticate**, both with a token cache:
  - **service account** (JWT bearer grant, RS256), optional domain-wide delegation with `--subject`
  - **OAuth installed-app flow** with a loopback redirect and PKCE; the refresh token is cached, so only the first
    login needs a browser
  - or a ready-made bearer token in `GSC_ACCESS_TOKEN` (for example from `gcloud auth print-access-token`)
- **URL sources**: arguments, text files (one URL per line, `#` comments), CSV/TSV files (the `url`, `address`, `page`
  or `loc` column, otherwise the first column), stdin, and XML sitemaps, local or remote, including sitemap indexes
  and `.gz` sitemaps. URLs are de-duplicated and URLs outside the property are skipped before any quota is spent.
- **Both property types**: URL-prefix (`https://www.example.com/`) and domain properties (`sc-domain:example.com`,
  which cover every subdomain and both schemes).
- **Quota handling** based on Google's published limits: 600 inspections per minute and 2,000 per day per property.
  The per-minute rate is enforced with a sliding window; the daily count is stored per property and per Pacific-time
  day in the state database, so separate runs on the same day share one budget. Per-minute `429`s are retried with
  `Retry-After` or exponential backoff; a per-day quota error pauses the run.
- **Resumable runs**: every result is committed to SQLite as it arrives. Rerun the same command and an unfinished
  run continues with the URLs that have no result yet. `--retry-errors` re-inspects failed URLs; `Ctrl-C` pauses.
- **The full inspection result**: index verdict, coverage state, robots.txt state, indexing state, page fetch state,
  last crawl time, crawled-as user agent, Google-selected and user-declared canonical, sitemaps, referring URLs, rich
  results (types, items, issues), AMP (URL, verdict, indexing state, issues) and the deprecated mobile usability section
  when the API still returns it. The raw `inspectionResult` is kept as well.
- **Diffs** between runs: newly not indexed, newly indexed, coverage changes, Google canonical changes, user canonical
  changes, indexing state and robots.txt changes, rich result type changes, and optionally URLs added or missing.
- **Reports** in text, CSV, JSON, Markdown and a self-contained HTML page, all grouped by coverage state with counts.
- **CI-friendly** exit codes and `--fail-on newly-not-indexed,canonical-changed,errors,not-indexed`.
- **Robust client**: retries for `5xx`, timeouts and network errors, one token refresh on `401`, a clear stop on `403`
  (no access to the property) so the budget is not burned on URLs that will all fail.
- One runtime dependency (`cryptography`, for signing service account JWTs). HTTP is plain `urllib`.

## Installation

Python 3.10 or newer.

```bash
pip install "gsc-url-inspector @ git+https://github.com/saizul/gsc-url-inspector"
# or from a checkout
pip install .
```

This installs the `gsc-url-inspector` command (also available as `python -m gsc_url_inspector`).

## Docker image

A multi-arch image (linux/amd64, linux/arm64) is published to GitHub Packages:

```bash
docker pull ghcr.io/saizul/gsc-url-inspector:latest

# Keep the state database and the token cache in named volumes between runs.
docker run --rm \
  -v gsc-data:/data -v gsc-cache:/cache \
  -v "$PWD/service-account.json:/work/sa.json:ro" \
  ghcr.io/saizul/gsc-url-inspector:latest \
  inspect --credentials sa.json --site sc-domain:example.com \
  --sitemap https://www.example.com/sitemap.xml --report-dir /data/reports -q
```

The working directory of the image is `/work`; mount your credentials and URL files there. The state database lives
in `/data` (`GSC_URL_INSPECTOR_DB=/data/gsc-inspections.sqlite`) and the token cache in `/cache`. Release images are
also tagged with the version (`ghcr.io/saizul/gsc-url-inspector:0.1.0`) and every build with its commit SHA. The
container runs as an unprivileged user (uid 10001). The OAuth browser login needs a loopback redirect to the machine
running the browser, so run `auth login` outside the container, or use a service account in Docker.

## Authentication

The API accepts the `https://www.googleapis.com/auth/webmasters.readonly` or `.../auth/webmasters` scope. This tool
asks for the read-only scope.

### Service account (servers, CI)

1. In Google Cloud, enable the **Google Search Console API** for a project and create a service account with a JSON
   key.
2. In Search Console, open **Settings > Users and permissions** for the property and add the service account's e-mail
   address (`...@<project>.iam.gserviceaccount.com`) as a user of the property.
3. Pass the key file:

```bash
gsc-url-inspector inspect --credentials service-account.json --site https://www.example.com/ ...
# or
export GSC_CREDENTIALS=service-account.json   # GOOGLE_APPLICATION_CREDENTIALS works too
```

### OAuth installed app (your own Google account)

1. Create an OAuth client of type **Desktop app** in the same project and download its JSON (it has an
   `"installed"` section; see `examples/oauth-client.example.json`).
2. Log in once:

```bash
gsc-url-inspector auth login --credentials client_secret.json
```

This opens the consent page, receives the redirect on `http://127.0.0.1:<random port>/`, checks the `state`
parameter, exchanges the code with the PKCE verifier and caches the tokens. Later runs refresh the access token
without a browser. `auth status` checks that a token can be obtained; `auth logout` removes it. Use `--no-browser` on
a machine where the URL should only be printed.

The file type is detected from its contents, so `--credentials` takes either kind.

## Usage

```bash
# A few URLs
gsc-url-inspector inspect -s https://www.example.com/ https://www.example.com/ https://www.example.com/pricing

# From files: text, CSV (url/address column) or '-' for stdin
gsc-url-inspector inspect -s sc-domain:example.com -i urls.txt -i crawl-export.csv
grep '/blog/' urls.txt | gsc-url-inspector inspect -s sc-domain:example.com

# From sitemaps (indexes and .gz are followed)
gsc-url-inspector inspect -s sc-domain:example.com --sitemap https://www.example.com/sitemap_index.xml

# Check what would be inspected, without credentials or quota
gsc-url-inspector inspect -s https://www.example.com/ --sitemap sitemap.xml --dry-run

# Write all four report files and a Markdown summary to stdout
gsc-url-inspector inspect -s sc-domain:example.com -i urls.txt --report-dir reports -f markdown -q

# Reports and diffs from stored runs
gsc-url-inspector runs
gsc-url-inspector report -s sc-domain:example.com -f html -o report.html
gsc-url-inspector report --run 7 --compare 5 -f markdown
gsc-url-inspector diff -s sc-domain:example.com --include-membership
gsc-url-inspector quota -s sc-domain:example.com
```

Main `inspect` options:

| Option | Meaning |
| --- | --- |
| `--site`, `-s` | the property, exactly as in Search Console: `https://www.example.com/` or `sc-domain:example.com` |
| `--input`, `-i` | URL file (repeatable); `-` reads stdin |
| `--sitemap` | sitemap or sitemap index, URL or local path (repeatable) |
| `--limit N` | only the first N URLs from the sources |
| `--new-run` | start a new run even if an unfinished one exists |
| `--retry-errors` | inspect URLs that failed in the current run again |
| `--max-requests N` | stop after N URLs in this invocation (the run stays resumable) |
| `--daily-limit N`, `--per-minute N` | budget for this property (defaults 2000 and 600) |
| `--language` | BCP-47 code for translated issue messages (the API default is `en-US`) |
| `--format`, `-o`, `--report-dir` | report on stdout or to a file, and the four report files to a directory |
| `--fail-on` | exit 1 on `errors`, `not-indexed`, `newly-not-indexed`, `canonical-changed` or `regressions` |
| `--dry-run` | list the URLs inside the property and exit |
| `--quiet`, `-q` | no per-URL progress lines on stderr |

## Quota and resumable runs

Google documents these limits for URL Inspection
([Usage limits](https://developers.google.com/webmaster-tools/limits)):

| Scope | Per day | Per minute |
| --- | ---: | ---: |
| Per site (property) | 2,000 | 600 |
| Per project | 10,000,000 | 15,000 |

A bulk run is bound by the per-property numbers. The limiter keeps both: requests are spaced to stay under 600 a
minute, and each inspection increments a counter for the property and the current day (midnight to midnight
Pacific time) in the state database. When the counter reaches the daily budget, the run is marked `paused` and the
command exits with code 3. If the API itself answers with a per-day quota error (because other tools use the same
property, for example), the counter is set to the budget and the run pauses the same way.

Running the same `inspect` command again resumes the latest unfinished run for that property: URLs already stored are
not inspected again, and URLs that are new in the sources are appended. On a cron schedule a 10,000-URL site is
therefore covered in five days without any extra logic (see `examples/daily-cron.sh`). Use `--new-run` to start
over, for example for a weekly comparison.

Lower `--daily-limit` to keep part of the budget for people using the Search Console UI or other tools.

## What is parsed

All fields of `UrlInspectionResult` from the API reference:

| Section | Fields |
| --- | --- |
| `indexStatusResult` | `verdict`, `coverageState`, `robotsTxtState`, `indexingState`, `lastCrawlTime`, `pageFetchState`, `googleCanonical`, `userCanonical`, `crawledAs`, `sitemap[]`, `referringUrls[]` |
| `richResultsResult` | `verdict`, `detectedItems[].richResultType`, `items[].name`, `items[].issues[]` (`issueMessage`, `severity`) |
| `ampResult` | `verdict`, `ampUrl`, `robotsTxtState`, `indexingState`, `ampIndexStatusVerdict`, `lastCrawlTime`, `pageFetchState`, `issues[]` |
| `mobileUsabilityResult` | `verdict`, `issues[]` (`issueType`, `message`). Marked deprecated by Google; parsed only when present. |
| top level | `inspectionResultLink` (shown as a link in the HTML report) |

A URL counts as **indexed** when the index verdict is `PASS`. A **canonical mismatch** is a URL whose
Google-selected canonical differs from the user-declared one (a trailing slash alone does not count). Unknown enum
values are passed through as strings, and the raw `inspectionResult` is stored and included in the JSON report.

Keep in mind that the method reports the version of the URL in Google's index; it cannot run a live test.

## Reports

| Format | Content |
| --- | --- |
| `text` | summary and counts per coverage state (default on stdout) |
| `csv` | one row per URL with every parsed field flattened (canonicals, states, rich result types and counts, AMP, mobile) |
| `json` | summary, diff and per-URL results including the raw `inspectionResult` |
| `markdown` | summary table, coverage table, changes since the previous run, canonical mismatches and URLs per coverage state |
| `html` | the same as a single self-contained page (no external assets, all values escaped), with collapsible groups |

`--report-dir DIR` writes `run-<id>.csv`, `.json`, `.md` and `.html` in one go.

## Diffs

After each run, and with `report` and `diff`, results are compared URL by URL with the previous run of the same
property:

| Change | When |
| --- | --- |
| `newly_not_indexed` | verdict was `PASS`, is not `PASS` any more |
| `newly_indexed` | the opposite |
| `coverage_changed` | the coverage state changed without a change of indexed status |
| `google_canonical_changed` | the Google-selected canonical changed (both runs have one) |
| `user_canonical_changed` | the declared canonical changed, appeared or disappeared |
| `indexing_state_changed`, `robots_changed` | indexing state (for example `BLOCKED_BY_META_TAG`) or robots.txt state changed |
| `rich_results_changed` | the set of detected rich result types changed |
| `new_url`, `removed_url` | with `--include-membership` only |

URLs that failed to inspect in either run are not compared, so a transient API error never shows up as a page that
dropped out of the index. `newly_not_indexed` and `google_canonical_changed` are the "regressions" for `--fail-on`.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | run completed (and no `--fail-on` condition matched) |
| 1 | a `--fail-on` condition matched |
| 2 | usage, configuration or authentication error |
| 3 | run paused: daily budget reached, `--max-requests` reached, interrupted, or too many consecutive errors; rerun to resume |
| 4 | run failed: the credentials have no access to the property |

## Configuration

| Variable | Purpose |
| --- | --- |
| `GSC_CREDENTIALS` | path of a service account key or OAuth client secrets file (`--credentials`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | used when `GSC_CREDENTIALS` is not set |
| `GSC_ACCESS_TOKEN` | a bearer token to use when no credentials file is configured |
| `GSC_SITE_URL` | default property for `--site` |
| `GSC_URL_INSPECTOR_DB` | state database (default `./gsc-inspections.sqlite`, `--db`) |
| `GSC_URL_INSPECTOR_CACHE` | token cache directory (default `$XDG_CACHE_HOME/gsc-url-inspector` or `~/.cache/gsc-url-inspector`) |

The state database holds four tables: `runs`, `run_urls` (the planned URL list of each run), `results` (key fields
plus the raw JSON) and `quota_usage`. It is plain SQLite, so you can query it directly:

```sql
SELECT coverage_state, COUNT(*) FROM results WHERE run_id = 3 GROUP BY 1 ORDER BY 2 DESC;
```

## Library use

```python
from gsc_url_inspector import (
    InspectionClient, Property, QuotaLimiter, Runner, StateStore, load_credentials, render,
)

site = Property.parse("sc-domain:example.com")
store = StateStore("gsc-inspections.sqlite")
client = InspectionClient(load_credentials("service-account.json"))
runner = Runner(client, store, site, QuotaLimiter(site.site_url, store))

run_id, outside = runner.plan(["https://www.example.com/", "https://shop.example.com/cart"])
outcome = runner.run(run_id)
print(outcome.status, outcome.inspected, outcome.pending)
print(render("markdown", store.records(run_id), site.site_url, run_id))
```

`InspectionClient.inspect(url, site_url)` returns the decoded response for a single URL;
`parse_inspection()` turns it into an `InspectionRecord`. Every network call goes through a `Transport` callable,
so you can plug in your own HTTP stack or a fake one in tests.

## Examples

`examples/` contains:

- `urls.txt`, `urls.csv`, `sitemap.xml`, `sitemap-index.xml`: input formats
- `inspect-response.json`: a complete API response with rich results, AMP and mobile usability
- `library_usage.py`: library use; runs offline against the sample response when no credentials are set
- `daily-cron.sh`: a cron job that keeps resuming a large run day after day
- `github-actions-gsc-url-inspector.yml`: a weekly workflow with the state file in the Actions cache and the Markdown
  report in the job summary
- `oauth-client.example.json`: the shape of a desktop OAuth client file (placeholder values)

## Testing

```console
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e ".[dev]"
$ pytest
$ ruff check . && ruff format --check .
```

The tests use a mocked HTTP transport for the token endpoint, the inspection endpoint and sitemap downloads, a
throwaway RSA key to check JWT signatures, an in-memory or temporary SQLite database, a fake clock for the quota
limiter, and a real loopback server for the OAuth redirect. They never contact Google. CI runs them on Python 3.10 to
3.13.

## Security notes

- Credentials are read from files you name; nothing is sent anywhere except Google's token endpoint
  (`oauth2.googleapis.com`) and the Search Console API.
- The token cache contains refresh tokens. It is written atomically with `0600` permissions. Delete it with
  `auth logout` or by removing the directory.
- The OAuth redirect listener binds to `127.0.0.1` only, uses a random port, checks `state` and uses PKCE (`S256`).
- Sitemaps with a DTD or entity declarations are rejected, and sitemap index nesting is limited to three levels.
- Never commit key files. The `.gitignore` excludes the usual names (`client_secret*.json`, `service-account*.json`,
  `credentials*.json`) and SQLite files.

## Limitations

- The API reports the indexed version only (no live test), and the coverage state strings are free text that Google
  may change or translate; grouping uses them as returned.
- The daily window is assumed to reset at midnight Pacific time, which is how Google API daily quotas usually work;
  the per-property counter only knows about inspections made by this tool. If the API reports the quota as used up
  earlier, the run pauses anyway.
- Mobile usability is deprecated by Google and may be missing from responses. It is reported only when present.
- Requests are sent one at a time. At 600 per minute the daily per-property limit is reached in a few minutes anyway,
  so parallel requests would not make a run finish sooner.
- The OAuth login needs a browser that can reach the loopback address of the machine running the command.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). Google and Search Console are trademarks of Google LLC; this project is not affiliated
with Google.
