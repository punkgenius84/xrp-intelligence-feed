from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryRegistryError, load_discovery_sources,
                                validate_discovery_sources)
from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.sec_edgar import (SEC_ARCHIVES_ROOT, SECEdgarDiscovery,
                                 SEC_SUBMISSIONS_ROOT, _extract_filing_context,
                                 _filing_index_url, _filing_url, validate_sec_source)
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality


FIXTURE = Path(__file__).parent / "fixtures" / "sec_submissions.json"
STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def sec_source(**changes):
    source = {
        "source_id": "sec-edgar-submissions",
        "name": "U.S. Securities and Exchange Commission",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": "sec_submissions",
        "source_url": SEC_SUBMISSIONS_ROOT,
        "enabled": True,
        "issuer_ciks": ["9999999999"],
        "filing_forms": ["8-K"],
        "contact_email_env": "SEC_CONTACT_EMAIL",
        "application_name": "XRPIntelligenceFeed/0.3",
        "max_issuers_per_run": 5,
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


def response(payload=None, *, status=200, headers=None):
    content = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return HttpResponse(status, headers or {"content-type": "application/json"}, content,
                        SEC_SUBMISSIONS_ROOT)


def test_config_is_small_explicit_and_has_no_invented_issuer_ids():
    configured = load_discovery_sources()
    sec = next(source for source in configured if source["source_id"] == "sec-edgar-submissions")
    # CIKs are cross-checked against official SEC EDGAR filing pages.
    # Keep the collector disabled until runner responses and relevance are validated.
    assert sec["issuer_ciks"] == ["1679788", "1876042", "1633917", "1403161"]
    assert sec["enabled"] is False
    assert sec["detail_enrichment_limit"] == 0
    assert sec["lookback_days"] == 30
    assert sec["filing_forms"] == ["8-K", "10-K", "10-Q", "S-1", "S-3"]
    assert {source["source_id"] for source in configured} == {
        "sec-edgar-submissions",
        "federal-register-api",
        "ofac-recent-actions",
        "cftc-press-releases",
        "fincen-press-releases",
        "treasury-press-releases",
        "fdic-press-releases",
        "occ-issuances",
        "federal-reserve-board-rss",
        "bis-media-releases",
        "sec-press-releases",
        "white-house-presidential-actions",
        "doj-news-api",
        "ripple-press-center",
        "xrpl-community-blog",
        "citi-press-releases",
        "circle-pressroom",
        "mastercard-press-releases",
        "coinbase-blog",
        "coinbase-investor-news",
        "swift-press-releases",
        "visa-press-releases",
        "dbs-newsroom",
        "jpmorgan-payments-newsroom",
        "bny-newsroom",
        "imf-blogs",
        "imf-global-financial-stability-report",
        "imf-media-center-news",
        "ecb-press-releases",
        "bank-of-canada-press-releases",
        "canada-gazette-part-i",
        "bnm-press-releases",
        "bot-news",
        "bank-of-england-news",
        "mas-google-news-discovery",
        "bot-google-news-english-discovery",
        "bot-google-news-thai-stablecoin-hearing",
        "coinbase-google-news-discovery",
        "swift-google-news-discovery",
        "imf-google-news-discovery",
        "coindesk-google-news-discovery",
        "coindesk-xrpl-technical-google-news-discovery",
        "cointelegraph-google-news-discovery",
        "theblock-google-news-discovery",
        "blockworks-google-news-discovery",
        "dlnews-google-news-discovery",
        "sgpc-media-releases-api",
    }


def test_filing_index_context_extracts_items_and_bounded_exhibit_rows():
    html = b"""<html><body>
    <div class="infoHead">Items</div><div class="info">1.01 Entry into a Material Definitive Agreement; 8.01 Other Events</div>
    <table><tr><td>EX-99.1</td><td class="desc">Press release announcing stablecoin settlement and cross-border payments</td></tr>
    <tr><td>EX-10.1</td><td class="desc">Purchase agreement</td></tr></table>
    </body></html>"""
    context = _extract_filing_context(html)
    assert context["filing_items"] == "1.01 Entry into a Material Definitive Agreement; 8.01 Other Events"
    assert len(context["exhibit_descriptions"]) == 2
    assert "stablecoin settlement" in context["exhibit_descriptions"][0]
    assert _filing_index_url("1876042", "0001876042-26-000267").endswith("-index.htm")
    # Accession prefix is the submitting account CIK; it may be a filing agent.
    assert _filing_index_url("1876042", "0001193125-26-000267") == (
        SEC_ARCHIVES_ROOT + "1876042/000119312526000267/0001193125-26-000267-index.htm"
    )
    assert _filing_url("1876042", "0001193125-26-000267", "report.htm") == (
        SEC_ARCHIVES_ROOT + "1876042/000119312526000267/report.htm"
    )
    with pytest.raises(ValueError, match="no recognizable Items or exhibit context"):
        _extract_filing_context(b"<html><body>JavaScript shell</body></html>")


def test_sec_index_enrichment_is_opt_in_and_bounded_to_two_pages_per_run():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    recent["accessionNumber"].extend(["9999999999-26-000003", "9999999999-26-000004"])
    recent["form"].extend(["8-K", "8-K"])
    recent["filingDate"].extend(["2026-09-20", "2026-09-19"])
    recent["primaryDocument"].extend(["second8k.htm", "third8k.htm"])
    recent["primaryDocDescription"].extend(["Second current report", "Third current report"])
    index_html = b"""<html><div class="infoHead">Items</div><div class="info">1.01 Material Agreement</div>
    <table><tr><td>EX-99.1</td><td class="desc">Stablecoin settlement partnership announcement</td></tr></table></html>"""
    index_url = _filing_index_url("9999999999", "9999999999-26-000001")
    second_index_url = _filing_index_url("9999999999", "9999999999-26-000003")
    http = FakeHttp([
        response(payload),
        HttpResponse(200, {"content-type": "text/html"}, index_html, index_url),
        HttpResponse(200, {"content-type": "text/html"}, index_html, second_index_url),
    ])
    result = SECEdgarDiscovery(
        sec_source(detail_enrichment_limit=2), http=http, now=lambda: STAMP,
        request_interval=0, environ={},
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 3
    enriched = [item for item in result.candidates if "filing_index_url" in item.source_native_metadata]
    assert len(enriched) == 2
    assert {item.source_native_metadata["filing_index_url"] for item in enriched} == {
        index_url, second_index_url
    }
    assert all(item.source_native_metadata["filing_items"] == "1.01 Material Agreement" for item in enriched)
    assert all("Stablecoin settlement partnership announcement" in item.summary for item in enriched)
    assert len(http.calls) == 3



def test_unrecognized_filing_index_keeps_candidate_but_marks_source_partial():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    index_url = _filing_index_url("9999999999", "9999999999-26-000001")
    http = FakeHttp([
        response(payload),
        HttpResponse(200, {"content-type": "text/html"},
                     b"<html><body>JavaScript shell</body></html>", index_url),
    ])
    result = SECEdgarDiscovery(
        sec_source(detail_enrichment_limit=1), http=http, now=lambda: STAMP,
        request_interval=0, environ={},
    ).collect()
    assert len(result.candidates) == 1
    assert result.status == "partial"
    assert result.candidates[0].source_status == "partial"
    assert "filing-index enrichment failed" in result.candidates[0].source_error
    assert "no recognizable Items or exhibit context" in result.candidates[0].source_error
    assert result.state_updates == {}


def test_registry_rejects_duplicate_sources_and_invalid_ciks():
    with pytest.raises(ValueError, match="detail_enrichment_limit"):
        validate_sec_source(sec_source(detail_enrichment_limit=6))
    with pytest.raises(ValueError, match="lookback_days"):
        validate_sec_source(sec_source(lookback_days=366))
    with pytest.raises(DiscoveryRegistryError, match="duplicate discovery source_id"):
        validate_discovery_sources({"schema_version": 1, "sources": [sec_source(), sec_source()]})
    with pytest.raises(ValueError, match="issuer_ciks"):
        validate_sec_source(sec_source(issuer_ciks=["not-a-cik"]))


def test_sec_submissions_discards_stale_rows_without_marking_source_partial():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    recent["filingDate"][0] = "2026-07-01"
    recent["accessionNumber"].insert(1, "0001193125-26-000005")
    recent["form"].insert(1, "8-K")
    recent["filingDate"].insert(1, "2026-09-23")
    recent["primaryDocument"].insert(1, "recent-agent-filed.htm")
    recent["primaryDocDescription"].insert(1, "Recent current report")
    http = FakeHttp([response(payload, headers={"content-type": "application/json", "etag": '"recent-only"'})])

    result = SECEdgarDiscovery(
        sec_source(lookback_days=30), http=http, now=lambda: STAMP,
        request_interval=0, environ={},
    ).collect()

    assert result.status == "success"
    assert len(result.candidates) == 1
    assert result.candidates[0].source_native_metadata["accession_number"] == "0001193125-26-000005"
    assert result.state_updates["9999999999"]["etag"] == '"recent-only"'


def test_no_requests_until_issuer_and_form_allowlists_are_configured():
    http = FakeHttp([])
    result = SECEdgarDiscovery(sec_source(issuer_ciks=[]), http=http, now=lambda: STAMP).collect()
    assert result.status == "not_configured"
    assert not http.calls


def test_valid_submissions_response_filters_forms_and_duplicate_accessions():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    http = FakeHttp([response(payload, headers={"content-type": "application/json", "etag": '"e1"',
                                               "last-modified": "Wed, 23 Sep 2026 12:00:00 GMT"})])
    adapter = SECEdgarDiscovery(sec_source(), http=http, now=lambda: STAMP, request_interval=0,
                                environ={})
    result = adapter.collect()
    assert result.status == "success"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.candidate_id == "sec-edgar-submissions:CIK9999999999:9999999999-26-000001"
    assert candidate.url == SEC_ARCHIVES_ROOT + "9999999999/999999999926000001/example8k.htm"
    assert candidate.source_native_metadata == {
        "issuer": "Example Issuer Inc.", "cik": "9999999999",
        "accession_number": "9999999999-26-000001", "form": "8-K",
        "filing_date": "2026-09-22", "primary_document": "example8k.htm",
    }
    assert candidate.authority_tier == 1
    assert candidate.discovery_method == "sec_submissions"
    assert candidate.source_id == "sec-edgar-submissions"
    assert candidate.source_url.endswith("CIK9999999999.json")
    assert candidate.primary_url == candidate.url
    assert result.state_updates["9999999999"] == {
        "etag": '"e1"', "last_modified": "Wed, 23 Sep 2026 12:00:00 GMT"
    }


def test_filtering_issuer_forms_filing_identity_and_candidate_id():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    http = FakeHttp([response(payload)])
    result = SECEdgarDiscovery(sec_source(filing_forms=["10-K"]), http=http,
                               now=lambda: STAMP, request_interval=0).collect()
    assert [candidate.document_type for candidate in result.candidates] == ["10-K"]
    assert result.candidates[0].candidate_id.endswith("9999999999-26-000002")
    assert _filing_url("9999999999", "9999999999-26-000001", "file 8k.htm").endswith("file%208k.htm")
    with pytest.raises(ValueError, match="filename"):
        _filing_url("9999999999", "9999999999-26-000001", "../other.htm")


def test_filing_agent_accession_is_kept_under_registrant_archive_path():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    recent["accessionNumber"].insert(0, "0001193125-26-000009")
    recent["form"].insert(0, "8-K")
    recent["filingDate"].insert(0, "2026-09-23")
    recent["primaryDocument"].insert(0, "agent-submitted.htm")
    recent["primaryDocDescription"].insert(0, "Current report submitted by filing agent")

    result = SECEdgarDiscovery(sec_source(), http=FakeHttp([response(payload)]),
                               now=lambda: STAMP, request_interval=0).collect()

    assert result.status == "success"
    assert len(result.candidates) == 2
    agent_candidate = next(
        item for item in result.candidates
        if item.source_native_metadata["accession_number"] == "0001193125-26-000009"
    )
    assert agent_candidate.url == (
        SEC_ARCHIVES_ROOT + "9999999999/000119312526000009/agent-submitted.htm"
    )
    assert agent_candidate.candidate_id.endswith("0001193125-26-000009")
    assert agent_candidate.source_native_metadata["cik"] == "9999999999"
    assert _filing_index_url("9999999999", "0001193125-26-000009") == (
        SEC_ARCHIVES_ROOT + "9999999999/000119312526000009/0001193125-26-000009-index.htm"
    )


def test_filing_agent_accession_does_not_mark_response_partial_or_drop_validators():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    recent["accessionNumber"].insert(0, "0001193125-26-000009")
    recent["form"].insert(0, "8-K")
    recent["filingDate"].insert(0, "2026-09-23")
    recent["primaryDocument"].insert(0, "agent-submitted.htm")
    recent["primaryDocDescription"].insert(0, "Current report")
    headers = {"content-type": "application/json", "etag": '"agent-submitted"',
               "last-modified": "Wed, 23 Sep 2026 12:00:00 GMT"}
    result = SECEdgarDiscovery(
        sec_source(), http=FakeHttp([response(payload, headers=headers)]),
        now=lambda: STAMP, request_interval=0,
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 2
    assert result.state_updates["9999999999"]["etag"] == '"agent-submitted"'
    assert result.state_updates["9999999999"]["last_modified"] == "Wed, 23 Sep 2026 12:00:00 GMT"


@pytest.mark.parametrize("payload, message", [
    ([], "must be an object"),
    ({"cik": "9999999999", "name": "Example", "filings": {}}, "missing issuer or filings.recent"),
    ({"cik": "9999999999", "name": "Example", "filings": {"recent": {
        "accessionNumber": ["9999999999-26-000001"], "form": ["8-K"], "filingDate": []
    }}}, "inconsistent lengths"),
    ({"name": "Example", "filings": {"recent": {}}}, "missing CIK"),
])
def test_malformed_or_missing_sec_data_is_reported_source_locally(payload, message):
    http = FakeHttp([response(payload)])
    result = SECEdgarDiscovery(sec_source(), http=http, now=lambda: STAMP,
                               request_interval=0).collect()
    assert result.status == "failed"
    assert message in result.errors[0]


def test_wrong_issuer_payload_is_rejected():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["cik"] = "1"
    http = FakeHttp([response(payload)])
    result = SECEdgarDiscovery(sec_source(), http=http, now=lambda: STAMP,
                               request_interval=0).collect()
    assert result.status == "failed"
    assert "CIK mismatch" in result.errors[0]


def test_http_failure_and_rate_limit_error_are_reported_without_losing_other_sources():
    http = FakeHttp([DiscoveryHttpError("HTTP status 503", kind="http_error", status_code=503)])
    result = SECEdgarDiscovery(sec_source(), http=http, now=lambda: STAMP,
                               request_interval=0).collect()
    assert result.status == "failed"
    assert "http_error (HTTP 503)" in result.errors[0]


def test_304_sends_saved_validators_and_yields_no_duplicate_candidates():
    http = FakeHttp([response(status=304, headers={"etag": '"e2"'})])
    state = {"sources": {"sec-edgar-submissions": {"requests": {
        "9999999999": {"etag": '"e1"', "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    }}}, "candidates": {}}
    result = SECEdgarDiscovery(sec_source(), http=http, now=lambda: STAMP,
                               request_interval=0).collect(state)
    assert result.status == "not_modified"
    assert result.candidates == []
    assert http.calls[0][1]["etag"] == '"e1"'
    assert http.calls[0][1]["last_modified"].startswith("Tue,")
    assert result.state_updates["9999999999"]["etag"] == '"e2"'


def test_candidate_id_remains_stable_across_runs():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = []
    for _ in range(2):
        result = SECEdgarDiscovery(sec_source(), http=FakeHttp([response(payload)]),
                                   now=lambda: STAMP, request_interval=0).collect()
        ids.append([candidate.candidate_id for candidate in result.candidates])
    assert ids[0] == ids[1]


def test_issuer_allowlist_is_rotated_in_bounded_batches():
    payload = {"cik": "8888888888", "name": "Example Two", "filings": {"recent": {
        "accessionNumber": [], "form": [], "filingDate": []
    }}}
    source = sec_source(issuer_ciks=["9999999999", "8888888888"], max_issuers_per_run=1)
    http = FakeHttp([response(payload)])
    result = SECEdgarDiscovery(source, http=http, now=lambda: STAMP,
                               request_interval=0).collect({
        "sources": {"sec-edgar-submissions": {"pagination": {"next_issuer_index": 1}}},
        "candidates": {},
    })
    assert http.calls[0][0].endswith("CIK8888888888.json")
    assert result.status == "success"
    assert result.pagination == {"next_issuer_index": 0}


@pytest.mark.parametrize(("environment", "expected"), [
    ({}, "XRPIntelligenceFeed/0.3"),
    ({"SEC_CONTACT_EMAIL": "monitor@example.org"},
     "XRPIntelligenceFeed/0.3 (contact: monitor@example.org)"),
])
def test_user_agent_uses_app_name_or_configured_real_contact(environment, expected):
    payload = FIXTURE.read_bytes()

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        def iter_content(self, chunk_size):
            yield payload
        def close(self):
            pass

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["headers"]["User-Agent"] == expected
            return Response()

    result = SECEdgarDiscovery(sec_source(), session=Session(), environ=environment,
                               now=lambda: STAMP, request_interval=0).collect()
    assert result.status == "success"



@pytest.mark.parametrize(
    ("index_html", "expected_relevant"),
    [
        (
            b"""<html><div class="infoHead">Items</div><div class="info">8.01 Other Events</div>
            <table><tr><td>EX-99.1</td><td class="desc">Press release announcing stablecoin settlement and cross-border payments integration</td></tr></table></html>""",
            True,
        ),
        (
            b"""<html><div class="infoHead">Items</div><div class="info">5.02 Departure of Directors or Certain Officers; 9.01 Financial Statements and Exhibits</div>
            <table><tr><td>EX-10.1</td><td class="desc">Executive employment agreement and compensation terms</td></tr></table></html>""",
            False,
        ),
    ],
)
def test_sec_filing_context_separates_relevant_stablecoin_filing_from_generic_issuer_filing(
    index_html, expected_relevant
):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["cik"] = "1679788"
    payload["name"] = "Coinbase Global, Inc."
    source = sec_source(issuer_ciks=["1679788"], detail_enrichment_limit=1)
    index_url = _filing_index_url("1679788", "9999999999-26-000001")
    http = FakeHttp([
        response(payload),
        HttpResponse(200, {"content-type": "text/html"}, index_html, index_url),
    ])

    result = SECEdgarDiscovery(
        source, http=http, now=lambda: STAMP, request_interval=0, environ={}
    ).collect()

    assert result.status == "success"
    candidate = next(item for item in result.candidates if item.document_type == "8-K")
    assert candidate.source_native_metadata["filing_index_url"] == index_url
    detect_entities(candidate)
    classify_source_quality(candidate)
    score, reasons = score_relevance(candidate)

    if expected_relevant:
        assert "Coinbase" in candidate.detected_entities
        assert "institutional_digital_asset_payment" in candidate.relevance_categories
        assert score >= 30, f"relevant filing missed gate: score={score}, reasons={reasons}"
        assert "stablecoin settlement" in candidate.summary
    else:
        assert "Coinbase" in candidate.detected_entities
        assert score < 30, f"generic filing unexpectedly cleared gate: score={score}, reasons={reasons}"
