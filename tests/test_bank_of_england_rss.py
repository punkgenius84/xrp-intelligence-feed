from datetime import datetime, timezone

import pytest

from discovery.bank_of_england_rss import (
    BankOfEnglandRSSDiscovery, FEED_URL, METHOD, SOURCE_ID,
    validate_bank_of_england_source,
)
from discovery.dispatch import (
    DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources,
)
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from models import NewsItem


STAMP = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def source(**changes):
    value = {
        "source_id": SOURCE_ID,
        "name": "Bank of England",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": FEED_URL,
        "enabled": True,
        "lookback_days": 180,
        "max_items": 50,
    }
    value.update(changes)
    return value


def response(body=b"", *, status=200, headers=None):
    default = {"content-type": "text/xml; charset=utf-8", "etag": '"fixture"'}
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


def item(slug="news/2026/october/official-stablecoin-rule", title="Bank of England announces stablecoin oversight measures",
         date="Wed, 21 Oct 2026 14:00:00 GMT", link=None, summary="Official central bank communication."):
    link = link or f"https://www.bankofengland.co.uk/{slug}"
    return (
        f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
        f"<description>{summary}</description></item>"
    )


def rss(*items):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Bank of England News</title>'
        '<link>https://www.bankofengland.co.uk/news/</link><description>Official feed</description>'
        + "".join(items) + "</channel></rss>"
    ).encode()


def run(body, *, config=None, state=None):
    http = FakeHttp(body if isinstance(body, list) else [response(body)])
    result = collect_source(config or source(), state, http=http, now=lambda: STAMP)
    return result, http


def test_bank_of_england_feed_is_registered_and_enabled_after_live_probe():
    configured = load_discovery_sources()
    row = next(item for item in configured if item["source_id"] == SOURCE_ID)
    assert row["source_url"] == FEED_URL
    assert row["discovery_method"] == METHOD
    assert row["enabled"] is True
    assert validate_bank_of_england_source(row) == row


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = collect_source(source(enabled=False), http=http, now=lambda: STAMP)
    assert result.status == "not_configured"
    assert http.calls == []


def test_registry_and_strategy_accept_exact_official_contract():
    assert validate_bank_of_england_source(source()) == source()
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
    bad_link = item(slug="bad", link="https://evil.example/news/2026/october/fake")
    old = item(slug="news/2024/january/old", date="Mon, 01 Jan 2024 10:00:00 GMT")
    result, http = run(rss(good, bad_link, old))
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert http.calls[0][0] == FEED_URL
    candidate = result.candidates[0]
    assert candidate.source_id == SOURCE_ID
    assert candidate.discovery_method == METHOD
    assert candidate.title == "Bank of England announces stablecoin oversight measures"
    assert candidate.url == "https://www.bankofengland.co.uk/news/2026/october/official-stablecoin-rule"
    assert candidate.published_at == datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    assert candidate.provenance == [FEED_URL, candidate.url]
    assert result.state_updates == {}
    assert any("missing official Bank of England" in error for error in result.errors)


@pytest.mark.parametrize("bad_url", [
    "http://www.bankofengland.co.uk/news/2026/october/article",
    "https://bankofengland.co.uk.evil.example/news/2026/october/article",
    "https://www.bankofengland.co.uk.evil.example/news/2026/october/article",
    "https://user@www.bankofengland.co.uk/news/2026/october/article",
    "https://www.bankofengland.co.uk/news/2026/../secret",
    "https://www.bankofengland.co.uk/news//2026/article",
    "https://www.bankofengland.co.uk/rss/news",
    "https://www.bankofengland.co.uk/news/2026/october/article?utm_source=x",
])
def test_official_article_url_validation_fails_closed(bad_url):
    result, _ = run(rss(item(link=bad_url)))
    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}


def test_success_saves_validators_and_304_preserves_them():
    result, http = run(rss(item()))
    assert result.status == "success"
    assert result.state_updates == {"news": {"etag": '"fixture"'}}
    assert http.calls[0][1]["expected_content_types"] == (
        "application/rss+xml", "application/xml", "text/xml", "application/atom+xml"
    )
    state = {"sources": {SOURCE_ID: {"requests": {"news": {"etag": '"old"'}}}}}
    unchanged, http = run([response(status=304, headers={})], state=state)
    assert unchanged.status == "not_modified"
    assert unchanged.candidates == []
    assert unchanged.state_updates == {"news": {"etag": '"old"'}}
    assert http.calls[0][1]["etag"] == '"old"'


def test_http_error_and_non_200_status_fail_without_state_updates():
    failed, _ = run([DiscoveryHttpError("blocked", kind="http_error", status_code=403)])
    assert failed.status == "failed"
    assert "403" in failed.errors[0]
    bad_status, _ = run([response(status=503)])
    assert bad_status.status == "failed"
    assert bad_status.state_updates == {}


def test_malformed_feed_does_not_advance_validators():
    result, _ = run(b"<html>not an RSS feed</html>")
    assert result.status == "failed"
    assert result.state_updates == {}


def test_relevance_recognizes_bank_of_england_as_regulator_for_digital_asset_actions():
    title = "Bank of England publishes stablecoin oversight measures"
    item = NewsItem(
        title=title,
        url="https://www.bankofengland.co.uk/news/2026/october/stablecoin-rule",
        source="Bank of England",
        published_at=STAMP,
        collected_at=STAMP,
        summary="The Bank of England publishes a stablecoin rule for payment firms.",
        source_type="discovery",
        source_id=SOURCE_ID,
        detected_entities=detect_entities(title + " The Bank of England publishes a stablecoin rule."),
        source_quality="primary",
    )
    score, reasons = score_relevance(item)
    assert score > 0
    assert "category:relevant_regulatory_digital_asset_payments" in reasons
