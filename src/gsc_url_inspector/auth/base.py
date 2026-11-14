"""Shared pieces of the two credential types."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

from ..http import Response, Transport, TransportError, form_encode

__all__ = [
    "GOOGLE_TOKEN_URI",
    "SCOPE_READONLY",
    "SCOPE_FULL",
    "AuthError",
    "Credentials",
    "parse_token_response",
    "post_token_request",
]

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Either scope is accepted by urlInspection.index.inspect; read-only is enough and is the default.
SCOPE_READONLY = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_FULL = "https://www.googleapis.com/auth/webmasters"


class AuthError(Exception):
    """Credentials are missing, malformed, revoked or rejected by the token endpoint."""


class Credentials(Protocol):
    def access_token(self) -> str:
        """Return a valid bearer token, refreshing it when needed."""

    def invalidate(self) -> None:
        """Forget the current access token (called after a 401)."""


def post_token_request(
    transport: Transport, token_uri: str, fields: Mapping[str, str], timeout: float = 30.0
) -> dict[str, Any]:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    try:
        resp: Response = transport("POST", token_uri, headers, form_encode(fields), timeout)
    except TransportError as exc:
        raise AuthError(f"token endpoint unreachable: {exc}") from exc
    try:
        payload = resp.json()
    except TransportError:
        payload = None
    if not resp.ok:
        if isinstance(payload, dict) and payload.get("error"):
            desc = payload.get("error_description") or ""
            raise AuthError(f"token request rejected ({resp.status}): {payload['error']} {desc}".strip())
        raise AuthError(f"token request failed with HTTP {resp.status}")
    if not isinstance(payload, dict):
        raise AuthError("token endpoint returned an unexpected body")
    return payload


def parse_token_response(payload: Mapping[str, Any], now: float | None = None) -> tuple[str, float]:
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise AuthError("token response has no access_token")
    try:
        expires_in = float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600.0
    now = time.time() if now is None else now
    return token, now + expires_in
