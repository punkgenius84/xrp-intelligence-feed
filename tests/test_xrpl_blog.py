from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.http import HttpResponse
from discovery.xrpl_blog import (
    XRPL_METHOD,
    XRPL_SOURCE_ID,
    XRPL_URL,
    XRPLBlogDiscovery,
    _parse_page,
    validate_xrpl_source,
)
from discovery.dispatch import DiscoveryRegistryError, load_discovery_sources, validate_discovery_sources


FIXTURE = Path(__file__).parent / "fixtures" / "xrpl_blog.html"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def xrpl_source(**changes):
    source = {
        "source_id": XRPL_SOURCE_ID,
        "name": "XRP Ledger Community",
        "authority_tier": 1,
        "category": "technology",
        "discovery_method": XRPL_METHOD,
        "source_url": XRPL_URL,
        "enabled": True,
        "lookback_days": 180,
        "max_items": 50,
        "max_pages": 4,
    }
    source.update(changes)
    return source


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def html_response(payload, *, headers=None):
    return HttpResponse(200, headers or {"content-type": "text/html"}, payload, XRPL_URL)


def test_registered_source_is_bounded_and_official():
    source = next(item for item in load_discovery_sources() if item["source_id"] == XRPL_SOURCE_ID)
    assert source["source_url"] == XRPL_URL
    assert source["max_pages"] == 4


@pytest.mark.parametrize("changes", [
    {"source_url": "https://evil.example/blog"},
    {"lookback_days": 366},
    {"max_pages": 7},
    {"max_items": 101},
])
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_xrpl_source(xrpl_source(**changes))


def test_parser_keeps_only_official_articles():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["slug"].startswith("2026/")
    assert rows[1]["date"] == datetime(2026, 9, 16, tzinfo=timezone.utc)


def test_collect_deduplicates():
    payload = FIXTURE.read_bytes()
    result = XRPLBlogDiscovery(
        xrpl_source(max_pages=2),
        http=FakeHttp([html_response(payload), html_response(payload)]),
        now=lambda: STAMP,
    ).collect()
    assert result.status == "partial"
    assert len(result.candidates) == 2
    assert len(result.candidates) == len({item.candidate_id for item in result.candidates})


def test_old_rows_stop_pagination():
    old = b'<html><body><h4><a href="/blog/2020/old">Old</a></h4><div>Jan 1, 2020</div></body></html>'
    result = XRPLBlogDiscovery(
        xrpl_source(lookback_days=30), http=FakeHttp([html_response(old)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "empty"
    assert result.pagination["pages_checked"] == 1


def test_malformed_page_fails_closed():
    bad = b"<html><body><h4><a href='/blog/2026/bad'>Bad</a>"
    result = XRPLBlogDiscovery(
        xrpl_source(max_pages=1), http=FakeHttp([html_response(bad)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates


def test_304_preserves_validator_state():
    from discovery.http import HttpResponse
    state = {"sources": {XRPL_SOURCE_ID: {"requests": {
        "page-1": {"etag": '"old"', "last_modified": "Wed, 23 Sep 2026 00:00:00 GMT"}
    }}}}
    result = XRPLBlogDiscovery(
        xrpl_source(max_pages=1),
        http=FakeHttp([HttpResponse(
            304, {"etag": '"new"', "last-modified": "Thu, 24 Sep 2026 00:00:00 GMT"},
            b"", XRPL_URL,
        )]),
        now=lambda: STAMP,
    ).collect(state)
    assert result.status == "empty"
    assert result.state_updates["page-1"]["etag"] == '"new"'


def test_pipeline_scores_xrpl_primary_source(tmp_path, monkeypatch):
    import main
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    http = FakeHttp([html_response(FIXTURE.read_bytes())])
    monkeypatch.setattr(
        main, "collect_source",
        lambda source, state: XRPLBlogDiscovery(source, http=http, now=lambda: STAMP).collect(state),
    )
    result = main.run_pipeline(
        sources=[],
        state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[xrpl_source(max_pages=1)],
        discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
    )
    assert result.failures == []
    assert len(result.collected) == 2
    assert len(result.fresh) == 2
    assert all("direct_xrp_xrpl" in item.relevance_categories for item in result.fresh)
    assert all(item.relevance_score >= 35 for item in result.fresh)


def test_registry_rejects_external_source_url():
    payload = {"schema_version": 1, "sources": [xrpl_source(source_url="https://evil.example/")]}
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources(payload)
