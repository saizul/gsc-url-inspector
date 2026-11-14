"""Minimal HTTP layer with an injectable transport.

Everything that talks to the network (token endpoints, the URL Inspection API, sitemap downloads) goes through a
``Transport`` callable, so tests can swap in a fake one and the package has no HTTP dependency.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ._version import __version__

__all__ = [
    "USER_AGENT",
    "Response",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "decode_body",
    "form_encode",
]

USER_AGENT = f"gsc-url-inspector/{__version__} (+https://github.com/saizul/gsc-url-inspector)"
DEFAULT_MAX_BYTES = 50 * 1024 * 1024


class TransportError(Exception):
    """Network failure, timeout or a response larger than allowed."""


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"invalid JSON from {self.url or 'server'}: {exc}") from exc


# A transport takes (method, url, headers, body, timeout) and returns a Response for every HTTP status
# (including 4xx/5xx). It raises TransportError for network problems.
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], Response]


def decode_body(body: bytes, content_encoding: str | None) -> bytes:
    enc = (content_encoding or "").strip().lower()
    if enc == "gzip" or (not enc and body[:2] == b"\x1f\x8b"):
        try:
            return gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise TransportError(f"invalid gzip body: {exc}") from exc
    if enc == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def form_encode(fields: Mapping[str, str]) -> bytes:
    return urllib.parse.urlencode(dict(fields)).encode("ascii")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibTransport:
    """Transport built on ``urllib``. Redirects are followed only when ``follow_redirects`` is true."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES, follow_redirects: bool = True) -> None:
        self.max_bytes = max_bytes
        handlers: list[urllib.request.BaseHandler] = []
        if not follow_redirects:
            handlers.append(_NoRedirect())
        self._opener = urllib.request.build_opener(*handlers)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Response:
        if not url.lower().startswith(("https://", "http://")):
            raise TransportError(f"unsupported URL scheme: {url}")
        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **dict(headers)}
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read(self.max_bytes + 1)
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                final_url = resp.geturl()
        except urllib.error.HTTPError as exc:
            raw = exc.read(self.max_bytes + 1) if exc.fp else b""
            status = exc.code
            resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            final_url = url
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"{method} {url}: {reason}") from exc
        if len(raw) > self.max_bytes:
            raise TransportError(f"{url}: response larger than {self.max_bytes} bytes")
        return Response(status, resp_headers, decode_body(raw, resp_headers.get("content-encoding")), final_url)
