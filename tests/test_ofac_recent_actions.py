from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from discovery.dispatch import DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources
from discovery.http import DiscoveryHttpError
from discovery.ofac_recent_actions import OFACRecentActionsDiscovery, OFAC_URL
from intelligence.relevance import score_relevance
from models import NewsItem


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "ofac_recent_actions.html"


def source(**overrides):
    value = {
        "source_id": "ofac-recent-actions",
        "name": "U.S. Department of the Treasury Office of Foreign Assets Control",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": "ofac_recent_actions_html",
        "source_url": OFAC_URL,
        "enabled": True,
        "lookback_days": 7,
    }
    value.update(overrides)
    return value


class Response:
    status_code = 200
    headers = {"content-type": "text/html; charset=UTF-8"}
    url = OFAC_URL

    def __init__(self, content):
        self.content = content


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)


def html_page(rows=(), *, valid=True, total="1 - 0 of 0"):
    root = ('<div class="view view-recent-actions-search view-id-recent_actions_search">'
            '<div class="view-header"><p>Displaying ' + total + ' results.</p></div>'
            '<div class="view-content">') if valid else "<html><body>changed markup"
    for native_id, title, date, slug, category in rows:
        root += (f'<div class="search-result views-row"><div><a href="/recent-actions/{native_id}">'
                 f'{title}</a></div><div class="margin-top-1 font-sans-2xs line-height-sans-3 '
                 f'margin-bottom-1">{date} - '
                 f'<a href="/recent-actions/{slug}">{category}</a>'
                 '</div></div>')
    return (root + "</div></div>" if valid else "</body></html>").encode()


def state(boundary="20260920", day="2026-09-20", hint=1):
    return {"sources": {"ofac-recent-actions": {"pagination": {
        "ofac_recent_actions": {"boundary_id": boundary, "boundary_date": day,
                                 "deep_page_hint": hint, "lookback_days": 7}
    }}}}


def ids(result):
    return [item.source_native_id for item in result.candidates]


def test_registry_loads_fixed_enabled_ofac_source_and_dispatches():
    configured = load_discovery_sources()
    ofac = next(item for item in configured if item["source_id"] == "ofac-recent-actions")
    http = FakeHttp([FIXTURE.read_bytes()])
    result = collect_source(ofac, http=http, now=lambda: STAMP)
    assert result.status == "success"
    assert len(result.candidates) == 2
    assert len(http.calls) == 1


@pytest.mark.parametrize("changes", [
    {"source_url": "https://example.org/recent-actions"},
    {"discovery_method": "generic_html"},
    {"lookback_days": 0},
    {"authority_tier": 2},
])
def test_registry_rejects_invalid_ofac_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_extracts_validated_row_fields_and_native_identity_without_fetching_action_urls():
    http = FakeHttp([FIXTURE.read_bytes()])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect()
    candidate = result.candidates[0]
    assert candidate.title == "OFAC designates entities supporting a digital asset network"
    assert candidate.published_at == datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert candidate.document_type == "Sanctions List Updates"
    assert candidate.url == "https://ofac.treasury.gov/recent-actions/20260923"
    assert candidate.source_native_id == "20260923"
    assert candidate.candidate_id == "ofac-recent-actions:20260923"
    assert candidate.source_native_metadata["category_slug"] == "sanctions-list-updates"
    assert len(http.calls) == 1
    assert urlsplit(http.calls[0][0]).hostname == "ofac.treasury.gov"


def test_page_zero_is_zero_based_with_date_window_and_disabled_source_makes_no_request():
    http = FakeHttp([FIXTURE.read_bytes()])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect()
    query = parse_qs(urlsplit(http.calls[0][0]).query)
    assert query == {"ra-start-date": ["2026-09-17"], "ra-end-date": ["2026-09-23"], "page": ["0"]}
    assert result.pagination["page_fetches"] == 1
    disabled_http = FakeHttp([])
    disabled = OFACRecentActionsDiscovery(source(enabled=False), http=disabled_http,
                                          now=lambda: STAMP).collect()
    assert disabled.status == "not_configured"
    assert disabled_http.calls == []


def test_initialization_persists_oldest_page_zero_native_id():
    http = FakeHttp([FIXTURE.read_bytes()])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect()
    progress = result.pagination["ofac_recent_actions"]
    assert progress["boundary_id"] == "20260922"
    assert progress["boundary_date"] == "2026-09-22"
    assert progress["deep_page_hint"] == 1


def test_boundary_found_on_page_zero_refreshes_two_following_pages_and_advances():
    http = FakeHttp([
        html_page([("20260923", "New digital asset sanction designation", "September 23, 2026",
                    "sanctions-list-updates", "Sanctions List Updates"),
                   ("20260920", "Saved boundary", "September 20, 2026",
                    "sanctions-list-updates", "Sanctions List Updates")]),
        html_page([("20260919", "Older action number one", "September 19, 2026",
                    "miscellaneous", "Miscellaneous")]),
        html_page([("20260918", "Older action number two", "September 18, 2026",
                    "miscellaneous", "Miscellaneous")]),
    ])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state())
    assert [parse_qs(urlsplit(call[0]).query)["page"][0] for call in http.calls] == ["0", "1", "2"]
    progress = result.pagination["ofac_recent_actions"]
    assert progress["boundary_id"] == "20260918"
    assert progress["deep_page_hint"] == 3
    assert result.pagination["page_fetches"] == 3


