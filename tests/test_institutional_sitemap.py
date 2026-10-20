from datetime import datetime, timezone
import hashlib

import pytest

from discovery.dispatch import create_strategy, load_discovery_sources, validate_discovery_sources
from discovery.http import HttpResponse
from discovery.institutional_sitemap import (
    METHOD,
    InstitutionalPressSitemapDiscovery,
    _sitemap_request_id,
    validate_institutional_sitemap_source,
)


SITEMAP_URL = "https://www.citigroup.com/global/sitemap.xml"
STAMP = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class SequenceHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _source(**changes):
    source = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "citi-press-releases"
    )
    return {**source, "enabled": True, **changes}


def _response(status, content, url, headers=None):
    return HttpResponse(status, headers or {"content-type": "application/xml"}, content, url)


def _sitemap(entries):
    items = "".join(
        f"<url><loc>{url}</loc><lastmod>{lastmod}</lastmod></url>"
        for url, lastmod in entries
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'.encode()


def _detail(url, title, date):
    return HttpResponse(
        200, {"content-type": "text/html"},
        f"<html><body><h1>{title}</h1><p>{date}</p></body></html>".encode(),
        url,
    )


def test_sitemap_strategy_is_registered_but_production_source_stays_disabled():
    source = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "citi-press-releases"
    )
    assert source["discovery_method"] == METHOD
    assert source["enabled"] is False
    assert isinstance(create_strategy(source), InstitutionalPressSitemapDiscovery)
    assert validate_discovery_sources({
        "schema_version": 1,
        "sources": [source],
    })[0]["source_id"] == "citi-press-releases"


def test_sitemap_strategy_uses_article_publication_date_not_lastmod():
    newer = "https://www.citigroup.com/global/news/press-release/2026/newer-release"
    older = "https://www.citigroup.com/global/news/press-release/2026/older-release"
    sitemap = _sitemap([
        (older, "2026-10-18T00:00:00Z"),
        (newer, "2026-10-20T11:00:00Z"),
        ("https://attacker.example/global/news/press-release/2026/fake", "2026-10-21"),
    ])
    http = SequenceHttp([
        _response(200, sitemap, SITEMAP_URL, {"content-type": "application/xml", "etag": "sitemap-v1"}),
        _detail(newer + "/", "Citi Newer Release", "October 19, 2026"),
        _detail(older + "/", "Citi Older Release", "October 17, 2026"),
    ])
    result = InstitutionalPressSitemapDiscovery(
        _source(max_items=2, sitemap_detail_limit=2),
        http=http,
        now=lambda: STAMP,
    ).collect(source_state={})

    assert result.status == "success", result.errors
    assert [candidate.title for candidate in result.candidates] == ["Citi Newer Release", "Citi Older Release"]
    assert [candidate.published_at for candidate in result.candidates] == [
        datetime(2026, 10, 19, tzinfo=timezone.utc),
        datetime(2026, 10, 17, tzinfo=timezone.utc),
    ]
    assert all(candidate.discovery_method == METHOD for candidate in result.candidates)
    assert len(http.calls) == 3
    assert http.calls[0][1]["expected_content_types"] == (
        "application/xml", "text/xml", "application/octet-stream"
    )
    assert result.pagination["citi_sitemap_sha256"] == hashlib.sha256(sitemap).hexdigest()
    assert _sitemap_request_id(SITEMAP_URL) in result.state_updates


def test_sitemap_strategy_handles_sitemap_index_with_bounded_child_fetch():
    article = "https://www.citigroup.com/global/news/press-release/2026/example-release"
    root = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://www.citigroup.com/global/sitemap-news.xml</loc><lastmod>2026-10-20</lastmod></sitemap>
      <sitemap><loc>https://attacker.example/global/sitemap.xml</loc></sitemap>
    </sitemapindex>"""
    child = _sitemap([(article, "2026-10-20")])
    http = SequenceHttp([
        _response(200, root, SITEMAP_URL),
        _response(200, child, "https://www.citigroup.com/global/sitemap-news.xml"),
        _detail(article + "/", "Citi Example Release", "October 20, 2026"),
    ])
    result = InstitutionalPressSitemapDiscovery(
        _source(max_items=1, sitemap_detail_limit=1),
        http=http,
        now=lambda: STAMP,
    ).collect(source_state={})

    assert result.status == "success", result.errors
    assert len(result.candidates) == 1
    assert len(http.calls) == 3
    assert http.calls[1][0] == "https://www.citigroup.com/global/sitemap-news.xml"


def test_sitemap_conditional_request_returns_not_modified_without_detail_fetch():
    source = _source()
    request_id = _sitemap_request_id(SITEMAP_URL)
    state = {"sources": {"citi-press-releases": {"requests": {request_id: {"etag": "tag-v1"}}}}}
    http = SequenceHttp([_response(304, b"", SITEMAP_URL, {"etag": "tag-v1"})])

    result = InstitutionalPressSitemapDiscovery(source, http=http, now=lambda: STAMP).collect(state)

    assert result.status == "not_modified"
    assert len(http.calls) == 1
    assert http.calls[0][1]["etag"] == "tag-v1"
    assert result.state_updates[request_id]["etag"] == "tag-v1"


def test_unchanged_sitemap_hash_skips_all_detail_fetches_without_etag_support():
    sitemap = _sitemap([
        ("https://www.citigroup.com/global/news/press-release/2026/example-release", "2026-10-20")
    ])
    digest = hashlib.sha256(sitemap).hexdigest()
    state = {"sources": {"citi-press-releases": {"pagination": {"citi_sitemap_sha256": digest}}}}
    http = SequenceHttp([_response(200, sitemap, SITEMAP_URL)])

    result = InstitutionalPressSitemapDiscovery(
        _source(max_items=1, sitemap_detail_limit=1),
        http=http,
        now=lambda: STAMP,
    ).collect(state)

    assert result.status == "empty"
    assert result.candidates == []
    assert len(http.calls) == 1
    assert result.pagination["citi_sitemap_sha256"] == digest


def test_sitemap_strategy_rejects_article_redirect_and_does_not_advance_state():
    article = "https://www.citigroup.com/global/news/press-release/2026/example-release"
    sitemap = _sitemap([(article, "2026-10-20")])
    http = SequenceHttp([
        _response(200, sitemap, SITEMAP_URL),
        _detail("https://attacker.example/global/news/press-release/2026/example-release", "Fake release", "October 20, 2026"),
    ])
    result = InstitutionalPressSitemapDiscovery(
        _source(max_items=1, sitemap_detail_limit=1),
        http=http,
        now=lambda: STAMP,
    ).collect(source_state={})

    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}
    assert result.pagination == {}
    assert any("invalid status or final URL" in error for error in result.errors)


def test_sitemap_strategy_rejects_invalid_detail_limit_and_unsafe_xml():
    source = _source(sitemap_detail_limit=11)
    with pytest.raises(ValueError, match="sitemap_detail_limit"):
        validate_institutional_sitemap_source(source)

    source = _source(sitemap_detail_limit=1)
    http = SequenceHttp([_response(200, b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><urlset/>", SITEMAP_URL)])
    result = InstitutionalPressSitemapDiscovery(source, http=http, now=lambda: STAMP).collect(source_state={})
    assert result.status == "failed"
    assert any("invalid sitemap XML" in error for error in result.errors)
