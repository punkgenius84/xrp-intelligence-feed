from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from discovery.dispatch import DiscoveryRegistryError, load_discovery_sources, validate_discovery_sources
from discovery.federal_register import (FEDERAL_REGISTER_API_URL, FEDERAL_REGISTER_API_ROOT,
                                        FederalRegisterDiscovery, validate_federal_register_source)
from discovery.http import DiscoveryHttpError, HttpResponse
from storage.database import JsonState
from storage.discovery_state import JsonDiscoveryState


FIXTURE = Path(__file__).parent / "fixtures" / "federal_register_documents.json"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def fr_source(**changes):
    source = {
        "source_id": "federal-register-api",
        "name": "Federal Register",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": "federal_register_api",
        "source_url": FEDERAL_REGISTER_API_ROOT,
        "enabled": True,
        "search_terms": ["digital assets"],
        "agencies": ["federal-reserve-system"],
        "document_types": ["RULE", "PRORULE", "NOTICE"],
        "lookback_days": 7,
        "page_size": 10,
        "max_pages": 2,
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


def api_response(payload):
    return HttpResponse(200, {"content-type": "application/json"},
                        json.dumps(payload).encode("utf-8"), FEDERAL_REGISTER_API_URL)


def fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_registered_config_is_small_bounded_and_official():
    sources = load_discovery_sources()
    source = next(item for item in sources if item["source_id"] == "federal-register-api")
    assert source["search_terms"] == ["digital assets", "stablecoin"]
    assert source["agencies"] == []
    assert source["page_size"] == 10
    assert source["max_pages"] == 2
    assert source["source_url"] == FEDERAL_REGISTER_API_ROOT


@pytest.mark.parametrize("changes", [
    {"source_url": "http://www.federalregister.gov/api/v1/"},
    {"source_url": "https://attacker.example/api/"},
    {"page_size": 1000},
    {"max_pages": 100},
    {"search_terms": [""]},
    {"agencies": ["Federal Reserve System"]},
    {"document_types": ["UNKNOWN"]},
])
def test_invalid_or_unsafe_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_federal_register_source(fr_source(**changes))


def test_empty_or_disabled_configuration_makes_zero_requests():
    for source in (fr_source(enabled=False), fr_source(search_terms=[])):
        http = FakeHttp([])
        result = FederalRegisterDiscovery(source, http=http, now=lambda: STAMP).collect()
        assert result.status == "not_configured"
        assert result.candidates == []
        assert not http.calls


def test_valid_documents_create_candidates_and_malformed_record_is_skipped():
    payload = fixture_payload()
    http = FakeHttp([api_response(payload)])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 2
    relevant = next(item for item in result.candidates if item.source_native_id == "2026-12345")
    irrelevant = next(item for item in result.candidates if item.source_native_id == "2026-12346")
    assert relevant.source_native_id == "2026-12345"
    assert relevant.candidate_id == "federal-register-api:2026-12345"
    assert relevant.title == "Federal Reserve Proposes Requirements for Stablecoin Issuers"
    assert relevant.published_at == datetime(2026, 9, 22, tzinfo=timezone.utc)
    assert relevant.document_type == "Proposed Rule"
    assert relevant.source_native_metadata["agencies"] == ["Federal Reserve System"]
    assert "payment stablecoin" in relevant.summary
    assert "must not replace" not in relevant.summary
    assert relevant.url.startswith("https://www.federalregister.gov/documents/")
    assert relevant.source_native_metadata["pdf_url"].startswith("https://www.govinfo.gov/")
    assert relevant.source_url.startswith(FEDERAL_REGISTER_API_URL + "?")
    assert relevant.provenance == [FEDERAL_REGISTER_API_ROOT, relevant.url]
    assert relevant.source_id == "federal-register-api"
    assert relevant.authority_tier == 1
    assert irrelevant.source_native_id == "2026-12346"
    assert "Operating hours for a visitor center are listed here." in irrelevant.summary
    assert any("document_number" in error for error in result.errors)
    query = parse_qs(urlsplit(http.calls[0][0]).query)
    assert query["conditions[term]"] == ["digital assets"]
    assert query["conditions[publication_date][gte]"] == ["2026-09-17"]
    assert query["conditions[agencies][]"] == ["federal-reserve-system"]
    assert query["conditions[type][]"] == ["RULE", "PRORULE", "NOTICE"]
    assert query["per_page"] == ["10"]
    assert query["order"] == ["newest"]
    assert "excerpts" in query["fields[]"]


def test_abstract_present_takes_precedence_over_excerpt_fallback():
    payload = fixture_payload()
    result = FederalRegisterDiscovery(
        fr_source(), http=FakeHttp([api_response(payload)]), now=lambda: STAMP,
    ).collect()
    candidate = next(item for item in result.candidates if item.source_native_id == "2026-12345")
    assert candidate.summary.startswith(
        "The proposal addresses payment stablecoin reserve and custody requirements."
    )
    assert "must not replace" not in candidate.summary


def test_native_candidate_id_and_duplicate_document_handling_are_deterministic():
    payload = fixture_payload()
    duplicate = dict(payload["results"][0])
    payload["results"].insert(1, duplicate)
    ids = []
    for _ in range(2):
        result = FederalRegisterDiscovery(fr_source(), http=FakeHttp([api_response(payload)]),
                                          now=lambda: STAMP).collect()
        ids.append([candidate.candidate_id for candidate in result.candidates])
        assert sum(candidate.source_native_id == "2026-12345" for candidate in result.candidates) == 1
    assert ids[0] == ids[1]


def test_malformed_api_response_and_missing_identity_are_reported():
    bad_json_http = FakeHttp([HttpResponse(200, {"content-type": "application/json"}, b"{", "")])
    bad_json = FederalRegisterDiscovery(fr_source(), http=bad_json_http,
                                        now=lambda: STAMP).collect()
    assert bad_json.status == "failed"
    assert not bad_json.candidates
    missing_identity = FederalRegisterDiscovery(
        fr_source(), http=FakeHttp([api_response({"total_pages": 1,
                                                  "results": [{"title": "No document number"}]})]),
        now=lambda: STAMP,
    ).collect()
    assert missing_identity.status == "failed"
    assert "document_number" in missing_identity.errors[0]


def test_pagination_is_bounded_even_when_api_claims_many_pages():
    source = fr_source(search_terms=["digital assets", "stablecoin"])
    state = fr_state("2026-00101", 3)
    state["sources"]["federal-register-api"]["pagination"]["federal_register_terms"]["stablecoin"] = {
        "boundary_document_number": "2026-00201",
        "deep_page_hint": 3,
        "lookback_days": 7,
        "frontier_status": "active",
    }
    http = FakeHttp([api_response(fr_payload([], 5000)) for _ in range(8)])
    result = FederalRegisterDiscovery(source, http=http, now=lambda: STAMP).collect(state)
    assert len(http.calls) == 8
    assert result.pagination["requests_made"] == 8
    pages = [parse_qs(urlsplit(url).query)["page"][0] for url, _ in http.calls]
    assert pages == ["1", "2", "3", "4", "1", "2", "3", "4"]
    assert all(urlsplit(url).scheme == "https" and urlsplit(url).hostname == "www.federalregister.gov"
               for url, _ in http.calls)


def fr_document(number):
    return {
        "document_number": number,
        "title": f"Digital asset notice {number}",
        "publication_date": "2026-09-22",
        "type": "NOTICE",
        "abstract": "Digital asset policy notice.",
        "html_url": f"https://www.federalregister.gov/documents/2026/09/22/{number}/notice",
    }


def fr_payload(numbers, total_pages=10):
    return {"total_pages": total_pages, "results": [fr_document(number) for number in numbers]}


def fr_state(boundary, hint, *, status="active", lookback=7):
    return {
        "sources": {
            "federal-register-api": {
                "pagination": {
                    "federal_register_terms": {
                        "digital assets": {
                            "boundary_document_number": boundary,
                            "deep_page_hint": hint,
                            "lookback_days": lookback,
                            "frontier_status": status,
                        }
                    }
                }
            }
        }
    }


def page_numbers(http):
    return [int(parse_qs(urlsplit(url).query)["page"][0]) for url, _ in http.calls]


def test_boundary_on_expected_page_advances_only_after_boundary_is_found():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 8)),
        api_response(fr_payload(["2026-00002"], 8)),
        api_response(fr_payload(["2026-00100", "2026-00101"], 8)),
        api_response(fr_payload(["2026-00102"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 3),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2, 3, 4]
    assert progress["boundary_document_number"] == "2026-00102"
    assert progress["deep_page_hint"] == 5


def test_initial_frontier_uses_oldest_available_page_and_next_page_hint():
    http = FakeHttp([api_response(fr_payload(["2026-00100", "2026-00101"], 1))])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect()
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1]
    assert progress["boundary_document_number"] == "2026-00101"
    assert progress["deep_page_hint"] == 2


