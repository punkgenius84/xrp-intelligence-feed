from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.dispatch import DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources
from discovery.http import HttpResponse
from discovery.federal_reserve_rss import _parse_feed


STAMP = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "federal-reserve-board-rss"
METHOD = "federal_reserve_rss"


def source(**overrides):
    value = {
        "source_id": SOURCE_ID,
        "name": "Board of Governors of the Federal Reserve System",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": "https://www.federalreserve.gov/feeds/feeds.htm",
        "enabled": True,
        "lookback_days": 60,
        "max_items_per_feed": 50,
    }
    value.update(overrides)
    return value


def response(body=b"", *, status=200, headers=None, url="https://www.federalreserve.gov/feeds/press_all.xml"):
    return HttpResponse(
        status,
        headers or {"content-type": "application/rss+xml", "etag": '"fixture"'},
        body,
        url,
    )


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


def test_registry_contains_federal_reserve_source_and_dispatches():
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    assert fed["discovery_method"] == METHOD
    assert fed["source_url"] == "https://www.federalreserve.gov/feeds/feeds.htm"


def test_federal_reserve_source_contract_is_accepted():
    payload = {"schema_version": 1, "sources": [source()]}
    validated = validate_discovery_sources(payload)
    assert validated == [source()]


@pytest.mark.parametrize("changes", [
    {"source_url": "https://example.org/feeds.htm"},
    {"source_id": "arbitrary-fed"},
    {"lookback_days": 0},
    {"max_items_per_feed": 101},
    {"authority_tier": 2},
])
def test_registry_rejects_invalid_federal_reserve_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_all_six_official_federal_reserve_press_feeds_are_polled():
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(fixture) for _ in range(6)])
    result = collect_source(fed, http=http, now=lambda: STAMP)
    assert result.status == "success"
    assert len(http.calls) == 6
    assert {call[0] for call in http.calls} == {
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.federalreserve.gov/feeds/press_bcreg.xml",
        "https://www.federalreserve.gov/feeds/press_enforcement.xml",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "https://www.federalreserve.gov/feeds/press_orders.xml",
        "https://www.federalreserve.gov/feeds/press_other.xml",
    }


def test_same_release_seen_in_multiple_fed_feeds_is_deduplicated_with_provenance():
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(fixture) for _ in range(6)])
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    result = collect_source(fed, http=http, now=lambda: STAMP)
    candidates = [item for item in result.candidates if item.source_native_id == "bcreg20260924a"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.candidate_id == f"{SOURCE_ID}:bcreg20260924a"
    assert len(candidate.provenance) == 7
    assert candidate.source_native_metadata["feed_ids"] == (
        "all,bcreg,enforcement,monetary,orders,other"
    )


def test_federal_reserve_release_identity_and_date_are_preserved_without_article_fetch():
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(fixture) for _ in range(6)])
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    result = collect_source(fed, http=http, now=lambda: STAMP)
    candidate = next(item for item in result.candidates if item.source_native_id == "bcreg20260924a")
    assert candidate.title.startswith("Federal Reserve Board requests public comment")
    assert candidate.published_at == datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
    assert candidate.url == "https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260924a.htm"
    assert candidate.document_type == "Federal Reserve Board Press Release"
    assert len(http.calls) == 6


def test_utf8_payload_with_stale_ascii_declaration_is_parsed():
    body = b'''<?xml version="1.0" encoding="us-ascii"?>
    <rss version="2.0"><channel><title>Federal Reserve</title><item>
    <title>Federal Reserve Board requests public comment on Caf\xc3\xa9 banking data</title>
    <link>https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260924a.htm</link>
    <pubDate>Thu, 24 Sep 2026 16:00:00 +0000</pubDate><guid>fed-utf8</guid>
    </item></channel></rss>'''
    parsed = _parse_feed(body)
    assert not parsed.bozo
    assert parsed.entries[0].title.endswith("Café banking data")


def test_invalid_external_article_url_is_skipped_and_feed_validator_is_not_saved():
    body = b'''<rss version="2.0"><channel><title>Federal Reserve</title><item>
    <title>Bad URL</title><link>https://example.org/not-fed</link>
    <pubDate>Thu, 24 Sep 2026 16:00:00 +0000</pubDate><guid>bad-guid</guid></item></channel></rss>'''
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(body)] + [response(fixture) for _ in range(5)])
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    result = collect_source(fed, http=http, now=lambda: STAMP)
    assert result.status == "partial"
    assert "all" not in result.state_updates
    assert any("malformed item" in error for error in result.errors)


def test_malformed_record_is_skipped_and_does_not_persist_that_feed_validator():
    body = b'''<rss version="2.0"><channel><title>Federal Reserve</title>
    <item><title>Missing date</title><link>https://www.federalreserve.gov/newsevents/pressreleases/2026-press.htm</link></item>
    <item><title>Valid release</title><link>https://www.federalreserve.gov/newsevents/pressreleases/2026-press.htm</link>
    <pubDate>Thu, 24 Sep 2026 16:00:00 +0000</pubDate><guid>fed-valid</guid></item>
    </channel></rss>'''
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(body)] + [response(fixture) for _ in range(5)])
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    result = collect_source(fed, http=http, now=lambda: STAMP)
    assert result.status == "partial"
    assert "all" not in result.state_updates


def test_lookback_and_item_processing_are_bounded():
    old = b'''<rss version="2.0"><channel><title>Federal Reserve</title>
    <item><title>Old release</title><link>https://www.federalreserve.gov/newsevents/pressreleases/2026-press.htm</link>
    <pubDate>Thu, 1 Jan 2026 16:00:00 +0000</pubDate><guid>old</guid></item></channel></rss>'''
    fixture = (FIXTURES / "federal_reserve_press.xml").read_bytes()
    http = FakeHttp([response(old)] + [response(fixture) for _ in range(5)])
    configured = load_discovery_sources()
    fed = next(row for row in configured if row["source_id"] == SOURCE_ID)
    result = collect_source(fed, http=http, now=lambda: STAMP)
    assert all(item.published_at >= datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc)
               for item in result.candidates)


def test_disabled_federal_reserve_source_makes_no_requests():
    from discovery.dispatch import collect_source
    http = FakeHttp([])
    result = collect_source(source(enabled=False), http=http, now=lambda: STAMP)
    assert result.status == "not_configured"
    assert http.calls == []
