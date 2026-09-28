from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.dispatch import (DiscoveryRegistryError, collect_source,
                                load_discovery_sources, validate_discovery_sources)
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "bis_media_releases.xml"
SOURCE_ID = "bis-media-releases"
METHOD = "bis_media_releases"
FEED_URL = "https://www.bis.org/doclist/all_pressrels.rss"
BASE = "https://www.bis.org/media-releases/"


def source(**overrides):
    value = {
        "source_id": SOURCE_ID,
        "name": "Bank for International Settlements",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": FEED_URL,
        "enabled": True,
        "lookback_days": 120,
        "max_items": 50,
    }
    value.update(overrides)
    return value


def response(body=b"", *, status=200, headers=None):
    default = {"content-type": "application/rss+xml; charset=utf-8", "etag": 'W/"fixture"'}
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


def rdf(*items):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<rdf:RDF xmlns="http://purl.org/rss/1.0/" '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<channel rdf:about="https://www.bis.org/doclist/all_pressrels.rss">'
            '<title>Media releases</title><link>https://www.bis.org/doclist/all_pressrels.rss</link>'
            '<description>Media releases</description></channel>'
            + "".join(items) + '</rdf:RDF>').encode()


def item(slug, title="A release", date="2026-09-23T00:00:00Z", link=None, description="Summary."):
    link = link if link is not None else f"{BASE}{slug}"
    date_tag = f"<dc:date>{date}</dc:date>" if date else ""
    return (f'<item rdf:about="{link}"><title>{title}</title><link>{link}</link>'
            f'<description>{description}</description>{date_tag}</item>')


def run(body, *, config=None, state=None):
    http = FakeHttp([response(body)] if not isinstance(body, list) else body)
    result = collect_source(config or source(), state, http=http, now=lambda: STAMP)
    return result, http


def test_registry_contains_bis_source_and_dispatches():
    configured = load_discovery_sources()
    bis = next(row for row in configured if row["source_id"] == SOURCE_ID)
    assert bis["discovery_method"] == METHOD
    assert bis["source_url"] == FEED_URL
    assert bis["lookback_days"] == 120


def test_bis_source_contract_is_accepted():
    payload = {"schema_version": 1, "sources": [source()]}
    assert validate_discovery_sources(payload) == [source()]


@pytest.mark.parametrize("changes", [
    {"source_url": "https://example.org/feed.rss"},
    {"source_id": "arbitrary-bis"},
    {"lookback_days": 0},
    {"lookback_days": 366},
    {"max_items": 101},
    {"authority_tier": 2},
    {"enabled": "yes"},
])
def test_registry_rejects_invalid_bis_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_real_feed_is_rss10_and_lookback_selects_recent_releases():
    result, http = run(FIXTURE.read_bytes())
    assert result.status == "success"
    assert [call[0] for call in http.calls] == [FEED_URL]
    ids = [c.source_native_id for c in result.candidates]
    assert len(ids) == 5
    assert ids[0].startswith("20260923-basel-iii-risk-based-capital")
    assert all(c.published_at >= datetime(2026, 5, 30, 23, 0, tzinfo=timezone.utc)
               for c in result.candidates)


def test_wider_lookback_returns_all_ten_real_releases():
    result, _ = run(FIXTURE.read_bytes(), config=source(lookback_days=200))
    assert len(result.candidates) == 10
    assert len({c.candidate_id for c in result.candidates}) == 10


def test_identity_url_date_and_title_are_preserved_exactly():
    result, _ = run(FIXTURE.read_bytes())
    first = result.candidates[0]
    assert first.candidate_id == f"{SOURCE_ID}:{first.source_native_id}"
    assert first.url == BASE + first.source_native_id
    assert first.primary_url == first.url
    assert first.published_at == datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    assert first.document_type == "BIS Media Release"
    assert first.provenance == [FEED_URL, first.url]
    assert first.title.startswith("Basel III risk-based capital and leverage ratios are stable")
    assert "  " not in first.title
    assert first.summary.startswith("Bank for International Settlements media release")
    assert first.source_native_metadata["feed_id"] == "media_releases"


