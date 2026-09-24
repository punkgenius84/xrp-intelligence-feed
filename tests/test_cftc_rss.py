from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.cftc_rss import CFTC_FEEDS, CFTC_SOURCE_ID, CFTCRSSDiscovery
from discovery.dispatch import DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from models import NewsItem


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def source(**overrides):
    value = {
        "source_id": CFTC_SOURCE_ID,
        "name": "U.S. Commodity Futures Trading Commission",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": "cftc_rss",
        "source_url": "https://www.cftc.gov/RSS/index.htm",
        "enabled": True,
        "lookback_days": 60,
        "max_items_per_feed": 50,
    }
    value.update(overrides)
    return value


def response(body=b"", *, status=200, headers=None, url="https://www.cftc.gov/RSS/RSSGP/rssgp.xml"):
    return HttpResponse(status, headers or {"content-type": "application/rss+xml",
                                            "etag": '"fixture"'}, body, url)


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


def make_adapter(http, **overrides):
    return CFTCRSSDiscovery(source(**overrides), http=http, now=lambda: STAMP)


def test_registry_dispatch_and_official_fixed_feed_configuration():
    configured = load_discovery_sources()
    cftc = next(row for row in configured if row["source_id"] == CFTC_SOURCE_ID)
    http = FakeHttp([response((FIXTURES / "cftc_general.xml").read_bytes()),
                     response((FIXTURES / "cftc_enforcement.xml").read_bytes())])
    result = collect_source(cftc, http=http, now=lambda: STAMP)
    assert result.status == "success"
    assert [call[0] for call in http.calls] == [feed["url"] for feed in CFTC_FEEDS.values()]
    assert all(url.startswith("https://www.cftc.gov/") for url, _ in http.calls)
    assert len(result.candidates) == 3


@pytest.mark.parametrize("changes", [
    {"source_url": "https://example.org/feed.xml"},
    {"source_id": "arbitrary-cftc"},
    {"lookback_days": 0},
    {"max_items_per_feed": 101},
    {"authority_tier": 2},
])
def test_registry_rejects_invalid_cftc_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_overlapping_feeds_deduplicate_by_native_release_number_and_keep_provenance():
    http = FakeHttp([response((FIXTURES / "cftc_general.xml").read_bytes()),
                     response((FIXTURES / "cftc_enforcement.xml").read_bytes())])
    result = make_adapter(http).collect()
    matching = [item for item in result.candidates if item.source_native_id == "9302-26"]
    assert len(matching) == 1
    candidate = matching[0]
    assert candidate.candidate_id == "cftc-press-releases:9302-26"
    assert candidate.source_native_metadata["release_number"] == "9302-26"
    assert candidate.source_native_metadata["feed_ids"] == "general,enforcement"
    assert candidate.source_native_metadata["feed_categories"] == "general,enforcement"
    assert set(candidate.provenance) == {
        CFTC_FEEDS["general"]["url"], CFTC_FEEDS["enforcement"]["url"], candidate.url,
    }


def test_release_number_date_title_and_article_url_are_preserved_without_fetching_article():
    http = FakeHttp([response((FIXTURES / "cftc_general.xml").read_bytes()),
                     response((FIXTURES / "cftc_enforcement.xml").read_bytes())])
    result = make_adapter(http).collect()
    candidate = next(item for item in result.candidates if item.source_native_id == "9302-26")
    assert candidate.title == "CFTC announces enforcement action involving XRP"
    assert candidate.published_at == datetime(2026, 9, 22, 20, 38, tzinfo=timezone.utc)
    assert candidate.url == "https://www.cftc.gov/PressRoom/PressReleases/9302-26"
    assert len(http.calls) == 2
    assert all(url in {feed["url"] for feed in CFTC_FEEDS.values()} for url, _ in http.calls)


@pytest.mark.parametrize("bad_url", [
    "http://www.cftc.gov/PressRoom/PressReleases/9302-26",
    "https://example.org/PressRoom/PressReleases/9302-26",
    "https://www.cftc.gov.evil.example/PressRoom/PressReleases/9302-26",
    "https://www.cftc.gov/PressRoom/PressReleases/not-a-release",
    "https://user@www.cftc.gov/PressRoom/PressReleases/9302-26",
])
def test_invalid_or_external_article_urls_are_skipped_and_validators_not_saved(bad_url):
    body = f'''<rss version="2.0"><channel><title>CFTC</title><item>
    <title>Valid-looking title</title><link>{bad_url}</link>
    <pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate></item></channel></rss>'''.encode()
    http = FakeHttp([response(body), response((FIXTURES / "cftc_enforcement.xml").read_bytes())])
    result = make_adapter(http).collect()
    # The General-feed row is invalid, but the same release legitimately exists
    # in the Enforcement feed. Assert its provenance came only from Enforcement.
    legitimate = next(item for item in result.candidates if item.source_native_id == "9302-26")
    assert legitimate.source_native_metadata["feed_id"] == "enforcement"
    assert "feed_ids" not in legitimate.source_native_metadata
    assert CFTC_FEEDS["general"]["url"] not in legitimate.provenance
    assert result.status == "partial"
    assert "general" not in result.state_updates
    assert any("General Press Releases: malformed item" in error for error in result.errors)


