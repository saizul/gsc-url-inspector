"""Bulk Google Search Console URL Inspection API client and CLI."""

from ._version import __version__
from .auth import (
    AuthError,
    InstalledAppCredentials,
    ServiceAccountCredentials,
    StaticTokenCredentials,
    TokenCache,
    load_credentials,
)
from .client import INSPECT_ENDPOINT, ApiError, InspectionClient, PermissionDenied, QuotaExceeded
from .diff import RunDiff, diff_records
from .models import InspectionRecord, parse_inspection
from .property import Property, PropertyError
from .quota import DailyQuotaExhausted, QuotaLimiter
from .report import render, summarize
from .runner import Runner, RunOutcome
from .sources import SitemapReader, collect_urls
from .state import StateStore

__all__ = [
    "INSPECT_ENDPOINT",
    "ApiError",
    "AuthError",
    "DailyQuotaExhausted",
    "InspectionClient",
    "InspectionRecord",
    "InstalledAppCredentials",
    "PermissionDenied",
    "Property",
    "PropertyError",
    "QuotaExceeded",
    "QuotaLimiter",
    "RunDiff",
    "RunOutcome",
    "Runner",
    "ServiceAccountCredentials",
    "SitemapReader",
    "StateStore",
    "StaticTokenCredentials",
    "TokenCache",
    "__version__",
    "collect_urls",
    "diff_records",
    "load_credentials",
    "parse_inspection",
    "render",
    "summarize",
]
