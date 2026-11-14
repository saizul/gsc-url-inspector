"""Client for ``POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect``.

Request body: ``inspectionUrl`` (required), ``siteUrl`` (required) and ``languageCode`` (optional, default ``en-US``).
Response body: ``{"inspectionResult": UrlInspectionResult}``. The method reports the indexed version only; it cannot
test a live URL.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from .auth.base import AuthError, Credentials
from .http import Response, Transport, TransportError, UrllibTransport

__all__ = [
    "INSPECT_ENDPOINT",
    "ApiError",
    "InspectionClient",
    "PermissionDenied",
    "QuotaExceeded",
    "RetryPolicy",
    "parse_api_error",
]

INSPECT_ENDPOINT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
_QUOTA_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None, api_status: str | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.reason = reason
        self.api_status = api_status

    @property
    def retryable(self) -> bool:
        return self.status in (408, 500, 502, 503, 504)


class QuotaExceeded(ApiError):
    """The API refused the request because a per-minute or per-day quota is used up."""

    def __init__(self, *args: Any, retry_after: float | None = None, daily: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after
        self.daily = daily


class PermissionDenied(ApiError):
    """The credentials have no access to the property, or the URL is outside the property."""


def _retry_after(resp: Response) -> float | None:
    value = resp.header("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def parse_api_error(resp: Response) -> ApiError:
    """Turn a Google API error body (``{"error": {"code", "message", "status", "errors"/"details"}}``) into an error."""
    message = f"HTTP {resp.status}"
    reason: str | None = None
    api_status: str | None = None
    try:
        body = json.loads(resp.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or message)
        api_status = err.get("status") if isinstance(err.get("status"), str) else None
        for item in err.get("errors") or []:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
        if reason is None:
            for detail in err.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason"):
                    reason = str(detail["reason"])
                    break
    elif resp.body:
        message = resp.text()[:300]
    quota_hit = resp.status == 429 or api_status == "RESOURCE_EXHAUSTED" or (reason in _QUOTA_REASONS)
    if quota_hit:
        lowered = message.lower()
        daily = "per day" in lowered or "daily" in lowered or reason in ("dailyLimitExceeded", "quotaExceeded")
        return QuotaExceeded(resp.status, message, reason, api_status, retry_after=_retry_after(resp), daily=daily)
    if resp.status == 403:
        return PermissionDenied(resp.status, message, reason, api_status)
    return ApiError(resp.status, message, reason, api_status)


@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter for 5xx, timeouts and per-minute quota responses."""

    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 64.0

    def delay(self, attempt: int, hint: float | None = None) -> float:
        if hint is not None:
            return min(self.max_delay, hint)
        return random.uniform(0, min(self.max_delay, self.base_delay * (2**attempt)))


class InspectionClient:
    def __init__(
        self,
        credentials: Credentials,
        transport: Transport | None = None,
        endpoint: str = INSPECT_ENDPOINT,
        language_code: str | None = None,
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.credentials = credentials
        self.transport = transport or UrllibTransport()
        self.endpoint = endpoint
        self.language_code = language_code
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.sleep = sleep
        self.requests_sent = 0

    def build_body(self, inspection_url: str, site_url: str) -> dict[str, str]:
        body = {"inspectionUrl": inspection_url, "siteUrl": site_url}
        if self.language_code:
            body["languageCode"] = self.language_code
        return body

    def inspect(self, inspection_url: str, site_url: str) -> dict[str, Any]:
        """Inspect one URL and return the decoded response body. Raises ``ApiError`` subclasses on failure."""
        payload = json.dumps(self.build_body(inspection_url, site_url)).encode("utf-8")
        refreshed = False
        attempt = 0
        while True:
            headers = {
                "Authorization": "Bearer " + self.credentials.access_token(),
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            }
            try:
                self.requests_sent += 1
                resp = self.transport("POST", self.endpoint, headers, payload, self.timeout)
            except TransportError as exc:
                attempt += 1
                if attempt >= self.retry.max_attempts:
                    raise ApiError(0, f"network error: {exc}") from exc
                self.sleep(self.retry.delay(attempt))
                continue
            if resp.ok:
                data = resp.json()
                if not isinstance(data, dict):
                    raise ApiError(resp.status, "response body is not a JSON object")
                return data
            if resp.status == 401 and not refreshed:
                refreshed = True
                self.credentials.invalidate()
                try:
                    self.credentials.access_token()
                except AuthError as exc:
                    raise ApiError(401, f"authentication failed: {exc}") from exc
                continue
            error = parse_api_error(resp)
            attempt += 1
            if isinstance(error, QuotaExceeded):
                # A daily quota does not come back within a retry window; let the runner pause the run.
                if error.daily or attempt >= self.retry.max_attempts:
                    raise error
                self.sleep(self.retry.delay(attempt, error.retry_after))
                continue
            if error.retryable and attempt < self.retry.max_attempts:
                self.sleep(self.retry.delay(attempt))
                continue
            raise error
