# Contributing

Thanks for taking the time to improve gsc-url-inspector. Bug reports, fixes,
new input formats and documentation corrections are all welcome.

## Reporting a bug

Open an issue with:

- the command you ran and the output (redact URLs you do not want to share)
- your Python version (`python --version`) and operating system
- for a parsing problem: the relevant part of the `inspectionResult` from the
  JSON report (`--format json`), with host names replaced by `example.com`
- for a quota or resume problem: the output of `gsc-url-inspector runs` and
  `gsc-url-inspector quota --site ...`

Never paste key files, client secrets or tokens.

## Development setup

You need Python 3.10 or newer.

```console
$ git clone https://github.com/saizul/gsc-url-inspector.git
$ cd gsc-url-inspector
$ python -m venv .venv
$ . .venv/bin/activate
$ python -m pip install -e ".[dev]"
```

Before every commit:

```console
$ ruff check .
$ ruff format --check .
$ pytest
```

All three must pass; CI runs them on Python 3.10 to 3.13.

## Guidelines

- **Tests never touch the network.** Use `FakeTransport` and `InspectApi` from
  `tests/conftest.py` for the token endpoint, the inspection endpoint and
  sitemaps. Only the OAuth loopback test opens a socket, on 127.0.0.1.
- **Follow the API reference.** Field names, enum values and the endpoint come
  from developers.google.com/webmaster-tools; link the reference page in the
  pull request when you add or change a field.
- **Keep the quota safe.** A change to the runner or limiter needs a test that
  shows no URL is inspected twice and the daily budget is respected.
- **Fixture data stays fictional.** Use `example.com` hosts in tests and
  `examples/`.
- **Every change comes with a test** that fails without it.
- Add a line to the `Unreleased` section of `CHANGELOG.md` for anything a user
  would notice.

## Project layout

```text
src/gsc_url_inspector/
    auth/          service account JWT, OAuth installed-app flow, token cache
    http.py        injectable transport (urllib by default)
    client.py      urlInspection/index:inspect client, retries, error mapping
    models.py      typed UrlInspectionResult
    property.py    URL-prefix and domain properties
    sources.py     URL files, stdin, sitemaps
    quota.py       per-minute and per-day limiter
    state.py       SQLite state store
    runner.py      bulk runs, pause and resume
    diff.py        run-to-run comparison
    report/        text, CSV, JSON, Markdown and HTML renderers
    cli.py         command-line interface
tests/             pytest suite with a mocked HTTP transport
examples/          inputs, a sample response, cron and CI examples
```

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why, and how you tested it.
- Do not bump the version number; that happens at release time.

By contributing you agree that your work is released under the MIT License.