def test_boundary_shift_recovery_is_sequential_and_bounded_to_two_probes():
    http = FakeHttp([
        html_page([("20260923", "New action", "September 23, 2026", "miscellaneous", "Miscellaneous")]),
        html_page([("20260921", "Intervening", "September 21, 2026", "miscellaneous", "Miscellaneous")]),
        html_page([("20260920", "Saved boundary", "September 20, 2026", "miscellaneous", "Miscellaneous"),
                   ("20260919", "Older row", "September 19, 2026", "miscellaneous", "Miscellaneous")]),
    ])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP, ).collect(state(hint=1))
    pages = [parse_qs(urlsplit(call[0]).query)["page"][0] for call in http.calls]
    assert pages == ["0", "1", "2"]
    assert result.pagination["page_fetches"] == 3
    assert result.pagination["ofac_recent_actions"]["boundary_id"] == "20260919"


def test_boundary_not_found_advances_only_hint_and_keeps_identity():
    http = FakeHttp([
        html_page([("20260923", "New", "September 23, 2026", "miscellaneous", "Miscellaneous")]),
        html_page([("20260921", "Still newer", "September 21, 2026", "miscellaneous", "Miscellaneous")]),
        html_page([("20260919", "Still newer", "September 19, 2026", "miscellaneous", "Miscellaneous")]),
    ])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state(hint=4))
    progress = result.pagination["ofac_recent_actions"]
    assert progress["boundary_id"] == "20260920"
    assert progress["deep_page_hint"] == 6


def test_boundary_outside_window_pauses_frontier_without_using_total_count():
    http = FakeHttp([FIXTURE.read_bytes()])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(
        state("20260901", "2026-09-01", 28))
    assert len(http.calls) == 1
    assert result.pagination["ofac_recent_actions"]["frontier_status"] == "boundary_expired"
    assert result.pagination["ofac_recent_actions"]["boundary_id"] == "20260901"


def test_valid_empty_page_is_not_malformed_or_authoritative_expiration():
    http = FakeHttp([html_page(), html_page(), html_page()])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state(hint=7))
    assert result.status == "success"
    assert result.pagination["ofac_recent_actions"]["boundary_id"] == "20260920"
    assert result.pagination["ofac_recent_actions"]["deep_page_hint"] == 7


def test_malformed_page_retains_parsed_candidates_but_does_not_advance_state_or_hint():
    mixed = html_page([("20260923", "Valid action", "September 23, 2026", "miscellaneous", "Miscellaneous"),
                       ("20260922", "Bad row", "not a date", "miscellaneous", "Miscellaneous")])
    http = FakeHttp([FIXTURE.read_bytes(), mixed])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state())
    assert result.status == "partial"
    assert "20260923" in ids(result)
    assert "ofac_recent_actions" not in result.pagination
    assert result.pagination["page_fetches"] == 2


def test_http_failure_after_page_zero_returns_valid_candidates_without_frontier_progress():
    http = FakeHttp([FIXTURE.read_bytes(), DiscoveryHttpError("timeout", kind="timeout")])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state())
    assert result.status == "partial"
    assert len(result.candidates) == 2
    assert "ofac_recent_actions" not in result.pagination
    assert result.pagination["page_fetches"] == 2


def test_malformed_existing_pagination_container_does_not_reseed_boundary():
    original = {"sources": {"ofac-recent-actions": {"pagination": ["corrupt"]}}}
    http = FakeHttp([])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(original)
    assert result.status == "failed"
    assert "Invalid OFAC pagination container" in result.errors[0]
    assert http.calls == []
    assert result.pagination == {}
    assert original["sources"]["ofac-recent-actions"]["pagination"] == ["corrupt"]


def test_non_object_discovery_state_does_not_reseed_boundary():
    http = FakeHttp([])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect([])
    assert result.status == "failed"
    assert "Invalid discovery state" in result.errors[0]
    assert http.calls == []


def test_malformed_ofac_pagination_entry_does_not_reseed_boundary():
    original = {"sources": {"ofac-recent-actions": {"pagination": {
        "ofac_recent_actions": "corrupt"
    }}}}
    http = FakeHttp([])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(original)
    assert result.status == "failed"
    assert "Invalid OFAC pagination state" in result.errors[0]
    assert http.calls == []
    assert original["sources"]["ofac-recent-actions"]["pagination"]["ofac_recent_actions"] == "corrupt"


