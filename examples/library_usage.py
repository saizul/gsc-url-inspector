"""Use gsc-url-inspector as a library.

With GSC_CREDENTIALS pointing at a service account key (or OAuth client secrets after ``gsc-url-inspector auth
login``) and GSC_SITE_URL set, this inspects real URLs. Without them it runs offline against a canned response, so
it also works as a smoke test.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from gsc_url_inspector import (
    InspectionClient,
    Property,
    QuotaLimiter,
    Runner,
    StateStore,
    StaticTokenCredentials,
    diff_records,
    load_credentials,
    render,
)
from gsc_url_inspector.http import Response

HERE = Path(__file__).resolve().parent
URLS = ["https://www.example.com/", "https://www.example.com/blog/post", "https://www.example.com/pricing"]


def offline_transport(method, url, headers, body, timeout):
    """Answer every inspection with the example response from inspect-response.json."""
    payload = json.loads((HERE / "inspect-response.json").read_text())
    return Response(200, {"content-type": "application/json"}, json.dumps(payload).encode(), url)


def main() -> int:
    creds_file = os.environ.get("GSC_CREDENTIALS")
    site = Property.parse(os.environ.get("GSC_SITE_URL", "https://www.example.com/"))
    if creds_file:
        credentials = load_credentials(creds_file)
        transport = None  # the default urllib transport
    else:
        credentials = StaticTokenCredentials("offline-demo")
        transport = offline_transport

    store = StateStore(":memory:")
    client = InspectionClient(credentials, transport=transport)
    runner = Runner(client, store, site, QuotaLimiter(site.site_url, store, per_day=50))

    first, _ = runner.plan(URLS)
    print(runner.run(first))
    second, _ = runner.plan(URLS, resume=False)
    runner.run(second)

    diff = diff_records(store.records(first), store.records(second), first, second)
    sys.stdout.write(render("markdown", store.records(second), site.site_url, second, diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
