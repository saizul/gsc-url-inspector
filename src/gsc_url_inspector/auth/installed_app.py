"""OAuth 2.0 installed-application flow with a loopback redirect and PKCE (RFC 8252, RFC 7636).

``login()`` opens the consent page, waits for Google to redirect the browser to ``http://127.0.0.1:<port>/`` and
exchanges the code for tokens. The refresh token is kept in the token cache, so later runs need no browser.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..http import Transport, UrllibTransport
from .base import GOOGLE_TOKEN_URI, SCOPE_READONLY, AuthError, parse_token_response, post_token_request
from .cache import CachedToken, TokenCache, cache_key

__all__ = [
    "GOOGLE_AUTH_URI",
    "ClientSecrets",
    "InstalledAppCredentials",
    "LoopbackReceiver",
    "build_authorization_url",
    "pkce_pair",
]

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"

_SUCCESS_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>gsc-url-inspector</title>"
    b"<p>Authorization complete. You can close this tab and return to the terminal.</p>"
)
_FAILURE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>gsc-url-inspector</title>"
    b"<p>Authorization failed. Check the terminal for details.</p>"
)


@dataclass(frozen=True)
class ClientSecrets:
    client_id: str
    client_secret: str
    auth_uri: str = GOOGLE_AUTH_URI
    token_uri: str = GOOGLE_TOKEN_URI

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientSecrets:
        section = data.get("installed") or data.get("web") or data
        if not isinstance(section, dict) or not section.get("client_id"):
            raise AuthError("client secrets must contain an 'installed' section with client_id")
        return cls(
            client_id=str(section["client_id"]),
            client_secret=str(section.get("client_secret", "")),
            auth_uri=str(section.get("auth_uri") or GOOGLE_AUTH_URI),
            token_uri=str(section.get("token_uri") or GOOGLE_TOKEN_URI),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ClientSecrets:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise AuthError(f"cannot read client secrets file {path}: {exc}") from exc
        except ValueError as exc:
            raise AuthError(f"client secrets file {path} is not valid JSON") from exc
        if not isinstance(data, dict):
            raise AuthError(f"client secrets file {path} is not a JSON object")
        return cls.from_dict(data)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return verifier, challenge.decode("ascii")


def build_authorization_url(
    client: ClientSecrets, redirect_uri: str, scopes: Sequence[str], state: str, code_challenge: str
) -> str:
    query = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return client.auth_uri + "?" + urllib.parse.urlencode(query)


class LoopbackReceiver:
    """A one-shot HTTP server on 127.0.0.1 that captures the ``code`` and ``state`` query parameters."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        receiver = self
        self.params: dict[str, str] | None = None
        self._done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = urllib.parse.urlparse(self.path).query
                params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                if "code" not in params and "error" not in params:
                    self.send_response(404)
                    self.end_headers()
                    return
                receiver.params = params
                ok = "code" in params
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_SUCCESS_PAGE if ok else _FAILURE_PAGE)
                receiver._done.set()

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = http.server.HTTPServer((host, port), Handler)
        self._server.timeout = 0.5

    @property
    def redirect_uri(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def wait(self, timeout: float = 300.0) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        try:
            while not self._done.is_set():
                if time.monotonic() > deadline:
                    raise AuthError("timed out waiting for the OAuth redirect")
                self._server.handle_request()
        finally:
            self._server.server_close()
        assert self.params is not None
        return self.params

    def close(self) -> None:
        self._server.server_close()


class InstalledAppCredentials:
    def __init__(
        self,
        client: ClientSecrets,
        scopes: Sequence[str] = (SCOPE_READONLY,),
        transport: Transport | None = None,
        cache: TokenCache | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.scopes = tuple(scopes)
        self.transport = transport or UrllibTransport()
        self.cache = cache if cache is not None else TokenCache()
        self.clock = clock
        self.key = cache_key("oauth:" + client.client_id, self.scopes)
        self._token: CachedToken | None = None

    # -- interactive login -------------------------------------------------------------------------------------

    def login(
        self,
        open_browser: bool = True,
        timeout: float = 300.0,
        printer: Callable[[str], None] = print,
        receiver: LoopbackReceiver | None = None,
    ) -> CachedToken:
        receiver = receiver or LoopbackReceiver()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        url = build_authorization_url(self.client, receiver.redirect_uri, self.scopes, state, challenge)
        printer("Open this URL in a browser to authorize read access to Search Console:\n\n  " + url + "\n")
        if open_browser:
            with contextlib.suppress(webbrowser.Error):
                webbrowser.open(url, new=1, autoraise=True)
        params = receiver.wait(timeout)
        if params.get("state") != state:
            raise AuthError("OAuth state mismatch; the redirect did not come from this login attempt")
        if "error" in params:
            raise AuthError(f"authorization denied: {params['error']}")
        return self.exchange_code(params["code"], receiver.redirect_uri, verifier)

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> CachedToken:
        fields = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client.client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        if self.client.client_secret:
            fields["client_secret"] = self.client.client_secret
        payload = post_token_request(self.transport, self.client.token_uri, fields)
        token, expires_at = parse_token_response(payload, self.clock())
        refresh = payload.get("refresh_token")
        if not refresh:
            previous = self.cache.get(self.key)
            refresh = previous.refresh_token if previous else None
        self._token = CachedToken(token, expires_at, refresh, scope=str(payload.get("scope", "")))
        self.cache.put(self.key, self._token)
        return self._token

    # -- non-interactive use -----------------------------------------------------------------------------------

    def has_refresh_token(self) -> bool:
        cached = self._token or self.cache.get(self.key)
        return bool(cached and cached.refresh_token)

    def access_token(self) -> str:
        now = self.clock()
        if self._token and self._token.valid(now):
            return self._token.access_token
        cached = self.cache.get(self.key)
        if cached and cached.valid(now):
            self._token = cached
            return cached.access_token
        refresh = (self._token and self._token.refresh_token) or (cached and cached.refresh_token)
        if not refresh:
            raise AuthError("no cached OAuth token; run 'gsc-url-inspector auth login' first")
        fields = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": self.client.client_id}
        if self.client.client_secret:
            fields["client_secret"] = self.client.client_secret
        payload = post_token_request(self.transport, self.client.token_uri, fields)
        token, expires_at = parse_token_response(payload, now)
        # Google may rotate the refresh token; keep the new one when it does.
        new_refresh = payload.get("refresh_token") or refresh
        self._token = CachedToken(token, expires_at, new_refresh, scope=str(payload.get("scope", "")))
        self.cache.put(self.key, self._token)
        return token

    def invalidate(self) -> None:
        if self._token:
            self._token = CachedToken("", 0, self._token.refresh_token, scope=self._token.scope)
            self.cache.put(self.key, self._token)

    def logout(self) -> bool:
        self._token = None
        return self.cache.delete(self.key)