def test_incomplete_ofac_pagination_dict_is_not_treated_as_first_run():
    original = {"sources": {"ofac-recent-actions": {"pagination": {
        "ofac_recent_actions": {}
    }}}}
    http = FakeHttp([])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(original)
    assert result.status == "failed"
    assert "Incomplete OFAC pagination state" in result.errors[0]
    assert http.calls == []
    assert original["sources"]["ofac-recent-actions"]["pagination"]["ofac_recent_actions"] == {}


def test_title_date_does_not_override_publication_date_field():
    fixture = html_page([("20260923", "OFAC announces changes effective September 1, 2026",
                          "September 23, 2026", "miscellaneous", "Miscellaneous")])
    result = OFACRecentActionsDiscovery(source(), http=FakeHttp([fixture]),
                                        now=lambda: STAMP).collect()
    assert result.status == "success"
    assert result.candidates[0].published_at == datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert "September 1, 2026" in result.candidates[0].title


def test_failure_after_boundary_rediscovery_retains_candidates_and_old_frontier():
    http = FakeHttp([
        html_page([("20260923", "New action", "September 23, 2026", "miscellaneous", "Miscellaneous"),
                   ("20260920", "Saved boundary", "September 20, 2026", "miscellaneous", "Miscellaneous")]),
        DiscoveryHttpError("timeout", kind="timeout"),
    ])
    old_state = state()
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(old_state)
    assert result.status == "partial"
    assert "20260923" in ids(result)
    assert result.pagination == {"page_fetches": 2}
    assert old_state["sources"]["ofac-recent-actions"]["pagination"]["ofac_recent_actions"]["boundary_id"] == "20260920"


def test_malformed_page_after_boundary_rediscovery_retains_old_frontier():
    malformed = html_page([("20260919", "Valid row", "September 19, 2026", "miscellaneous", "Miscellaneous"),
                           ("20260918", "Malformed row", "bad date", "miscellaneous", "Miscellaneous")])
    http = FakeHttp([
        html_page([("20260923", "New action", "September 23, 2026", "miscellaneous", "Miscellaneous"),
                   ("20260920", "Saved boundary", "September 20, 2026", "miscellaneous", "Miscellaneous")]),
        malformed,
    ])
    old_state = state()
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(old_state)
    assert result.status == "partial"
    assert "20260919" in ids(result)
    assert result.pagination == {"page_fetches": 2}
    assert old_state["sources"]["ofac-recent-actions"]["pagination"]["ofac_recent_actions"]["boundary_id"] == "20260920"


def test_malformed_documented_structure_fails_but_valid_empty_listing_succeeds():
    bad = FakeHttp([html_page(valid=False)])
    failed = OFACRecentActionsDiscovery(source(), http=bad, now=lambda: STAMP).collect()
    assert failed.status == "failed"
    assert "Recent Actions listing structure" in failed.errors[0]
    empty = OFACRecentActionsDiscovery(source(), http=FakeHttp([html_page()]),
                                       now=lambda: STAMP).collect()
    assert empty.status == "success"
    assert empty.candidates == []


def test_external_and_noncanonical_action_links_are_rejected():
    bad = html_page([("20260923", "External", "September 23, 2026", "miscellaneous", "Miscellaneous")])
    bad = bad.replace(b"/recent-actions/20260923", b"https://evil.example/recent-actions/20260923")
    result = OFACRecentActionsDiscovery(source(), http=FakeHttp([bad]), now=lambda: STAMP).collect()
    assert result.status == "failed"
    assert result.candidates == []


def test_duplicate_native_ids_across_overlapping_pages_yield_one_candidate():
    duplicate = ("20260919", "Same action", "September 19, 2026", "miscellaneous", "Miscellaneous")
    http = FakeHttp([
        html_page([("20260923", "New action", "September 23, 2026", "miscellaneous", "Miscellaneous"),
                   ("20260920", "Saved boundary", "September 20, 2026", "miscellaneous", "Miscellaneous")]),
        html_page([duplicate]), html_page([duplicate]),
    ])
    result = OFACRecentActionsDiscovery(source(), http=http, now=lambda: STAMP).collect(state())
    assert ids(result).count("20260919") == 1


def test_ofac_authority_alone_does_not_make_generic_story_relevant(tmp_path):
    generic = NewsItem("OFAC announces unrelated action", "https://ofac.treasury.gov/recent-actions/20260923",
                       "OFAC", published_at=STAMP, summary="Sanctions matter.", source_id="ofac-recent-actions",
                       authority_tier=1, category="regulatory", detected_entities=["OFAC"],
                       source_quality="primary")
    direct = NewsItem("OFAC designates XRP-related entities", generic.url, "OFAC", published_at=STAMP,
                      summary="The action concerns XRP digital asset activity.",
                      source_id="ofac-recent-actions", authority_tier=1, category="regulatory",
                      detected_entities=["OFAC", "XRP"], source_quality="primary")
    score_relevance(generic)
    score_relevance(direct)
    assert "relevant_regulatory_digital_asset_payments" not in generic.relevance_categories
    assert "relevant_regulatory_digital_asset_payments" in direct.relevance_categories
    assert direct.relevance_score > generic.relevance_score
