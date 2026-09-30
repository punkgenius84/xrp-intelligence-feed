from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.doj_news_api import (
    DOJ_API_URL,
    DOJ_METHOD,
    DOJ_SOURCE_ID,
    DOJNewsAPIDiscovery,
    _parse,
    validate_doj_source,
)
from discovery.dispatch import DiscoveryRegistryError, load_discovery_sources, validate_discovery_sources


FIXTURE = Path(__file__).parent / "fixtures" / "doj_news_api.json"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def doj_source(**changes):
    source = {
        "source_id": DOJ_SOURCE_ID,
        "name": "U.S. Department of Justice",
        "authority_tier": 1,
        "category": "government",
        "discovery_method": DOJ_METHOD,
        "source_url": DOJ_API_URL,
        "enabled": True,
        "lookback_days": 180,
        "search_terms": ["cryptocurrency", "digital asset"],
        "page_size": 50,
        "max_pages": 1,
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
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(payload, *, headers=None):
    return HttpResponse(
        200, headers or {"content-type": "application/json"}, payload, DOJ_API_URL,
    )


def test_registered_source_is_bounded_and_official():
    source = next(item for item in load_discovery_sources() if item["source_id"] == DOJ_SOURCE_ID)
    assert source["source_url"] == DOJ_API_URL
    assert source["page_size"] == 50
    assert source["max_pages"] == 1
    assert "cryptocurrency" in source["search_terms"]


@pytest.mark.parametrize("changes", [
    {"source_url": "https://attacker.example/api/v1/press_releases.json"},
    {"page_size": 51},
    {"max_pages": 3},
    {"max_items": 201},
    {"search_terms": []},
    {"search_terms": ["crypto", "Crypto"]},
])
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_doj_source(doj_source(**changes))


def test_disabled_source_makes_no_requests():
    result = DOJNewsAPIDiscovery(
        doj_source(enabled=False), http=FakeHttp([]), now=lambda: STAMP,
    ).collect()
    assert result.status == "not_configured"


def test_parser_rejects_external_urls_and_keeps_valid_rows():
    payload = FIXTURE.read_bytes()
    import json
    value = json.loads(payload)
    value["results"].append({
        "date": "1789833600",
        "title": "External",
        "url": "https://evil.example/fake",
        "uuid": "33333333-3333-4333-8333-333333333333",
    })
    rows, complete = _parse(json.dumps(value).encode())
    assert complete is False
    assert len(rows) == 2
    assert all("justice.gov" in row["url"] for row in rows)


def test_collect_deduplicates_across_terms_and_extracts_metadata():
    payload = FIXTURE.read_bytes()
    http = FakeHttp([response(payload), response(payload)])
    result = DOJNewsAPIDiscovery(
        doj_source(search_terms=["cryptocurrency", "digital asset"]),
        http=http, now=lambda: STAMP,
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 2
    candidate = next(item for item in result.candidates if item.source_native_id.startswith("1111"))
    assert candidate.candidate_id == f"{DOJ_SOURCE_ID}:11111111-1111-4111-8111-111111111111"
    assert "cryptocurrency" in candidate.summary.lower()
    assert candidate.source_native_metadata["topic"] == "Financial Fraud"
    assert len(http.calls) == 2


def test_old_rows_are_filtered():
    import json
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["results"] = [dict(value["results"][0], date="1")]
    result = DOJNewsAPIDiscovery(
        doj_source(search_terms=["cryptocurrency"]), http=FakeHttp([response(json.dumps(value).encode())]),
        now=lambda: STAMP,
    ).collect()
    assert result.status == "empty"


def test_304_preserves_and_updates_validators():
    state = {"sources": {DOJ_SOURCE_ID: {"requests": {
        "cryptocurrency:page-0": {
            "etag": '"old"', "last_modified": "Wed, 23 Sep 2026 00:00:00 GMT"
        }
    }}}}
    result = DOJNewsAPIDiscovery(
        doj_source(search_terms=["cryptocurrency"]),
        http=FakeHttp([HttpResponse(
            304, {"etag": '"new"', "last-modified": "Wed, 24 Sep 2026 00:00:00 GMT"},
            b"", DOJ_API_URL,
        )]),
        now=lambda: STAMP,
    ).collect(state)
    assert result.status == "empty"
    assert result.state_updates["cryptocurrency:page-0"]["etag"] == '"new"'


def test_registry_rejects_untrusted_source_endpoint():
    payload = {"schema_version": 1, "sources": [doj_source(source_url="https://evil.example/")]}
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources(payload)


def test_pipeline_relevance_forwards_crypto_doj_signal(tmp_path, monkeypatch):
    import main
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    http = FakeHttp([response(FIXTURE.read_bytes())])
    monkeypatch.setattr(
        main, "collect_source",
        lambda source, state: DOJNewsAPIDiscovery(source, http=http, now=lambda: STAMP).collect(state),
    )
    result = main.run_pipeline(
        sources=[],
        state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[doj_source(search_terms=["cryptocurrency"])],
        discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
    )
    assert failures == []
    assert len(collected) == 2
    assert len(result.fresh) == 2
    crypto = result.fresh[0]
    assert "government" in crypto.relevance_categories
    assert crypto.relevance_score > 0
