from __future__ import annotations

import gzip
import io
from pathlib import Path

import pytest

from gsc_url_inspector.property import Property, PropertyError, normalize_url
from gsc_url_inspector.sources import (
    SitemapError,
    SitemapReader,
    collect_urls,
    dedupe,
    read_url_file,
    read_url_lines,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc> https://www.example.com/a </loc></url>
  <url><loc>https://www.example.com/b</loc><lastmod>2026-01-01</lastmod></url>
</urlset>"""


def index_of(*locs: str) -> bytes:
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'.encode()


# -- property ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.example.com", "https://www.example.com/"),
        ("HTTPS://WWW.Example.com/", "https://www.example.com/"),
        ("https://www.example.com:443/blog", "https://www.example.com/blog/"),
        ("sc-domain:Example.COM", "sc-domain:example.com"),
    ],
)
def test_property_parse(raw, expected):
    assert Property.parse(raw).site_url == expected


@pytest.mark.parametrize("raw", ["", "example.com", "ftp://example.com/", "sc-domain:", "https://x.com/?a=1"])
def test_property_parse_rejects(raw):
    with pytest.raises(PropertyError):
        Property.parse(raw)


def test_url_prefix_contains():
    p = Property.parse("https://www.example.com/blog/")
    assert p.contains("https://www.example.com/blog/post")
    assert p.contains("https://WWW.EXAMPLE.COM/blog/")
    assert not p.contains("https://www.example.com/shop")
    assert not p.contains("http://www.example.com/blog/post")
    assert not p.contains("https://example.com/blog/post")
    assert not p.contains("mailto:x@example.com")
    assert p.domain == "www.example.com" and not p.is_domain


def test_domain_property_contains_subdomains_and_schemes():
    p = Property.parse("sc-domain:example.com")
    assert p.contains("http://example.com/")
    assert p.contains("https://shop.example.com/x")
    assert p.contains("https://user@www.example.com:8443/")
    assert not p.contains("https://notexample.com/")
    assert not p.contains("https://example.com.evil.test/")
    assert p.is_domain and str(p) == "sc-domain:example.com"


def test_normalize_url():
    assert normalize_url("HTTP://Example.com:80") == "http://example.com/"
    assert normalize_url("https://example.com/a?b=1#frag") == "https://example.com/a?b=1"


# -- url lists ----------------------------------------------------------------------------------------------------


def test_read_url_lines_skips_comments_and_takes_first_csv_column():
    text = "﻿# comment\n\nhttps://a.example/\nhttps://b.example/,extra\nnot a url\n  https://c.example/x \n"
    assert list(read_url_lines(io.StringIO(text))) == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/x",
    ]


def test_read_url_file_txt_and_csv():
    assert read_url_file(EXAMPLES / "urls.txt")[0] == "https://www.example.com/"
    assert read_url_file(EXAMPLES / "urls.csv") == [
        "https://www.example.com/",
        "https://www.example.com/pricing",
        "https://www.example.com/blog/post",
    ]


def test_read_csv_with_address_column(tmp_path):
    f = tmp_path / "crawl.csv"
    f.write_text("Status,Address\n200,https://x.example/a\n301,https://x.example/b\n")
    assert read_url_file(f) == ["https://x.example/a", "https://x.example/b"]


def test_dedupe_keeps_order():
    assert dedupe(["b", "a", "b", " a ", ""]) == ["b", "a"]


# -- sitemaps ------------------------------------------------------------------------------------------------------


def test_parse_urlset_and_index():
    assert SitemapReader.parse(URLSET) == ("urlset", ["https://www.example.com/a", "https://www.example.com/b"])
    assert SitemapReader.parse(index_of("https://x/1.xml")) == ("sitemapindex", ["https://x/1.xml"])


@pytest.mark.parametrize(
    "body",
    [
        b"<html></html>",
        b"not xml at all <",
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><urlset/>',
    ],
)
def test_parse_rejects(body):
    with pytest.raises(SitemapError):
        SitemapReader.parse(body)


def test_reader_follows_index_and_gzip(transport):
    transport.add(
        "GET",
        "https://www.example.com/sitemap.xml",
        200,
        index_of(
            "https://www.example.com/s1.xml", "https://www.example.com/s2.xml.gz", "https://www.example.com/missing.xml"
        ),
    )
    transport.add("GET", "https://www.example.com/s1.xml", 200, URLSET)
    transport.add(
        "GET",
        "https://www.example.com/s2.xml.gz",
        200,
        gzip.compress(URLSET.replace(b"/a", b"/c").replace(b"/b", b"/d")),
    )
    reader = SitemapReader(transport=transport)
    urls = list(reader.read("https://www.example.com/sitemap.xml"))
    assert urls == [
        "https://www.example.com/a",
        "https://www.example.com/b",
        "https://www.example.com/c",
        "https://www.example.com/d",
    ]
    assert any("missing.xml: HTTP 404" in e for e in reader.errors)


def test_reader_stops_at_depth_and_loops(transport):
    transport.add("GET", "https://x.example/a.xml", 200, index_of("https://x.example/b.xml"))
    transport.add("GET", "https://x.example/b.xml", 200, index_of("https://x.example/a.xml", "https://x.example/c.xml"))
    transport.add("GET", "https://x.example/c.xml", 200, index_of("https://x.example/d.xml"))
    transport.add("GET", "https://x.example/d.xml", 200, URLSET)
    reader = SitemapReader(transport=transport, max_depth=2)
    assert list(reader.read("https://x.example/a.xml")) == []
    assert any("deeper than 2" in e for e in reader.errors)
    # a.xml was requested once even though b.xml points back at it
    assert len(transport.calls("GET", "https://x.example/a.xml")) == 1


def test_reader_local_file_and_network_error(transport):
    reader = SitemapReader(transport=transport)
    assert len(list(reader.read(str(EXAMPLES / "sitemap.xml")))) == 3
    transport.error("GET", "https://down.example/sitemap.xml")
    assert list(reader.read("https://down.example/sitemap.xml")) == []
    assert list(reader.read("/no/such/file.xml")) == []
    assert len(reader.errors) == 2


def test_collect_urls_merges_sources(transport, tmp_path):
    transport.add("GET", "https://www.example.com/sitemap.xml", 200, URLSET)
    f = tmp_path / "list.txt"
    f.write_text("https://www.example.com/a\nhttps://www.example.com/z\n")
    urls, warnings = collect_urls(
        files=[f],
        sitemaps=["https://www.example.com/sitemap.xml"],
        urls=["https://www.example.com/first"],
        stdin=io.StringIO("https://www.example.com/stdin\n"),
        reader=SitemapReader(transport=transport),
    )
    assert urls == [
        "https://www.example.com/first",
        "https://www.example.com/a",
        "https://www.example.com/z",
        "https://www.example.com/stdin",
        "https://www.example.com/b",
    ]
    assert warnings == []


def test_collect_urls_dash_reads_stdin_and_limit():
    urls, warnings = collect_urls(files=["-"], stdin=io.StringIO("https://a.example/\nhttps://b.example/\n"), limit=1)
    assert urls == ["https://a.example/"]
    assert "only the first 1" in warnings[0]