def test_feed_is_fetched_with_saved_validators_and_saved_after_success():
    state = {"sources": {SOURCE_ID: {"requests": {"media_releases": {
        "etag": 'W/"old"', "last_modified": "Sat, 26 Sep 2026 17:08:20 GMT"}}}}}
    result, http = run(FIXTURE.read_bytes(), state=state)
    kwargs = http.calls[0][1]
    assert kwargs["etag"] == 'W/"old"'
    assert kwargs["last_modified"] == "Sat, 26 Sep 2026 17:08:20 GMT"
    assert result.state_updates == {"media_releases": {"etag": 'W/"fixture"'}}


def test_not_modified_makes_no_candidates_and_keeps_validators():
    state = {"sources": {SOURCE_ID: {"requests": {"media_releases": {"etag": 'W/"old"'}}}}}
    result, _ = run([response(status=304, headers={})], state=state)
    assert result.status == "empty"
    assert result.candidates == []
    assert result.state_updates == {"media_releases": {"etag": 'W/"old"'}}


def test_rss20_or_wrong_document_type_fails_closed_without_saving_validator():
    body = (b'<rss version="2.0"><channel><title>x</title><item><title>t</title>'
            b'<link>https://www.bis.org/media-releases/20260923-x</link></item></channel></rss>')
    result, _ = run(body)
    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}
    assert any("malformed RSS document" in error for error in result.errors)


@pytest.mark.parametrize("bad_item", [
    item("20260923-x", link="https://example.org/media-releases/20260923-x"),
    item("20260923-x", link="http://www.bis.org/media-releases/20260923-x"),
    item("bad slug/../x", link="https://www.bis.org/media-releases/../secret"),
    item("20260923-x", date=""),
    item("20260923-x", date="yesterday"),
    item("20260923-x", date="2026-09-23T00:00:00"),
    item("20260923-x", title=""),
])
def test_malformed_item_is_skipped_and_validator_is_not_saved(bad_item):
    good = item("20260908-good-release", title="Good release", date="2026-09-08T00:00:00Z")
    result, _ = run(rdf(bad_item, good))
    assert result.status == "partial"
    assert [c.source_native_id for c in result.candidates] == ["20260908-good-release"]
    assert result.state_updates == {}
    assert any("malformed item" in error for error in result.errors)


def test_conflicting_duplicate_identity_is_rejected():
    first = item("20260908-dup", title="One", date="2026-09-08T00:00:00Z")
    second = item("20260908-dup", title="Two", date="2026-09-08T00:00:00Z")
    result, _ = run(rdf(first, second))
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "One"
    assert result.state_updates == {}


def test_item_limit_truncates_and_does_not_save_validator():
    result, _ = run(FIXTURE.read_bytes(), config=source(lookback_days=200, max_items=3))
    assert result.status == "partial"
    assert len(result.candidates) == 3
    assert result.state_updates == {}
    assert any("item limit exceeded" in error for error in result.errors)


def test_http_error_reports_failed_without_state():
    error = DiscoveryHttpError("boom", kind="http_status", status_code=404)
    result, _ = run([error])
    assert result.status == "failed"
    assert result.state_updates == {}
    assert any("404" in text for text in result.errors)


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = collect_source(source(enabled=False), http=http, now=lambda: STAMP)
    assert result.status == "not_configured"
    assert http.calls == []


def _scored(title, summary):
    news = NewsItem(title=title, url=BASE + "20260923-example", source="Bank for International Settlements",
                    source_id=SOURCE_ID, authority_tier=1, category="regulatory", summary=summary)
    detect_entities(news)
    classify_source_quality(news)
    score_relevance(news)
    return news


def test_adapter_summary_lets_existing_entity_detection_recognize_bis():
    result, _ = run(FIXTURE.read_bytes())
    news = _scored(result.candidates[0].title, result.candidates[0].summary)
    assert "BIS" in news.detected_entities


def test_us_commerce_bis_is_not_mistaken_for_the_bank_for_international_settlements():
    news = _scored("Bureau of Industry and Security (BIS) issues export control rule for chips",
                   "Commerce Department BIS announcement.")
    assert "BIS" not in news.detected_entities


def test_bis_authority_alone_does_not_make_a_generic_release_relevant():
    news = _scored("BIS announces new office opening",
                   "Bank for International Settlements media release: a new representative office.")
    assert "BIS" in news.detected_entities
    assert news.relevance_score == 0


def test_bis_digital_asset_publication_reaches_existing_relevance_scoring():
    news = _scored("BIS publishes report on stablecoins and cross-border payments",
                   "Bank for International Settlements media release: analysis of stablecoin payment risks.")
    assert "BIS" in news.detected_entities
    assert news.relevance_score > 0
