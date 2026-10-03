from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.base import DiscoveryResult
from discovery.dispatch import DiscoveryRegistryError, load_discovery_sources, validate_discovery_sources
from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.white_house_actions import (
    WHITE_HOUSE_METHOD,
    WHITE_HOUSE_SOURCE_ID,
    WHITE_HOUSE_URL,
    WhiteHouseActionsDiscovery,
    _parse_page,
    validate_white_house_source,
)


FIXTURE = Path(__file__).parent / "fixtures" / "white_house_actions.html"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def wh_source(**changes):
    source = {
        "source_id": WHITE_HOUSE_SOURCE_ID,
        "name": "The White House",
        "authority_tier": 1,
        "category": "government",
        "discovery_method": WHITE_HOUSE_METHOD,
        "source_url": WHITE_HOUSE_URL,
        "enabled": True,
        "lookback_days": 90,
        "max_items": 50,
        "max_pages": 3,
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


def html_response(payload, *, headers=None, url=WHITE_HOUSE_URL):
    return HttpResponse(200, headers or {"content-type": "text/html"}, payload, url)


def test_config_is_registered_with_bounded_official_endpoint():
    source = next(item for item in load_discovery_sources()
                  if item["source_id"] == WHITE_HOUSE_SOURCE_ID)
    assert source["source_url"] == WHITE_HOUSE_URL
    assert source["lookback_days"] == 90
    assert source["max_pages"] == 3
    assert source["max_items"] == 50


@pytest.mark.parametrize("changes", [
    {"source_url": "http://www.whitehouse.gov/presidential-actions/"},
    {"source_url": "https://attacker.example/presidential-actions/"},
    {"lookback_days": 181},
    {"max_pages": 6},
    {"max_items": 101},
])
def test_unsafe_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_white_house_source(wh_source(**changes))


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = WhiteHouseActionsDiscovery(
        wh_source(enabled=False), http=http, now=lambda: STAMP,
    ).collect()
    assert result.status == "not_configured"
    assert not http.calls


def test_parser_keeps_only_official_action_links_and_extracts_type_and_date():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["native_id"] == "integrating-financial-technology-innovation-into-regulatory-frameworks"
    assert rows[0]["action_type"] == "Executive Orders"
    assert rows[0]["date"] == datetime(2026, 5, 19, 4, tzinfo=timezone.utc)
    assert rows[1]["action_type"] == "Presidential Memoranda"


def test_parser_does_not_require_article_wrapper_elements():
    html = b'''<main>
      <div class="card"><a href="/presidential-actions/inaugurating-the-era-of-super-intelligence/">
      Inaugurating The Era Of Super Intelligence</a><span>September 29, 2026</span></div>
      <section><a href="/presidential-actions/streamlining-access-to-government-services-through-america-gov/">
      Streamlining Access to Government Services Through America.gov</a><span>September 29, 2026</span></section>
    </main>'''
    rows, complete = _parse_page(html)
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["date"] == datetime(2026, 9, 29, tzinfo=timezone.utc)


def test_collect_filters_lookback_and_deduplicates():
    payload = FIXTURE.read_bytes()
    http = FakeHttp([
        html_response(payload, headers={"content-type": "text/html", "etag": '"page1"'}),
        html_response(payload, headers={"content-type": "text/html", "etag": '"page2"'}),
    ])
    result = WhiteHouseActionsDiscovery(
        wh_source(lookback_days=180, max_pages=2), http=http, now=lambda: STAMP,
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 2
    assert len(http.calls) == 2
    assert result.candidates[0].candidate_id.startswith(f"{WHITE_HOUSE_SOURCE_ID}:")
    assert result.candidates[0].provenance[0] == WHITE_HOUSE_URL


def test_old_rows_stop_pagination():
    old_html = b'''<html><body><article><a href="/presidential-actions/old-action/">Old Action</a><time datetime="2020-01-01T00:00:00Z">January 1, 2020</time></article></body></html>'''
    http = FakeHttp([html_response(old_html)])
    result = WhiteHouseActionsDiscovery(wh_source(lookback_days=90), http=http, now=lambda: STAMP).collect()
    assert result.status == "empty"
    assert len(http.calls) == 1


\ndef test_official_navigation_links_without_dates_are_ignored():\n    html = b'''<html><body>\n      <nav><a href="/presidential-actions/executive-orders/">Executive Orders</a></nav>\n      <h2><a href="/presidential-actions/example-action/">Example Action</a></h2>\n      <div>Executive Orders <time datetime="2026-09-29T00:00:00Z">September 29, 2026</time></div>\n    </body></html>'''\n    rows, complete = _parse_page(html)\n    assert complete\n    assert len(rows) == 1\n    assert rows[0]["native_id"] == "example-action"\n
def test_malformed_page_fails_closed_without_candidates():
    bad = b"<html><body><main><article><a href='/presidential-actions/bad/'>Bad</a>"
    result = WhiteHouseActionsDiscovery(
        wh_source(), http=FakeHttp([html_response(bad)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates
    assert result.errors


def test_network_failure_is_bounded_and_reported():
    result = WhiteHouseActionsDiscovery(
        wh_source(), http=FakeHttp([DiscoveryHttpError("down", kind="request_error")]),
        now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert "request_error" in result.errors[0]


def test_conditional_validators_are_sent_and_saved():
    state = {
        "sources": {
            WHITE_HOUSE_SOURCE_ID: {
                "requests": {"page-1": {"etag": '"old"', "last_modified": "Wed, 23 Sep 2026 00:00:00 GMT"}}
            }
        }
    }
    response = HttpResponse(304, {"etag": '"new"', "last-modified": "Wed, 24 Sep 2026 00:00:00 GMT"}, b"", WHITE_HOUSE_URL)
    http = FakeHttp([response])
    result = WhiteHouseActionsDiscovery(
        wh_source(max_pages=1), http=http, now=lambda: STAMP,
    ).collect(state)
    assert result.status == "empty"
    assert http.calls[0][1]["etag"] == '"old"'
    assert result.state_updates["page-1"]["etag"] == '"new"'
    assert result.state_updates["page-1"]["last_modified"] == "Wed, 24 Sep 2026 00:00:00 GMT"


def test_external_action_links_never_become_candidates():
    html = b'''<html><body><article><a href="https://evil.example/presidential-actions/fake/">Fake</a><time datetime="2026-09-22T00:00:00Z">September 22, 2026</time></article></body></html>'''
    result = WhiteHouseActionsDiscovery(
        wh_source(max_pages=1), http=FakeHttp([html_response(html)]), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates


def test_candidate_flows_through_existing_intelligence_pipeline(tmp_path, monkeypatch):
    import main
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    relevant_html = FIXTURE.read_bytes().replace(
        b"Integrating Financial Technology Innovation into Regulatory Frameworks",
        b"Ripple Payments stablecoin regulatory framework",
    )
    http = FakeHttp([html_response(relevant_html, headers={"content-type": "text/html"})])
    monkeypatch.setattr(
        main, "collect_source",
        lambda source, state: WhiteHouseActionsDiscovery(source, http=http, now=lambda: STAMP).collect(state),
    )
    result = main.run_pipeline(
        sources=[],
        state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[wh_source(max_pages=1, lookback_days=180)],
        discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
    )
    assert result.failures == []
    assert len(result.collected) == 2
    assert len(result.fresh) == 2
    assert all(item.source_quality == "primary" for item in result.fresh)
    assert all(item.relevance_categories for item in result.fresh)

    # The fixture intentionally contains one topical Ripple candidate and one
    # government/cybercrime candidate. The shared pipeline must process both,
    # while relevance scoring should only mark the topical candidate relevant.
    relevant = next(item for item in result.fresh if "Ripple Payments" in item.title)
    generic = next(item for item in result.fresh if "Transnational Cyber-Enabled Crime" in item.title)
    assert relevant.relevance_score > 0
    assert relevant.relevance_categories
    assert generic.relevance_score == 0
    assert "government" in generic.relevance_categories
