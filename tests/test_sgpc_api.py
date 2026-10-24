from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from discovery.dispatch import create_strategy, load_discovery_sources
from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.sgpc_api import (
    SGPC_API_URL,
    SGPC_METHOD,
    SGPC_SOURCE_ID,
    SGPCMediaReleasesDiscovery,
    _official_article_url,
    _parse_payload,
    _published,
    validate_sgpc_source,
)
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality


FIXTURE = Path(__file__).parent / "fixtures" / "sgpc_api.json"
STAMP = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
BLOOM_ID = "456e3db0-935f-4dde-9f17-6d4b01a8eb93"


def sgpc_source(**changes):
    source = {
        "source_id": SGPC_SOURCE_ID,
        "name": "Singapore Government Press Centre",
        "authority_tier": 1,
        "category": "government",
        "discovery_method": SGPC_METHOD,
        "source_url": SGPC_API_URL,
        "enabled": True,
        "lookback_days": 30,
        "search_terms": ["BLOOM", "stablecoin", "digital money"],
        "max_terms_per_run": 2,
        "max_items": 100,
    }
    source.update(changes)
    return source


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def response(value, url=SGPC_API_URL, headers=None, status=200):
    body = b"" if status == 304 else json.dumps(value).encode("utf-8")
    return HttpResponse(status, headers or {"content-type": "application/json"}, body, url)


def test_registered_source_is_bounded_official_and_initially_disabled():
    configured = next(
        item for item in load_discovery_sources()
        if item["source_id"] == SGPC_SOURCE_ID
    )
    assert configured["enabled"] is False
    assert configured["source_url"] == SGPC_API_URL
    assert configured["max_terms_per_run"] == 4
    assert configured["lookback_days"] == 30
    strategy = create_strategy(configured, http=FakeHttp([]), sleep=lambda _: None)
    assert isinstance(strategy, SGPCMediaReleasesDiscovery)


@pytest.mark.parametrize("changes", [
    {"source_url": "https://attacker.example/api/ApiMainListing/SubmitData"},
    {"authority_tier": 2},
    {"lookback_days": 0},
    {"lookback_days": 91},
    {"search_terms": []},
    {"search_terms": ["BLOOM", "bloom"]},
    {"search_terms": ["x"] * 13},
    {"max_terms_per_run": 6},
    {"max_terms_per_run": 0},
    {"max_items": 101},
])
def test_invalid_source_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_sgpc_source(sgpc_source(**changes))


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = SGPCMediaReleasesDiscovery(
        sgpc_source(enabled=False), http=http, sleep=lambda _: None, now=lambda: STAMP
    ).collect()
    assert result.status == "not_configured"
    assert result.candidates == []
    assert http.calls == []


def test_live_fixture_creates_official_dated_candidates_and_rotates_terms():
    fixture = payload()
    http = FakeHttp([response(fixture), response(fixture), response(fixture)])
    source = sgpc_source(search_terms=["BLOOM", "stablecoin", "digital money"], max_terms_per_run=2)
    result = SGPCMediaReleasesDiscovery(
        source, http=http, sleep=lambda _: None, request_interval=0, now=lambda: STAMP
    ).collect()

    assert result.status == "success"
    assert len(result.candidates) == 2
    bloom = next(item for item in result.candidates if item.source_native_id == BLOOM_ID)
    assert bloom.title.startswith("MAS, Bank Negara Malaysia and Bank of Thailand")
    assert bloom.url == (
        "https://www.sgpc.gov.sg/media_releases/mas/press_release/P-20261023-1"
    )
    assert bloom.published_at == datetime(2026, 10, 23, 2, tzinfo=timezone.utc)
    assert bloom.source_native_metadata["agency_abbreviation"] == "MAS"
    assert bloom.source_native_metadata["published_at_local"] == "2026-10-23T10:00:00"
    assert "Official agency: MAS" in bloom.summary
    assert len(bloom.provenance) == 4
    assert result.pagination == {"next_term_index": 2, "requests_made": 3}
    assert len(http.calls) == 3

    for item in result.candidates:
        detect_entities(item)
        classify_source_quality(item)
        score_relevance(item)
    assert bloom.relevance_score >= 30
    generic = next(item for item in result.candidates if item.source_native_id != BLOOM_ID)
    assert generic.relevance_score < 30


def test_query_urls_are_date_bounded_and_search_terms_are_encoded():
    http = FakeHttp([response(payload()), response(payload()), response(payload())])
    source = sgpc_source(search_terms=["BLOOM", "digital money", "XRP"], max_terms_per_run=2)
    result = SGPCMediaReleasesDiscovery(
        source, http=http, sleep=lambda _: None, request_interval=0, now=lambda: STAMP
    ).collect()

    assert result.status == "success"
    latest_query = parse_qs(urlsplit(http.calls[0][0]).query)
    assert latest_query["StartDate"] == ["25-Sep-2026"]
    assert latest_query["EndDate"] == ["24-Oct-2026"]
    assert latest_query["page"] == ["1"]
    assert "KeySearch" not in latest_query
    bloom_query = parse_qs(urlsplit(http.calls[1][0]).query)
    assert bloom_query["KeySearch"] == ["BLOOM"]
    digital_query = parse_qs(urlsplit(http.calls[2][0]).query)
    assert digital_query["KeySearch"] == ["digital money"]


