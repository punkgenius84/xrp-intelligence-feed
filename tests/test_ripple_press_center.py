from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.ripple_press_center import (
    RIPPLE_METHOD,
    RIPPLE_SOURCE_ID,
    RIPPLE_URL,
    RipplePressCenterDiscovery,
    _parse_page,
    validate_ripple_source,
)
from discovery.dispatch import DiscoveryRegistryError, load_discovery_sources, validate_discovery_sources


FIXTURE = Path(__file__).parent / "fixtures" / "ripple_press_center.html"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def ripple_source(**changes):
    source = {
        "source_id": RIPPLE_SOURCE_ID,
        "name": "Ripple",
        "authority_tier": 1,
        "category": "company",
        "discovery_method": RIPPLE_METHOD,
        "source_url": RIPPLE_URL,
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
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def html_response(payload, *, headers=None):
    return HttpResponse(200, headers or {"content-type": "text/html"}, payload, RIPPLE_URL)


def test_registered_source_is_bounded_and_official():
    source = next(item for item in load_discovery_sources() if item["source_id"] == RIPPLE_SOURCE_ID)
    assert source["source_url"] == RIPPLE_URL
    assert source["max_pages"] == 4


@pytest.mark.parametrize("changes", [
    {"source_url": "https://evil.example/press-releases/"},
    {"lookback_days": 366},
    {"max_pages": 7},
    {"max_items": 101},
])
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_ripple_source(ripple_source(**changes))


def test_parser_keeps_only_official_releases():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["slug"] == "ripple-and-jeonbuk-bank-partner-to-modernize-cross-border-payments"
    assert rows[1]["date"] == datetime(2026, 6, 24, tzinfo=timezone.utc)


def test_parser_does_not_require_article_wrapper_elements():
    html = b'''<main>
      <div class="card"><h3><a href="/ripple-press/ripple-treasury-brings-governed-ai/">Ripple Treasury Brings Governed AI</a></h3>
      <p>Sep 10, 2026</p></div>
      <section><a href="/ripple-press/ripple-prime-launches-delta-one/">Ripple Prime Launches Delta One</a>
      <span>Aug 27, 2026</span></section>
    </main>'''
    rows, complete = _parse_page(html)
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["date"] == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_collect_deduplicates_and_uses_official_page_pagination():
    payload = FIXTURE.read_bytes()
    result = RipplePressCenterDiscovery(
        ripple_source(max_pages=2, lookback_days=180),
        http=FakeHttp([html_response(payload), html_response(payload)]),
        now=lambda: STAMP,
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 2
    assert len(result.candidates) == len({item.candidate_id for item in result.candidates})


def test_old_rows_stop_pagination():
    old = b'<html><body><article><a href="/ripple-press/old/">Old</a><time datetime="2020-01-01T00:00:00Z">Jan 1, 2020</time></article></body></html>'
    http = FakeHttp([html_response(old)])
    result = RipplePressCenterDiscovery(
        ripple_source(lookback_days=30), http=http, now=lambda: STAMP,
    ).collect()
    assert result.status == "empty"
    assert len(http.calls) == 1


def test_malformed_page_fails_closed():
    bad = b"<html><body><article><a href='/ripple-press/bad/'>Bad</a>"
    result = RipplePressCenterDiscovery(
        ripple_source(max_pages=1), http=FakeHttp([html_response(bad)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates


def test_conditional_validators_are_saved():
    state = { "sources": {RIPPLE_SOURCE_ID: {"requests": {
        "page-1": {"etag": '"old"', "last_modified": "Wed, 23 Sep 2026 00:00:00 GMT"}
    }}}}
    result = RipplePressCenterDiscovery(
        ripple_source(max_pages=1),
        http=FakeHttp([HttpResponse(
            304, {"etag": '"new"', "last-modified": "Thu, 24 Sep 2026 00:00:00 GMT"},
            b"", RIPPLE_URL,
        )]),
        now=lambda: STAMP,
    ).collect(state)
    assert result.status == "empty"
    assert result.state_updates["page-1"]["etag"] == '"new"'


def test_pipeline_flows_ripple_signal_into_existing_relevance(tmp_path, monkeypatch):
    import main
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    http = FakeHttp([html_response(FIXTURE.read_bytes())])
    monkeypatch.setattr(
        main, "collect_source",
        lambda source, state: RipplePressCenterDiscovery(source, http=http, now=lambda: STAMP).collect(state),
    )
    result = main.run_pipeline(
        sources=[],
        state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[ripple_source(max_pages=1)],
        discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
    )
    assert result.failures == []
    assert len(result.collected) == 2
    assert len(result.fresh) == 2
    assert all("direct_ripple" in item.relevance_categories for item in result.fresh)
    assert all(item.relevance_score >= 35 for item in result.fresh)


def test_external_url_never_becomes_candidate():
    html = b'<html><body><article><a href="https://evil.example/ripple-press/fake/">Fake</a><time datetime="2026-09-22T00:00:00Z">Sep 22, 2026</time></article></body></html>'
    result = RipplePressCenterDiscovery(
        ripple_source(max_pages=1), http=FakeHttp([html_response(html)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates
