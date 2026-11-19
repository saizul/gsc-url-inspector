"""URL sources: plain-text or CSV files, standard input and XML sitemaps (including sitemap indexes and .gz)."""

from __future__ import annotations

import csv
import gzip
import io
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .http import Transport, TransportError, UrllibTransport, decode_body

__all__ = [
    "SitemapError",
    "SitemapReader",
    "collect_urls",
    "compile_patterns",
    "dedupe",
    "filter_urls",
    "read_url_file",
    "read_url_lines",
]

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_SITEMAP_DEPTH = 3
# The sitemaps protocol caps one file at 50,000 URLs and 50 MB uncompressed.
MAX_SITEMAP_BYTES = 50 * 1024 * 1024


class SitemapError(Exception):
    """A sitemap could not be fetched or parsed."""


def _looks_like_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def read_url_lines(stream: TextIO) -> Iterator[str]:
    """One URL per line; blank lines and ``#`` comments are skipped; the first CSV column is used if present."""
    for raw in stream:
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if "," in line or "\t" in line:
            delimiter = "\t" if "\t" in line and "," not in line else ","
            first = next(csv.reader([line], delimiter=delimiter), [""])
            line = first[0].strip() if first else ""
        if _looks_like_url(line):
            yield line


def read_url_file(path: str | Path) -> list[str]:
    """Read a URL list. CSV files with a header use the ``url`` (or ``URL``/``address``) column when present."""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in (".csv", ".tsv"):
        dialect = "excel-tab" if p.suffix.lower() == ".tsv" else "excel"
        rows = list(csv.reader(io.StringIO(text), dialect=dialect))
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        for name in ("url", "address", "page", "loc"):
            if name in header:
                idx = header.index(name)
                return [r[idx].strip() for r in rows[1:] if len(r) > idx and _looks_like_url(r[idx].strip())]
    return list(read_url_lines(io.StringIO(text)))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class SitemapReader:
    """Fetch and expand sitemaps. Indexes are followed up to ``max_depth`` levels; each sitemap is read once."""

    transport: Transport = field(default_factory=UrllibTransport)
    timeout: float = 60.0
    max_depth: int = MAX_SITEMAP_DEPTH
    visited: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def _load(self, location: str) -> bytes:
        if _looks_like_url(location):
            try:
                resp = self.transport("GET", location, {"Accept": "application/xml,text/xml,*/*"}, None, self.timeout)
            except TransportError as exc:
                raise SitemapError(str(exc)) from exc
            if resp.status != 200:
                raise SitemapError(f"{location}: HTTP {resp.status}")
            body = resp.body
        else:
            try:
                body = Path(location).read_bytes()
            except OSError as exc:
                raise SitemapError(f"{location}: {exc}") from exc
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError) as exc:
                raise SitemapError(f"{location}: invalid gzip: {exc}") from exc
        else:
            body = decode_body(body, None)
        if len(body) > MAX_SITEMAP_BYTES:
            raise SitemapError(f"{location}: larger than {MAX_SITEMAP_BYTES} bytes uncompressed")
        return body

    @staticmethod
    def parse(body: bytes, location: str = "sitemap") -> tuple[str, list[str]]:
        """Return ``("urlset" | "sitemapindex", [loc, ...])``."""
        head = body[:4096].upper()
        if b"<!DOCTYPE" in head or b"<!ENTITY" in body.upper():
            raise SitemapError(f"{location}: DTDs and entities are not allowed in sitemaps")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SitemapError(f"{location}: invalid XML: {exc}") from exc
        kind = _local(root.tag)
        if kind not in ("urlset", "sitemapindex"):
            raise SitemapError(f"{location}: root element <{kind}> is not urlset or sitemapindex")
        child = "url" if kind == "urlset" else "sitemap"
        locs: list[str] = []
        for entry in root:
            if _local(entry.tag) != child:
                continue
            for el in entry:
                if _local(el.tag) == "loc" and el.text and el.text.strip():
                    locs.append(el.text.strip())
                    break
        return kind, locs

    def read(self, location: str, depth: int = 0) -> Iterator[str]:
        if location in self.visited:
            return
        self.visited.add(location)
        try:
            kind, locs = self.parse(self._load(location), location)
        except SitemapError as exc:
            self.errors.append(str(exc))
            return
        if kind == "urlset":
            yield from locs
            return
        if depth >= self.max_depth:
            self.errors.append(f"{location}: sitemap index nesting deeper than {self.max_depth}; skipped")
            return
        for loc in locs:
            yield from self.read(loc, depth + 1)


def dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def compile_patterns(patterns: Iterable[str | re.Pattern[str]]) -> list[re.Pattern[str]]:
    """Compile regular expressions; raises ``ValueError`` naming the bad pattern."""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            compiled.append(pattern)
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc
    return compiled


def filter_urls(
    urls: Iterable[str],
    include: Iterable[str | re.Pattern[str]] = (),
    exclude: Iterable[str | re.Pattern[str]] = (),
) -> tuple[list[str], int]:
    """Keep URLs that match any ``include`` pattern (all, when there is none) and no ``exclude`` pattern.

    Patterns are regular expressions searched anywhere in the URL, so ``/blog/`` matches every blog URL and
    ``\\.pdf$`` matches PDF files. Returns ``(kept, dropped_count)``.
    """
    inc = compile_patterns(include)
    exc = compile_patterns(exclude)
    kept: list[str] = []
    dropped = 0
    for url in urls:
        if (inc and not any(p.search(url) for p in inc)) or any(p.search(url) for p in exc):
            dropped += 1
            continue
        kept.append(url)
    return kept, dropped


def collect_urls(
    files: Iterable[str | Path] = (),
    sitemaps: Iterable[str] = (),
    urls: Iterable[str] = (),
    stdin: TextIO | None = None,
    reader: SitemapReader | None = None,
    limit: int | None = None,
    include: Iterable[str | re.Pattern[str]] = (),
    exclude: Iterable[str | re.Pattern[str]] = (),
) -> tuple[list[str], list[str]]:
    """Gather URLs from every source, in order, de-duplicated and filtered. Returns ``(urls, warnings)``.

    ``include``/``exclude`` patterns (see :func:`filter_urls`) are applied before ``limit``.
    """
    gathered: list[str] = list(urls)
    warnings: list[str] = []
    for f in files:
        if str(f) == "-":
            gathered.extend(read_url_lines(stdin or sys.stdin))
        else:
            gathered.extend(read_url_file(f))
    if stdin is not None and not any(str(f) == "-" for f in files):
        gathered.extend(read_url_lines(stdin))
    sitemaps = list(sitemaps)
    if sitemaps:
        reader = reader or SitemapReader()
        for sm in sitemaps:
            gathered.extend(reader.read(sm))
        warnings.extend(reader.errors)
    result = dedupe(gathered)
    include, exclude = list(include), list(exclude)
    if include or exclude:
        result, dropped = filter_urls(result, include, exclude)
        if dropped:
            warnings.append(f"{dropped} URL(s) skipped by --include/--exclude patterns")
    if limit is not None and len(result) > limit:
        warnings.append(f"{len(result)} URLs found; only the first {limit} will be inspected")
        result = result[:limit]
    return result, warnings