@pytest.mark.parametrize("item", [
    '<item><title>Missing link</title><pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate></item>',
    '<item><title>Missing date</title><link>https://www.cftc.gov/PressRoom/PressReleases/9302-26</link></item>',
    '<item><title>Bad date</title><link>https://www.cftc.gov/PressRoom/PressReleases/9302-26</link><pubDate>yesterday</pubDate></item>',
    '<item><title></title><link>https://www.cftc.gov/PressRoom/PressReleases/9302-26</link><pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate></item>',
])
def test_malformed_record_is_skipped_and_does_not_persist_that_feed_validator(item):
    body = f'''<rss version="2.0"><channel><title>CFTC</title>{item}
      <item><title>Valid release</title><link>https://www.cftc.gov/PressRoom/PressReleases/9299-26</link>
      <pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate></item></channel></rss>'''.encode()
    http = FakeHttp([response(body), response(b"<rss version='2.0'><channel><title>Empty</title></channel></rss>")])
    result = make_adapter(http).collect()
    assert [candidate.source_native_id for candidate in result.candidates] == ["9299-26"]
    assert result.status == "partial"
    assert "general" not in result.state_updates
    assert "enforcement" in result.state_updates


def test_valid_empty_feed_is_not_malformed_and_persists_validators():
    empty = b"<?xml version='1.0'?><rss version='2.0'><channel><title>Empty</title></channel></rss>"
    http = FakeHttp([response(empty), response(empty)])
    result = make_adapter(http).collect()
    assert result.status == "empty"
    assert result.candidates == []
    assert result.state_updates == {"general": {"etag": '"fixture"'},
                                    "enforcement": {"etag": '"fixture"'}}


def test_malformed_rss_container_retains_parseable_items_but_not_its_validator():
    malformed = b'''<rss version="2.0"><channel><title>CFTC</title>
      <item><title>Valid release</title><link>https://www.cftc.gov/PressRoom/PressReleases/9299-26</link>
      <pubDate>Tue, 22 Sep 2026 20:38:00 +0000</pubDate></item><broken></channel></rss>'''
    empty = b"<rss version='2.0'><channel><title>Empty</title></channel></rss>"
    result = make_adapter(FakeHttp([response(malformed), response(empty)])).collect()
    assert [item.source_native_id for item in result.candidates] == ["9299-26"]
    assert result.status == "partial"
    assert "general" not in result.state_updates


def test_validator_requests_and_persists_per_feed_and_304_keeps_existing_values():
    http = FakeHttp([response(status=304, headers={"content-type": "application/rss+xml"}),
                     response((FIXTURES / "cftc_enforcement.xml").read_bytes(),
                              headers={"content-type": "application/rss+xml", "last_modified": "Wed, 23 Sep 2026 12:00:00 GMT"})])
    state = {"sources": {CFTC_SOURCE_ID: {"requests": {
        "general": {"etag": '"old"', "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    }}}}
    result = make_adapter(http).collect(state)
    assert http.calls[0][1]["etag"] == '"old"'
    assert http.calls[0][1]["last_modified"].startswith("Tue,")
    assert result.state_updates["general"] == {"etag": '"old"',
                                                "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    assert result.state_updates["enforcement"] == {"last_modified": "Wed, 23 Sep 2026 12:00:00 GMT"}


def test_failed_feed_does_not_erase_other_feed_results_or_validators():
    http = FakeHttp([DiscoveryHttpError("timeout", kind="timeout"),
                     response((FIXTURES / "cftc_enforcement.xml").read_bytes())])
    result = make_adapter(http).collect()
    assert result.status == "partial"
    assert result.candidates
    assert "general" not in result.state_updates
    assert "enforcement" in result.state_updates


def test_lookback_and_item_processing_are_bounded():
    body = (FIXTURES / "cftc_general.xml").read_bytes()
    http = FakeHttp([response(body), response(body)])
    result = make_adapter(http, lookback_days=1, max_items_per_feed=1).collect()
    assert len(http.calls) == 2
    assert len(result.candidates) <= 2
    assert "item limit exceeded" in " ".join(result.errors)
    assert "general" not in result.state_updates


def test_cftc_authority_alone_does_not_make_generic_release_relevant():
    item = NewsItem(title="CFTC announces an office renovation project", url="https://www.cftc.gov/",
                    source="CFTC", source_id=CFTC_SOURCE_ID, authority_tier=1, category="regulatory")
    detect_entities(item)
    item.source_quality = "primary"
    score, _ = score_relevance(item)
    assert "CFTC" in item.detected_entities
    assert score == 0


def test_existing_digital_asset_signal_makes_cftc_release_relevant():
    item = NewsItem(title="CFTC announces enforcement action involving XRP", url="https://www.cftc.gov/",
                    source="CFTC", source_id=CFTC_SOURCE_ID, authority_tier=1, category="regulatory")
    detect_entities(item)
    item.source_quality = "primary"
    score, _ = score_relevance(item)
    assert "XRP" in item.detected_entities
    assert score > 0


def test_disabled_cftc_source_makes_no_requests():
    http = FakeHttp([])
    result = make_adapter(http, enabled=False).collect()
    assert result.status == "not_configured"
    assert http.calls == []


def test_cftc_candidates_and_validators_use_existing_discovery_state_lifecycle(
        tmp_path, monkeypatch):
    import main
    from discovery.base import DiscoveryResult
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    candidate = make_adapter(FakeHttp([
        response((FIXTURES / "cftc_general.xml").read_bytes()),
        response((FIXTURES / "cftc_enforcement.xml").read_bytes()),
    ])).collect().candidates[0]
    result = DiscoveryResult(
        CFTC_SOURCE_ID, "cftc_rss", "success", candidates=[candidate], fetched_at=STAMP,
        state_updates={"general": {"etag": '"g"'}, "enforcement": {"etag": '"e"'}},
    )
    monkeypatch.setattr(main, "collect_source", lambda configured, state: result)
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    main.run_pipeline(
        sources=[], state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[source()], discovery_state=store,
    )
    saved = store.load()["sources"][CFTC_SOURCE_ID]["requests"]
    assert saved["general"]["etag"] == '"g"'
    assert saved["enforcement"]["etag"] == '"e"'