def test_rotation_resumes_at_saved_term_index():
    fixture = payload()
    http = FakeHttp([response(fixture), response(fixture), response(fixture)])
    state = {
        "sources": {
            SGPC_SOURCE_ID: {
                "pagination": {"next_term_index": 1},
            }
        }
    }
    result = SGPCMediaReleasesDiscovery(
        sgpc_source(search_terms=["BLOOM", "stablecoin", "digital money"], max_terms_per_run=2),
        http=http, sleep=lambda _: None, request_interval=0, now=lambda: STAMP,
    ).collect(state)
    assert parse_qs(urlsplit(http.calls[1][0]).query)["KeySearch"] == ["stablecoin"]
    assert parse_qs(urlsplit(http.calls[2][0]).query)["KeySearch"] == ["digital money"]
    assert result.pagination["next_term_index"] == 0


def test_malformed_rows_mark_source_partial_without_dropping_valid_release():
    value = payload()
    value["result"]["items"].append({
        "MediaReleaseId": "bad-link",
        "Title": "Unsafe external link",
        "DateTime": "2026-10-23T10:00:00",
        "Url": "https://attacker.example/media_releases/fake",
    })
    http = FakeHttp([response(value), response(value), response(value)])
    result = SGPCMediaReleasesDiscovery(
        sgpc_source(), http=http, sleep=lambda _: None, request_interval=0, now=lambda: STAMP
    ).collect()
    assert result.status == "partial"
    assert any("approved official media-release route" in error for error in result.errors)
    assert any(item.source_native_id == BLOOM_ID for item in result.candidates)


@pytest.mark.parametrize("url", [
    "http://www.sgpc.gov.sg/media_releases/mas/press_release/P-20261023-1",
    "https://sgpc.gov.sg.evil.example/media_releases/mas/press_release/P-20261023-1",
    "https://www.sgpc.gov.sg.evil.example/media_releases/mas/press_release/P-20261023-1",
    "https://www.sgpc.gov.sg/other/route",
    "https://www.sgpc.gov.sg/media_releases/../private",
    "https://user:pass@www.sgpc.gov.sg/media_releases/mas/press_release/P-20261023-1",
])
def test_official_article_url_rejects_untrusted_routes(url):
    with pytest.raises(ValueError):
        _official_article_url(url)


def test_publication_time_treats_naive_api_timestamps_as_singapore_local():
    published, raw = _published("2026-10-23T10:00:00")
    assert published == datetime(2026, 10, 23, 2, tzinfo=timezone.utc)
    assert raw == "2026-10-23T10:00:00"


def test_invalid_json_or_api_shape_is_rejected():
    with pytest.raises(ValueError, match="invalid SGPC JSON"):
        _parse_payload(b"{")
    with pytest.raises(ValueError, match="result.items"):
        _parse_payload(b'{"result": {}}')


def test_304_preserves_saved_request_validators():
    date_key = "2026-09-25:2026-10-24"
    state = {
        "sources": {
            SGPC_SOURCE_ID: {
                "requests": {
                    f"latest:{date_key}": {
                        "etag": '"old-latest"',
                        "last_modified": "Wed, 23 Oct 2026 02:00:00 GMT",
                    },
                    f"term:BLOOM:{date_key}": {
                        "etag": '"old-bloom"',
                        "last_modified": "Wed, 23 Oct 2026 02:00:00 GMT",
                    },
                }
            }
        }
    }
    http = FakeHttp([
        response({}, headers={"etag": '"new-latest"'}, status=304),
        response({}, headers={"etag": '"new-bloom"', "last-modified": "Thu, 24 Oct 2026 02:00:00 GMT"}, status=304),
        response(payload()),
    ])
    result = SGPCMediaReleasesDiscovery(
        sgpc_source(search_terms=["BLOOM", "stablecoin", "digital money"], max_terms_per_run=2),
        http=http, sleep=lambda _: None, request_interval=0, now=lambda: STAMP,
    ).collect(state)
    assert result.status == "success"
    assert result.state_updates[f"latest:{date_key}"]["etag"] == '"new-latest"'
    assert result.state_updates[f"term:BLOOM:{date_key}"]["etag"] == '"new-bloom"'
    assert result.state_updates[f"term:BLOOM:{date_key}"]["last_modified"] == "Thu, 24 Oct 2026 02:00:00 GMT"
