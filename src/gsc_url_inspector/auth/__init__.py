"""Authentication: service account (JWT bearer) or OAuth installed-app flow, with a shared token cache."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..http import Transport
from .base import GOOGLE_TOKEN_URI, SCOPE_FULL, SCOPE_READONLY, AuthError, Credentials
from .cache import CachedToken, TokenCache, cache_key, default_cache_dir
from .installed_app import ClientSecrets, InstalledAppCredentials, LoopbackReceiver
from .service_account import ServiceAccountCredentials

__all__ = [
    "GOOGLE_TOKEN_URI",
    "SCOPE_FULL",
    "SCOPE_READONLY",
    "AuthError",
    "CachedToken",
    "ClientSecrets",
    "Credentials",
    "InstalledAppCredentials",
    "LoopbackReceiver",
    "ServiceAccountCredentials",
    "StaticTokenCredentials",
    "TokenCache",
    "cache_key",
    "default_cache_dir",
    "detect_credentials_type",
    "load_credentials",
]


class StaticTokenCredentials:
    """A bearer token obtained elsewhere (for example ``gcloud auth print-access-token``). It is never refreshed."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise AuthError("empty access token")
        self._token = token.strip()

    def access_token(self) -> str:
        if not self._token:
            raise AuthError("the supplied access token was rejected and cannot be refreshed")
        return self._token

    def invalidate(self) -> None:
        self._token = ""


def detect_credentials_type(data: dict[str, Any]) -> str:
    if data.get("type") == "service_account" or ("client_email" in data and "private_key" in data):
        return "service_account"
    if "installed" in data or "web" in data:
        return "oauth"
    raise AuthError("unrecognised credentials file: expected a service account key or OAuth client secrets")


def load_credentials(
    path: str | Path,
    scopes: Sequence[str] = (SCOPE_READONLY,),
    transport: Transport | None = None,
    cache: TokenCache | None = None,
    subject: str | None = None,
) -> ServiceAccountCredentials | InstalledAppCredentials:
    """Build credentials from a JSON file, picking the flow from the file's shape."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise AuthError(f"cannot read credentials file {path}: {exc}") from exc
    except ValueError as exc:
        raise AuthError(f"credentials file {path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise AuthError(f"credentials file {path} is not a JSON object")
    kind = detect_credentials_type(data)
    cache = cache if cache is not None else TokenCache()
    if kind == "service_account":
        return ServiceAccountCredentials(data, scopes=scopes, transport=transport, cache=cache, subject=subject)
    return InstalledAppCredentials(ClientSecrets.from_dict(data), scopes=scopes, transport=transport, cache=cache)
