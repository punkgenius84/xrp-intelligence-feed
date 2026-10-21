from datetime import datetime, timezone

import pytest

from discovery.dispatch import (DiscoveryRegistryError, collect_source,
                                load_discovery_sources, validate_discovery_sources)
from discovery.ecb_rss import ECBRSSDiscovery, FEED_URL, METHOD, SOURCE_ID, validate_ecb_source
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def source(**changes):
    value = {
        "source_id": SOURCE_ID,
        "name": "European Central Bank",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": FEED_URL,
        "enabled": True,
        "lookback_days": 90,
        "max_items": 50,
    }
    value.update(changes)
    return value


def response(body=b"", *, status=200, headers=None):
    default = {"content-type": "application/rss+xml; charset=utf-8", "etag": '"fixture"'}
    return HttpResponse(status, default if headers is None else headers, body, FEED_URL)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def item(slug="ecb.pr261021_1", title="ECB announces a digital euro settlement update",
         date="Wed, 21 Oct 2026 10:00:00 GMT",
         link=None, summary="Official central bank communication."):
    link = link or f"https://www.ecb.europa.eu/press/pr/2026/html/{slug}.en.html"
    return (f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
            f"<description>{summary}</description></item>")


def rss(*items):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>ECB press releases</title>'
            '<link>https://www.ecb.europa.eu/</link><description>Official ECB feed</description>'
            + "".join(items) + "</channel></rss>").encode()


def run(body, *, config=None, state=None):
    http = FakeHttp(body if isinstance(body, list) else [response(body)])
    result = collect_source(config or source(), state, http=http, now=lambda: STAMP)
    return result, http


def test_official_ecb_feed_is_registered_but_parked_until_live_probe():
    configured = load_discovery_sources()
    ecb = next(row for row in configured if row["source_id"] == SOURCE_ID)
    assert ecb["source_url"] == FEED_URL
    assert ecb["discovery_method"] == METHOD
    assert ecb["enabled"] is False
    http = FakeHttp([])
    result = collect_source(ecb, http=http, now=lambda: STAMP)
    assert result.status == "not_configured"
    assert http.calls == []


def test_registry_and_strategy_accept_exact_official_contract():
    assert validate_ecb_source(source()) == source()
    assert validate_discovery_sources({"schema_version": 1, "sources": [source()]}) == [source()]


@pytest.mark.parametrize("changes", [
    {"source_id": "other"},
    {"source_url": "https://example.org/feed.xml"},
    {"discovery_method": "institutional_press_html"},
    {"authority_tier": 2},
    {"enabled": "yes"},
    {"lookback_days": 0},
    {"lookback_days": 366},
    {"max_items": 0},
    {"max_items": 101},
])
def test_registry_rejects_invalid_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_rss_items_are_normalized_and_only_official_dated_articles_are_kept():
    good = item()
    bad_link = item(slug="evil", link="https://evil.example/press/pr/2026/evil.html")
    old = item(slug="old", date="Mon, 01 Jan 2024 10:00:00 GMT")
    result, http = run(rss(good, bad_link, old))
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert http.calls[0][0] == FEED_URL
    candidate = result.candidates[0]
    assert candidate.source_id == SOURCE_ID
    assert candidate.discovery_method == METHOD
    assert candidate.title == "ECB announces a digital euro settlement update"
    assert candidate.url == "https://www.ecb.europa.eu/press/pr/2026/html/ecb.pr261021_1.en.html"
    assert candidate.published_at == datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
    assert candidate.provenance == [FEED_URL, candidate.url]
    assert result.state_updates == {}
    assert any("missing official ECB" in error for error in result.errors)


@pytest.mark.parametrize("bad_url", [
    "http://www.ecb.europa.eu/press/pr/2026/html/a.en.html",
    "https://ecb.europa.eu.evil.example/press/pr/2026/html/a.en.html",
    "https://www.ecb.europa.eu.evil.example/press/pr/2026/html/a.en.html",
    "https://user@www.ecb.europa.eu/press/pr/2026/html/a.en.html",
    "https://www.ecb.europa.eu/press/../secret",
    "https://www.ecb.europa.eu/other/a.html",
])
def test_official_article_url_validation_fails_closed(bad_url):
    result, _ = run(rss(item(link=bad_url)))
    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}


def test_success_saves_validators_and_304_preserves_them():
    result, http = run(rss(item()))
    assert result.status == "success"
    assert result.state_updates == {"press_releases": {"etag": '"fixture"'}}
    assert http.calls[0][1]["expected_content_types"] == (
        "application/rss+xml", "application/xml", "text/xml", "application/atom+xml"
    )
    state = {"sources": {SOURCE_ID: {"requests": {"press_releases": {"etag": '"old"'}}}}}
    unchanged, http = run([response(status=304, headers={})], state=state)
    assert unchanged.status == "not_modified"
    assert unchanged.candidates == []
    assert unchanged.state_updates == {"press_releases": {"etag": '"old"'}}
    assert http.calls[0][1]["etag"] == '"old"'


def test_http_error_and_non_200_status_fail_without_state_updates():
    failed, _ = run([DiscoveryHttpError("blocked", kind="http_error", status_code=403)])
    assert failed.status == "failed"
    assert "403" in failed.errors[0]
    bad_status, _ = run([response(status=202)])
    assert bad_status.status == "failed"
    assert "202" in bad_status.errors[0]
    assert bad_status.state_updates == {}


def test_feed_item_limit_does_not_save_validator():
    result, _ = run(rss(item(slug="one"), item(slug="two")),
                    config=source(max_items=1))
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.state_updates == {}
    assert any("item limit exceeded" in error for error in result.errors)


def test_ecb_entity_detection_and_relevance_use_existing_pipeline():
    news = NewsItem(
        title="European Central Bank publishes digital euro settlement update",
        url="https://www.ecb.europa.eu/press/pr/2026/html/ecb.pr261021_1.en.html",
        source="European Central Bank",
        source_id=SOURCE_ID,
        authority_tier=1,
        category="regulatory",
        summary="European Central Bank official press feed discusses digital euro and cross-border payments.",
    )
    detect_entities(news)
    classify_source_quality(news)
    score_relevance(news)
    assert "ECB" in news.detected_entities
    assert news.relevance_score > 0
