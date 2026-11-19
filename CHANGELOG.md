# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `inspect --include REGEX` and `--exclude REGEX` (both repeatable) to inspect one section of a site, such as
  `/blog/` without tag archives, from a full sitemap. Patterns are searched anywhere in the URL, are applied after
  de-duplication and before `--limit`, and the number of skipped URLs is reported. An invalid pattern is a usage
  error (exit 2). Library: `filter_urls()` and the `include`/`exclude` arguments of `collect_urls()`.
- A code of conduct, issue forms and a pull request template.
- `inspect --reuse-within AGE` (`30m`, `12h`, `7d`, `2w`): pending URLs whose last successful inspection in any
  stored run of the property is younger than `AGE` get that result copied into the run instead of a new inspection,
  so weekly full runs do not spend the daily budget on URLs checked a day ago. `--dry-run` reports how many results
  would be reused.
- `inspect --order source|stale-first|not-indexed-first` to inspect never-inspected, long-unchecked or problem URLs
  first when the budget runs out before the run completes. Reports keep the source order.
- `StateStore.url_history()`, `StateStore.carry_result()` and the `planning` module (`parse_age`, `split_fresh`,
  `order_urls`, `UrlHistory`) for library use; `RunOutcome.reused`.

### Changed

- State database schema version 2: `results.carried_from` marks reused rows, plus an index on `results.url`. Version 1
  databases are migrated in place when opened; 0.1.0 refuses to open a migrated database.

## [0.1.0] - 2026-11-14

### Added

- `inspect` command: bulk URL Inspection API (`urlInspection/index:inspect`) runs for URL-prefix and domain
  properties, with URLs from arguments, text and CSV files, stdin and XML sitemaps (indexes and `.gz`).
- Authentication with a service account (JWT bearer grant, optional `--subject`), the OAuth installed-app flow with a
  loopback redirect and PKCE, or a bearer token in `GSC_ACCESS_TOKEN`; tokens are cached with `0600` permissions.
- Quota handling for the documented per-property limits (2,000 per day, 600 per minute), with a persistent daily
  counter per property and Pacific-time day, `Retry-After` and backoff for per-minute `429`s, and a clean pause on
  per-day quota errors.
- SQLite state store with resumable runs, `--retry-errors`, `--max-requests` and `--new-run`.
- Parsing of the full `UrlInspectionResult`: index status, canonicals, crawl data, rich results, AMP and the
  deprecated mobile usability section.
- Diffs between runs (newly not indexed, newly indexed, coverage, canonical, indexing state, robots.txt and rich
  result changes; optional added and missing URLs) with `--fail-on`.
- Reports in text, CSV, JSON, Markdown and self-contained HTML, grouped by coverage state with counts, plus
  `--report-dir` for all four files.
- `report`, `diff`, `runs`, `quota` and `auth login|status|logout` commands.
- Docker image published to `ghcr.io/saizul/gsc-url-inspector`.

[Unreleased]: https://github.com/saizul/gsc-url-inspector/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/gsc-url-inspector/releases/tag/v0.1.0
