"""On-disk token cache.

Tokens are stored in one JSON file, keyed by a hash of the client identity and the scopes. The file is written
atomically and with ``0600`` permissions because it holds refresh tokens.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = ["CachedToken", "TokenCache", "cache_key", "default_cache_dir"]

# Refresh a little before the real expiry so a token never expires in the middle of a request.
EXPIRY_SKEW_SECONDS = 60


def default_cache_dir() -> Path:
    env = os.environ.get("GSC_URL_INSPECTOR_CACHE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "gsc-url-inspector"


def cache_key(identity: str, scopes: Iterable[str]) -> str:
    material = identity + "\n" + " ".join(sorted(scopes))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


@dataclass
class CachedToken:
    access_token: str
    expires_at: float
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scope: str = ""

    def valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.access_token) and now < self.expires_at - EXPIRY_SKEW_SECONDS


class TokenCache:
    """A JSON file of ``{key: CachedToken}``. A missing or corrupt file is treated as empty."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_cache_dir() / "tokens.json"

    def _load_all(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> CachedToken | None:
        entry = self._load_all().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            return CachedToken(
                access_token=str(entry.get("access_token", "")),
                expires_at=float(entry.get("expires_at", 0)),
                refresh_token=entry.get("refresh_token") or None,
                token_type=str(entry.get("token_type", "Bearer")),
                scope=str(entry.get("scope", "")),
            )
        except (TypeError, ValueError):
            return None

    def put(self, key: str, token: CachedToken) -> None:
        data = self._load_all()
        data[key] = asdict(token)
        self._write(data)

    def delete(self, key: str) -> bool:
        data = self._load_all()
        if key not in data:
            return False
        del data[key]
        self._write(data)
        return True

    def clear(self) -> None:
        self._write({})

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
