from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.dispatch import (
    DiscoveryRegistryError,
    collect_source,
    load_discovery_sources,
    validate_discovery_sources,
)
from discovery.http import DiscoveryHttpError, HttpResponse


STAMP = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "sec_press_releases.xml"
SOURCE_ID = "sec-press-releases"
METHOD = "sec_press_releases_rss"
FEED_URL = "https://www.sec.gov/news/pressreleases.rss"
SEC_FEED_ID = "press_releases"


def source(**overrides):
    value = {
        "source_id": SOURCE_ID,
        "name": "U.S. Securities and Exchange Commission",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": FEED_URL,
        "enabled": True,
        "lookback_days": 60,
        "max_items": 50,
    }
    value.update(overrides)
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


def rss(*items):
    return (b'<?xml version="1.0" encoding="utf-8"?>'
            b'<rss version="2.0"><channel><title>SEC Press Releases</title>'
            b'<link>https://www.sec.gov/newsroom/press-releases</link>'
            b'<description>SEC press releases</description>'
            + b"".join(items) + b"</channel></rss>")


def item(release_id, title="A release",
         date="Wed, 23 Sep 2026 16:00:00 +0000",
         link=None, description="Summary."):
    link = link if link is not None else (
        f"https://www.sec.gov/newsroom/press-releases/{release_id}"
    )
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{description}</description>"
        f"<pubDate>{date}</pubDate><guid>{release_id}</guid></item>"
    ).encode()


def run(body, *, config=None, state=None):
    http = FakeHttp([response(body)] if not isinstance(body, list) else body)
    result = collect_source(config or source(), state, http=http, now=lambda: STAMP)
    return result, http


def test_registry_contains_sec_press_source_and_dispatches():
    configured = load_discovery_sources()
    sec = next(row for row in configured if row["source_id"] == SOURCE_ID)
    assert sec["discovery_method"] == METHOD
    assert sec["source_url"] == FEED_URL


def test_sec_press_source_contract_is_accepted():
    payload = {"schema_version": 1, "sources": [source()]}
    assert validate_discovery_sources(payload) == [source()]


@pytest.mark.parametrize("changes", [
    {"source_url": "https://example.org/feed.rss"},
    {"source_id": "arbitrary-sec"},
    {"lookback_days": 0},
    {"max_items": 101},
    {"authority_tier": 2},
    {"enabled": "yes"},
])
def test_registry_rejects_invalid_sec_press_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_recent_release_is_parsed_and_legacy_official_url_is_canonicalized():
    body = rss(
        item("2026-93", title="SEC Publishes Updated Market Statistics"),
        item("2026-90", title="SEC Issues Innovation Exemption",
             link="https://www.sec.gov/news/pressreleases/2026-90.htm"),
    )
    result, http = run(body)
    assert result.status == "success"
    assert [call[0] for call in http.calls] == [FEED_URL]
    assert [c.source_native_id for c in result.candidates] == ["2026-93", "2026-90"]
    assert result.candidates[0].url == "https://www.sec.gov/newsroom/press-releases/2026-93"
    assert result.candidates[0].published_at == datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)
    assert result.candidates[0].document_type == "SEC Press Release"
    assert result.candidates[0].candidate_id == f"{SOURCE_ID}:2026-93"


def test_feed_is_fetched_with_saved_validators():
    state = {"sources": {SOURCE_ID: {"requests": {SEC_FEED_ID: {
        "etag": '"old"', "last_modified": "Sat, 26 Sep 2026 17:08:20 GMT"}}}}}
    result, http = run(FIXTURE.read_bytes(), state=state)
    kwargs = http.calls[0][1]
    assert kwargs["etag"] == '"old"'
    assert kwargs["last_modified"] == "Sat, 26 Sep 2026 17:08:20 GMT"
    assert result.state_updates == {SEC_FEED_ID: {"etag": '"fixture"'}}


def test_not_modified_keeps_validators_without_candidates():
    state = {"sources": {SOURCE_ID: {"requests": {SEC_FEED_ID: {"etag": '"old"'}}}}}
    result, _ = run([response(status=304, headers={})], state=state)
    assert result.status == "empty"
    assert result.candidates == []
    assert result.state_updates == {SEC_FEED_ID: {"etag": '"old"'}}


def test_invalid_external_url_is_skipped_and_validator_is_not_saved():
    body = rss(item("2026-93", link="https://example.org/news/2026-93"))
    result, _ = run(body)
    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}
    assert any("malformed item" in error for error in result.errors)


def test_missing_date_is_skipped_and_validator_is_not_saved():
    body = rss(item("2026-93", date=""))
    result, _ = run(body)
    assert result.status == "failed"
    assert result.state_updates == {}
    assert any("malformed item" in error for error in result.errors)


def test_item_limit_is_bounded_and_validator_is_not_saved():
    body = rss(
        item("2026-93"),
        item("2026-92"),
        item("2026-91"),
    )
    result, _ = run(body, config=source(max_items=2))
    assert result.status == "partial"
    assert len(result.candidates) == 2
    assert result.state_updates == {}
    assert any("item limit exceeded" in error for error in result.errors)


def test_lookback_filters_old_items():
    body = rss(
        item("2026-93"),
        item("2026-01", date="Thu, 1 Jan 2026 16:00:00 +0000"),
    )
    result, _ = run(body)
    assert [c.source_native_id for c in result.candidates] == ["2026-93"]


def test_conflicting_duplicate_identity_is_rejected():
    body = rss(
        item("2026-93", title="One"),
        item("2026-93", title="Two"),
    )
    result, _ = run(body)
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "One"
    assert result.state_updates == {}


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


def test_real_fixture_contains_current_sec_crypto_releases():
    result, _ = run(FIXTURE.read_bytes())
    titles = {c.title for c in result.candidates}
    assert "SEC Proposes New Regulation Crypto Assets" in titles
    assert "SEC Issues “Innovation Exemption” to Facilitate the Trading of Tokenized NMS Stock and Request for Comment" in titles