def test_boundary_on_page_one_advances_through_successful_shallow_and_deep_pages():
    http = FakeHttp([
        api_response(fr_payload(["2026-00100", "2026-00101"], 8)),
        api_response(fr_payload(["2026-00102"], 8)),
        api_response(fr_payload(["2026-00103"], 8)),
        api_response(fr_payload(["2026-00104"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 12, status="boundary_expired"),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2, 3, 4]
    assert {item.source_native_id for item in result.candidates} >= {
        "2026-00101", "2026-00102", "2026-00103", "2026-00104",
    }
    assert progress["boundary_document_number"] == "2026-00104"
    assert progress["deep_page_hint"] == 5


def test_boundary_shifted_deeper_advances_hint_without_advancing_boundary():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 9)),
        api_response(fr_payload(["2026-00002"], 9)),
        api_response(fr_payload(["2026-00101"], 9)),
        api_response(fr_payload(["2026-00102"], 9)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 3),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert progress["boundary_document_number"] == "2026-00100"
    assert progress["deep_page_hint"] == 5
    assert page_numbers(http) == [1, 2, 3, 4]


def test_boundary_is_eventually_rediscovered_after_hint_recovery():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 9)),
        api_response(fr_payload(["2026-00002"], 9)),
        api_response(fr_payload(["2026-00100", "2026-00103"], 9)),
        api_response(fr_payload(["2026-00104"], 9)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 5),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2, 5, 6]
    assert progress["boundary_document_number"] == "2026-00104"
    assert progress["deep_page_hint"] == 7


