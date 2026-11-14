"""Search Console property handling.

Two property types exist: URL-prefix properties (``https://www.example.com/``, the trailing slash is required by the
API) and domain properties (``sc-domain:example.com``, covering every subdomain and both schemes). The API rejects an
``inspectionUrl`` that is not under ``siteUrl``, so URLs are checked locally before any quota is spent.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

__all__ = ["Property", "PropertyError", "normalize_url"]


class PropertyError(ValueError):
    """The property string is not a valid Search Console property."""


def _host(netloc: str) -> str:
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):
        return host.split("]", 1)[0].lower() + "]"
    return host.split(":", 1)[0].lower().rstrip(".")


def normalize_url(url: str) -> str:
    """Lower-case scheme and host, drop the fragment and default ports; keep path and query as given."""
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if (scheme == "https" and netloc.endswith(":443")) or (scheme == "http" and netloc.endswith(":80")):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


@dataclass(frozen=True)
class Property:
    site_url: str

    @classmethod
    def parse(cls, value: str) -> Property:
        value = value.strip()
        if not value:
            raise PropertyError("empty property")
        if value.lower().startswith("sc-domain:"):
            domain = value.split(":", 1)[1].strip().lower().rstrip(".")
            if not domain or "/" in domain or "." not in domain:
                raise PropertyError(f"invalid domain property: {value!r}")
            return cls("sc-domain:" + domain)
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise PropertyError(f"a URL-prefix property needs http(s)://host/: {value!r}")
        if parts.query or parts.fragment:
            raise PropertyError(f"a URL-prefix property cannot contain a query or fragment: {value!r}")
        normalized = normalize_url(value)
        if not normalized.endswith("/"):
            normalized += "/"
        return cls(normalized)

    @property
    def is_domain(self) -> bool:
        return self.site_url.startswith("sc-domain:")

    @property
    def domain(self) -> str:
        if self.is_domain:
            return self.site_url.split(":", 1)[1]
        return _host(urllib.parse.urlsplit(self.site_url).netloc)

    def contains(self, url: str) -> bool:
        try:
            parts = urllib.parse.urlsplit(url.strip())
        except ValueError:
            return False
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return False
        if self.is_domain:
            host = _host(parts.netloc)
            return host == self.domain or host.endswith("." + self.domain)
        return normalize_url(url).startswith(self.site_url)

    def __str__(self) -> str:
        return self.site_url
