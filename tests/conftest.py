from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gsc_url_inspector.client import INSPECT_ENDPOINT
from gsc_url_inspector.http import Response, TransportError

TOKEN_URI = "https://oauth2.googleapis.com/token"
SITE = "https://www.example.com/"


class FakeTransport:
    """Mocked HTTP transport. Routes are (METHOD, URL) -> Response, a queue of responses, or a callable."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[dict[str, Any]] = []

    def add(self, method: str, url: str, status: int = 200, body: Any = b"", headers: dict[str, str] | None = None):
        self.routes[(method.upper(), url)] = _response(status, body, headers, url)

    def queue(self, method: str, url: str, *responses: Response) -> None:
        self.routes[(method.upper(), url)] = deque(responses)

    def handler(self, method: str, url: str, fn: Callable[[dict[str, Any]], Response]) -> None:
        self.routes[(method.upper(), url)] = fn

    def error(self, method: str, url: str, message: str = "connection refused") -> None:
        self.routes[(method.upper(), url)] = TransportError(message)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        req = {"method": method, "url": url, "headers": dict(headers), "body": body}
        self.requests.append(req)
        route = self.routes.get((method.upper(), url))
        if route is None:
            return Response(404, {}, b'{"error": {"code": 404, "message": "not found"}}', url)
        if isinstance(route, TransportError):
            raise route
        if isinstance(route, deque):
            if not route:
                raise AssertionError(f"no more queued responses for {method} {url}")
            item = route.popleft()
            if isinstance(item, TransportError):
                raise item
            return item
        if callable(route):
            return route(req)
        return route

    def calls(self, method: str, url: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["url"] == url]


def _response(status: int, body: Any, headers: dict[str, str] | None = None, url: str = "") -> Response:
    if isinstance(body, (dict, list)):
        raw = json.dumps(body).encode()
    elif isinstance(body, str):
        raw = body.encode()
    else:
        raw = body
    return Response(status, {k.lower(): v for k, v in (headers or {}).items()}, raw, url)


def resp(status: int = 200, body: Any = b"", headers: dict[str, str] | None = None) -> Response:
    return _response(status, body, headers)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def sa_info(rsa_key: rsa.RSAPrivateKey) -> dict[str, str]:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        "type": "service_account",
        "project_id": "demo-project",
        "private_key_id": "kid-123",
        "private_key": pem,
        "client_email": "inspector@demo-project.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": TOKEN_URI,
    }


def token_ok(token: str = "ya29.test-token", expires_in: int = 3599, **extra: Any) -> Response:
    return resp(200, {"access_token": token, "expires_in": expires_in, "token_type": "Bearer", **extra})


def index_result(
    verdict: str = "PASS",
    coverage: str = "Submitted and indexed",
    google_canonical: str | None = None,
    user_canonical: str | None = None,
    indexing_state: str = "INDEXING_ALLOWED",
    fetch: str = "SUCCESSFUL",
    robots: str = "ALLOWED",
    rich_types: tuple[str, ...] = (),
    amp: dict[str, Any] | None = None,
    mobile: dict[str, Any] | None = None,
    url: str = "",
) -> dict[str, Any]:
    idx: dict[str, Any] = {
        "verdict": verdict,
        "coverageState": coverage,
        "robotsTxtState": robots,
        "indexingState": indexing_state,
        "lastCrawlTime": "2026-11-01T10:20:30Z",
        "pageFetchState": fetch,
        "crawledAs": "MOBILE",
        "sitemap": ["https://www.example.com/sitemap.xml"],
        "referringUrls": ["https://www.example.com/"],
    }
    if google_canonical:
        idx["googleCanonical"] = google_canonical
    if user_canonical:
        idx["userCanonical"] = user_canonical
    result: dict[str, Any] = {
        "inspectionResultLink": "https://search.google.com/search-console/inspect?resource_id=x&id=y",
        "indexStatusResult": idx,
    }
    if rich_types:
        result["richResultsResult"] = {
            "verdict": "PASS",
            "detectedItems": [
                {"richResultType": t, "items": [{"name": f"{t} item", "issues": []}]} for t in rich_types
            ],
        }
    if amp is not None:
        result["ampResult"] = amp
    if mobile is not None:
        result["mobileUsabilityResult"] = mobile
    return {"inspectionResult": result}


class FakeCredentials:
    def __init__(self, token: str = "tok") -> None:
        self.token = token
        self.invalidated = 0

    def access_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidated += 1


class InspectApi:
    """A fake inspect endpoint that answers per inspectionUrl from a dict."""

    def __init__(self, transport: FakeTransport, results: dict[str, Any]) -> None:
        self.results = results
        transport.handler("POST", INSPECT_ENDPOINT, self)

    def __call__(self, req: dict[str, Any]) -> Response:
        body = json.loads(req["body"])
        answer = self.results.get(body["inspectionUrl"])
        if isinstance(answer, Response):
            return answer
        if answer is None:
            return resp(200, index_result("NEUTRAL", "URL is unknown to Google"))
        return resp(200, answer)
