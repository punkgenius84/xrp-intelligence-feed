from datetime import datetime, timezone

import pytest

from discovery.coinbase_blog_rss import (
    CoinbaseBlogRSSDiscovery, METHOD, SOURCE_URL, _identity,
    validate_coinbase_blog_rss_source,
)
from discovery.dispatch import collect_source, load_discovery_sources
from discovery.http import HttpResponse

STAMP = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
SOURCE = {
    "source_id": "coinbase-blog", "name": "Coinbase", "authority_tier": 1,
    "category": "institutional", "discovery_method": METHOD, "source_url": SOURCE_URL,
    "enabled": True, "lookback_days": 180, "max_items": 50,
}
RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Coinbase Blog</title>
<item><title>Coinbase and Citi expand stablecoin payments</title>
<link>https://www.coinbase.com/blog/coinbase-and-citi-expand-stablecoin-payments</link>
<pubDate>Wed, 14 Oct 2026 12:00:00 GMT</pubDate></item>
<item><title>Fake release</title><link>https://evil.example/blog/fake</link>
<pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_coinbase_rss_source_is_registered_and_strict():
    configured = next(item for item in load_discovery_sources() if item["source_id"] == "coinbase-blog")
    assert configured["discovery_method"] == METHOD
    assert configured["source_url"] == SOURCE_URL
    assert validate_coinbase_blog_rss_source(configured) == configured
    with pytest.raises(ValueError, match="official Coinbase RSS endpoint"):
        validate_coinbase_blog_rss_source({**SOURCE, "source_url": "https://evil.example/rss.xml"})


def test_coinbase_rss_collects_only_official_dated_articles():
    http = FakeHttp(HttpResponse(200, {"content-type": "application/rss+xml", "etag": '"cb-v1"'}, RSS, SOURCE_URL))
    result = CoinbaseBlogRSSDiscovery(SOURCE, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.title == "Coinbase and Citi expand stablecoin payments"
    assert candidate.url == "https://www.coinbase.com/blog/coinbase-and-citi-expand-stablecoin-payments"
    assert candidate.published_at == datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    assert candidate.source_id == "coinbase-blog"
    assert result.errors
    assert "malformed RSS item 2" in result.errors[0]
    assert http.calls[0][0] == SOURCE_URL
    assert http.calls[0][1]["expected_content_types"]


def test_coinbase_rss_requires_official_https_article_routes():
    assert _identity("https://www.coinbase.com/blog/official-story") is not None
    assert _identity("https://coinbase.com/en-gb/blog/official-story") is not None
    assert _identity("https://www.coinbase.com/blog/landing") is None
    assert _identity("https://www.coinbase.com.evil.example/blog/story") is None
    assert _identity("http://www.coinbase.com/blog/story") is None
    assert _identity("https://www.coinbase.com:444/blog/story") is None
    assert _identity("https://user@www.coinbase.com/blog/story") is None


def test_coinbase_rss_304_preserves_request_validators():
    http = FakeHttp(HttpResponse(304, {"etag": '"cb-v1"'}, b"", SOURCE_URL))
    state = {"sources": {"coinbase-blog": {"requests": {
        "feed": {"etag": '"cb-v1"', "last_modified": "Wed, 14 Oct 2026 12:00:00 GMT"}
    }}}}
    result = CoinbaseBlogRSSDiscovery(SOURCE, http=http, now=lambda: STAMP).collect(state)

    assert result.status == "not_modified"
    assert result.candidates == []
    assert result.errors == []
    assert http.calls[0][1]["etag"] == '"cb-v1"'
    assert http.calls[0][1]["last_modified"] == "Wed, 14 Oct 2026 12:00:00 GMT"


def test_registry_dispatches_coinbase_rss_strategy():
    configured = next(item for item in load_discovery_sources() if item["source_id"] == "coinbase-blog")
    http = FakeHttp(HttpResponse(200, {"content-type": "application/rss+xml"}, RSS, SOURCE_URL))
    result = collect_source(configured, http=http, now=lambda: STAMP)
    assert result.discovery_method == METHOD
    assert result.candidates
