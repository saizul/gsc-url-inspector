"""Service account credentials: a signed JWT exchanged for an access token (RFC 7523 bearer grant).

The service account's e-mail address must be added as a user of the Search Console property, otherwise the API
answers 403 for every URL.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..http import Transport, UrllibTransport
from .base import GOOGLE_TOKEN_URI, SCOPE_READONLY, AuthError, parse_token_response, post_token_request
from .cache import CachedToken, TokenCache, cache_key

__all__ = ["JWT_GRANT_TYPE", "ServiceAccountCredentials", "b64url", "build_assertion"]

JWT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_LIFETIME = 3600  # Google accepts at most one hour.


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - cryptography is a declared dependency
        raise AuthError("the 'cryptography' package is required for service account auth") from exc
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise AuthError(f"service account private_key is not a valid PEM key: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError("service account private_key must be an RSA key")
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def build_assertion(
    client_email: str,
    private_key_pem: str,
    scopes: Sequence[str],
    token_uri: str = GOOGLE_TOKEN_URI,
    key_id: str | None = None,
    subject: str | None = None,
    now: float | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if key_id:
        header["kid"] = key_id
    claims: dict[str, Any] = {
        "iss": client_email,
        "scope": " ".join(scopes),
        "aud": token_uri,
        "iat": issued,
        "exp": issued + ASSERTION_LIFETIME,
    }
    if subject:
        claims["sub"] = subject
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    signature = _sign_rs256(private_key_pem, signing_input.encode("ascii"))
    return signing_input + "." + b64url(signature)


class ServiceAccountCredentials:
    def __init__(
        self,
        info: dict[str, Any],
        scopes: Sequence[str] = (SCOPE_READONLY,),
        transport: Transport | None = None,
        cache: TokenCache | None = None,
        subject: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if info.get("type") not in (None, "service_account"):
            raise AuthError(f"expected a service_account key file, got type {info.get('type')!r}")
        for key in ("client_email", "private_key"):
            if not info.get(key):
                raise AuthError(f"service account key file is missing '{key}'")
        self.client_email: str = info["client_email"]
        self._private_key: str = info["private_key"]
        self._key_id: str | None = info.get("private_key_id")
        self.token_uri: str = info.get("token_uri") or GOOGLE_TOKEN_URI
        self.scopes = tuple(scopes)
        self.subject = subject
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.clock = clock
        self._key = cache_key("sa:" + self.client_email + ":" + (subject or ""), self.scopes)
        self._token: CachedToken | None = None

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> ServiceAccountCredentials:
        try:
            info = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise AuthError(f"cannot read service account file {path}: {exc}") from exc
        except ValueError as exc:
            raise AuthError(f"service account file {path} is not valid JSON") from exc
        if not isinstance(info, dict):
            raise AuthError(f"service account file {path} is not a JSON object")
        return cls(info, **kwargs)

    def access_token(self) -> str:
        now = self.clock()
        if self._token and self._token.valid(now):
            return self._token.access_token
        if self.cache is not None:
            cached = self.cache.get(self._key)
            if cached and cached.valid(now):
                self._token = cached
                return cached.access_token
        assertion = build_assertion(
            self.client_email, self._private_key, self.scopes, self.token_uri, self._key_id, self.subject, now
        )
        payload = post_token_request(
            self.transport, self.token_uri, {"grant_type": JWT_GRANT_TYPE, "assertion": assertion}
        )
        token, expires_at = parse_token_response(payload, now)
        self._token = CachedToken(token, expires_at, scope=" ".join(self.scopes))
        if self.cache is not None:
            self.cache.put(self._key, self._token)
        return token

    def invalidate(self) -> None:
        self._token = None
        if self.cache is not None:
            self.cache.delete(self._key)