def test_api_total_pages_below_hint_marks_boundary_expired_without_rebasing():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 4)),
        api_response(fr_payload(["2026-00002"], 4)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 5),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2]
    assert progress["boundary_document_number"] == "2026-00100"
    assert progress["deep_page_hint"] == 5
    assert progress["frontier_status"] == "boundary_expired"


def test_invalid_persisted_hint_fails_closed_without_reseeding_boundary():
    http = FakeHttp([])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 0),
    )
    assert result.status == "failed"
    assert not http.calls
    assert "federal_register_terms" not in result.pagination
    assert "refusing to reset its boundary" in result.errors[0]


def test_expired_frontier_continues_shallow_refresh_without_deep_requests():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 8)),
        api_response(fr_payload(["2026-00002"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 5, status="boundary_expired"),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2]
    assert progress["boundary_document_number"] == "2026-00100"
    assert progress["frontier_status"] == "boundary_expired"


def test_expired_boundary_rediscovered_shallow_restarts_search_after_shallow_pages():
    http = FakeHttp([
        api_response(fr_payload(["2026-00100", "2026-00101"], 8)),
        api_response(fr_payload(["2026-00102"], 8)),
        api_response(fr_payload(["2026-00103"], 8)),
        api_response(fr_payload(["2026-00104"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 12, status="boundary_expired"),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert page_numbers(http) == [1, 2, 3, 4]
    assert progress["frontier_status"] == "active"
    assert progress["boundary_document_number"] == "2026-00104"


def test_shallow_boundary_match_does_not_advance_without_successful_deep_page():
    http = FakeHttp([
        api_response(fr_payload(["2026-00100", "2026-00101"], 2)),
        api_response(fr_payload(["2026-00102"], 2)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 12, status="boundary_expired"),
    )
    progress = result.pagination["federal_register_terms"]["digital assets"]
    assert progress["boundary_document_number"] == "2026-00100"
    assert progress["deep_page_hint"] == 3
    assert page_numbers(http) == [1, 2]


def test_deep_request_failure_does_not_return_pagination_progress():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 8)),
        api_response(fr_payload(["2026-00002"], 8)),
        api_response(fr_payload(["2026-00101"], 8)),
        DiscoveryHttpError("network failed", kind="request"),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 3),
    )
    assert result.status == "partial"
    assert "federal_register_terms" not in result.pagination
    assert page_numbers(http) == [1, 2, 3, 4]


def test_malformed_deep_response_keeps_candidates_but_does_not_advance_progress():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 8)),
        api_response(fr_payload(["2026-00002"], 8)),
        api_response(fr_payload(["2026-00100", "2026-00101", "bad-row"], 8)),
        api_response(fr_payload(["2026-00102"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 3),
    )
    assert result.status == "partial"
    assert any(item.source_native_id == "2026-00101" for item in result.candidates)
    assert "federal_register_terms" not in result.pagination


def test_duplicate_documents_on_overlapping_pages_keep_one_candidate():
    http = FakeHttp([
        api_response(fr_payload(["2026-00001"], 8)),
        api_response(fr_payload(["2026-00002"], 8)),
        api_response(fr_payload(["2026-00100", "2026-00101"], 8)),
        api_response(fr_payload(["2026-00101", "2026-00102"], 8)),
    ])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect(
        fr_state("2026-00100", 3),
    )
    assert sum(item.source_native_id == "2026-00101" for item in result.candidates) == 1


def test_external_result_urls_are_never_fetched_or_used_as_candidate_urls():
    payload = fixture_payload()
    payload["results"][0]["pdf_url"] = "https://attacker.example/payload.pdf"
    payload["results"][1]["html_url"] = "https://attacker.example/redirect-me"
    http = FakeHttp([api_response(payload)])
    result = FederalRegisterDiscovery(fr_source(), http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].source_native_metadata["pdf_url"] == ""
    assert result.candidates[0].url.startswith("https://www.federalregister.gov/")
    assert len(http.calls) == 1
    assert all(urlsplit(url).hostname == "www.federalregister.gov" for url, _ in http.calls)


def test_registry_dispatch_rejects_untrusted_source_endpoint():
    payload = {"schema_version": 1, "sources": [fr_source(source_url="https://example.test/")]}
    with pytest.raises(DiscoveryRegistryError, match="fixed Federal Register API root"):
        validate_discovery_sources(payload)


def test_federal_register_candidate_flows_through_existing_intelligence_pipeline(
        tmp_path, monkeypatch):
    import main

    payload = fixture_payload()
    payload["results"] = payload["results"][:1]
    payload["results"][0]["title"] = "Federal Reserve proposed rule for Ripple Payments stablecoin activity"
    payload["results"][0]["abstract"] = None
    payload["results"][0]["excerpts"] = (
        "The proposal covers Ripple Payments stablecoin and digital asset payment settlement."
    )
    http = FakeHttp([api_response(payload)])
    monkeypatch.setattr(
        main, "collect_source",
        lambda source, state: FederalRegisterDiscovery(source, http=http, now=lambda: STAMP).collect(state),
    )

    collected, fresh, failures = main.run_pipeline(
        sources=[], state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[fr_source(agencies=[])],
        discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
    )

    assert failures == []
    assert collected == fresh
    candidate = fresh[0]
    assert "Ripple Payments stablecoin" in candidate.summary
    assert "Federal Reserve" in candidate.detected_entities
    assert "Ripple" in candidate.detected_entities
    assert candidate.source_quality == "primary"
    assert candidate.relevance_score > 0
    assert candidate.score_reasons
    assert candidate.relevance_categories
