from __future__ import annotations

import json

import pytest

from conftest import SITE, FakeCredentials, index_result, resp
from gsc_url_inspector.client import (
    INSPECT_ENDPOINT,
    ApiError,
    InspectionClient,
    PermissionDenied,
    QuotaExceeded,
    RetryPolicy,
    parse_api_error,
)
from gsc_url_inspector.http import TransportError


def make_client(transport, **kw):
    sleeps: list[float] = []
    client = InspectionClient(FakeCredentials(), transport=transport, sleep=sleeps.append, **kw)
    return client, sleeps


def google_error(code: int, message: str, status: str, reason: str | None = None) -> dict:
    err: dict = {"code": code, "message": message, "status": status}
    if reason:
        err["errors"] = [{"message": message, "domain": "usageLimits", "reason": reason}]
    return {"error": err}


def test_endpoint_is_the_documented_one():
    assert INSPECT_ENDPOINT == "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


def test_request_shape(transport):
    transport.add("POST", INSPECT_ENDPOINT, 200, index_result())
    client, _ = make_client(transport, language_code="de-CH")
    data = client.inspect("https://www.example.com/a", SITE)
    assert data["inspectionResult"]["indexStatusResult"]["verdict"] == "PASS"
    (call,) = transport.requests
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["headers"]["Content-Type"].startswith("application/json")
    assert json.loads(call["body"]) == {
        "inspectionUrl": "https://www.example.com/a",
        "siteUrl": SITE,
        "languageCode": "de-CH",
    }


def test_language_code_omitted_by_default(transport):
    transport.add("POST", INSPECT_ENDPOINT, 200, index_result())
    client, _ = make_client(transport)
    client.inspect("https://www.example.com/", SITE)
    assert "languageCode" not in json.loads(transport.requests[0]["body"])


def test_retries_server_errors_then_succeeds(transport):
    transport.queue(
        "POST",
        INSPECT_ENDPOINT,
        resp(503, google_error(503, "Backend Error", "UNAVAILABLE")),
        resp(500, "oops"),
        resp(200, index_result()),
    )
    client, sleeps = make_client(transport)
    client.inspect("https://www.example.com/", SITE)
    assert len(sleeps) == 2
    assert client.requests_sent == 3


def test_gives_up_after_max_attempts(transport):
    transport.queue("POST", INSPECT_ENDPOINT, *[resp(500, "x")] * 3)
    client, _ = make_client(transport, retry=RetryPolicy(max_attempts=3))
    with pytest.raises(ApiError) as info:
        client.inspect("https://www.example.com/", SITE)
    assert info.value.status == 500


def test_network_errors_are_retried(transport):
    transport.queue("POST", INSPECT_ENDPOINT, TransportError("reset"), resp(200, index_result()))
    client, sleeps = make_client(transport)
    client.inspect("https://www.example.com/", SITE)
    assert len(sleeps) == 1


def test_network_errors_exhaust(transport):
    transport.error("POST", INSPECT_ENDPOINT)
    client, _ = make_client(transport, retry=RetryPolicy(max_attempts=2))
    with pytest.raises(ApiError, match="network error"):
        client.inspect("https://www.example.com/", SITE)


def test_per_minute_quota_backs_off_with_retry_after(transport):
    transport.queue(
        "POST",
        INSPECT_ENDPOINT,
        resp(
            429,
            google_error(429, "Quota exceeded for quota metric 'per minute'", "RESOURCE_EXHAUSTED"),
            {"Retry-After": "7"},
        ),
        resp(200, index_result()),
    )
    client, sleeps = make_client(transport)
    client.inspect("https://www.example.com/", SITE)
    assert sleeps == [7.0]


def test_daily_quota_raises_immediately(transport):
    transport.add(
        "POST",
        INSPECT_ENDPOINT,
        429,
        google_error(429, "Quota exceeded for quota metric 'Queries per day'", "RESOURCE_EXHAUSTED"),
    )
    client, sleeps = make_client(transport)
    with pytest.raises(QuotaExceeded) as info:
        client.inspect("https://www.example.com/", SITE)
    assert info.value.daily
    assert sleeps == []


def test_permission_denied(transport):
    transport.add(
        "POST",
        INSPECT_ENDPOINT,
        403,
        google_error(
            403, "You do not own this site, or the inspected URL is not part of this property.", "PERMISSION_DENIED"
        ),
    )
    client, _ = make_client(transport)
    with pytest.raises(PermissionDenied, match="do not own"):
        client.inspect("https://www.example.com/", SITE)


def test_bad_request_is_not_retried(transport):
    transport.add("POST", INSPECT_ENDPOINT, 400, google_error(400, "Invalid argument", "INVALID_ARGUMENT"))
    client, sleeps = make_client(transport)
    with pytest.raises(ApiError) as info:
        client.inspect("not-a-url", SITE)
    assert info.value.status == 400 and info.value.api_status == "INVALID_ARGUMENT"
    assert sleeps == []


def test_401_triggers_one_token_refresh(transport):
    transport.queue(
        "POST",
        INSPECT_ENDPOINT,
        resp(401, google_error(401, "Request had invalid authentication credentials.", "UNAUTHENTICATED")),
        resp(200, index_result()),
    )
    creds = FakeCredentials()
    client = InspectionClient(creds, transport=transport, sleep=lambda s: None)
    client.inspect("https://www.example.com/", SITE)
    assert creds.invalidated == 1


def test_repeated_401_fails(transport):
    transport.add("POST", INSPECT_ENDPOINT, 401, google_error(401, "bad creds", "UNAUTHENTICATED"))
    client, _ = make_client(transport)
    with pytest.raises(ApiError) as info:
        client.inspect("https://www.example.com/", SITE)
    assert info.value.status == 401


def test_non_object_body(transport):
    transport.add("POST", INSPECT_ENDPOINT, 200, [1, 2])
    client, _ = make_client(transport)
    with pytest.raises(ApiError, match="not a JSON object"):
        client.inspect("https://www.example.com/", SITE)


def test_parse_api_error_variants():
    e = parse_api_error(resp(403, google_error(403, "Rate", "PERMISSION_DENIED", "rateLimitExceeded")))
    assert isinstance(e, QuotaExceeded) and not e.daily
    e = parse_api_error(resp(403, google_error(403, "Daily", "PERMISSION_DENIED", "dailyLimitExceeded")))
    assert isinstance(e, QuotaExceeded) and e.daily
    e = parse_api_error(resp(502, "<html>bad gateway</html>"))
    assert type(e) is ApiError and "bad gateway" in e.message and e.retryable
    e = parse_api_error(
        resp(429, {"error": {"code": 429, "message": "m", "details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}})
    )
    assert isinstance(e, QuotaExceeded) and e.reason == "RATE_LIMIT_EXCEEDED"


def test_retry_after_http_date():
    e = parse_api_error(resp(429, "", {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    assert isinstance(e, QuotaExceeded) and e.retry_after == 0.0


def test_retry_policy_delay_bounds():
    policy = RetryPolicy(base_delay=1, max_delay=10)
    for attempt in range(1, 8):
        assert 0 <= policy.delay(attempt) <= 10
    assert policy.delay(1, hint=99) == 10
