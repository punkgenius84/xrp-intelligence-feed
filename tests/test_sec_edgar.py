from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryRegistryError, load_discovery_sources,
                                validate_discovery_sources)
from discovery.http import DiscoveryHttpError, HttpResponse
from discovery.sec_edgar import (SEC_ARCHIVES_ROOT, SECEdgarDiscovery,
                                 SEC_SUBMISSIONS_ROOT, _filing_url, validate_sec_source)


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
    assert sec["issuer_ciks"] == []
    assert sec["filing_forms"] == ["8-K", "10-K", "10-Q", "S-1", "S-3"]
    assert {source["source_id"] for source in configured} == {
        "sec-edgar-submissions",
        "federal-register-api",
        "ofac-recent-actions",
        "cftc-press-releases",
        "fincen-press-releases",
        "treasury-press-releases",
    }


def test_registry_rejects_duplicate_sources_and_invalid_ciks():
    with pytest.raises(DiscoveryRegistryError, match="duplicate discovery source_id"):
        validate_discovery_sources({"schema_version": 1, "sources": [sec_source(), sec_source()]})
    with pytest.raises(ValueError, match="issuer_ciks"):
        validate_sec_source(sec_source(issuer_ciks=["not-a-cik"]))


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


def test_accession_for_another_cik_is_rejected_without_losing_valid_rows():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = payload["filings"]["recent"]
    recent["accessionNumber"].insert(0, "0000000001-26-000009")
    recent["form"].insert(0, "8-K")
    recent["filingDate"].insert(0, "2026-09-23")
    recent["primaryDocument"].insert(0, "wrong-issuer.htm")
    recent["primaryDocDescription"].insert(0, "Wrong issuer filing")

    result = SECEdgarDiscovery(sec_source(), http=FakeHttp([response(payload)]),
                               now=lambda: STAMP, request_interval=0).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source_native_metadata["accession_number"] == "9999999999-26-000001"
    assert "wrong-issuer" not in candidate.url
    assert "0000000001-26-000009" not in candidate.candidate_id
    with pytest.raises(ValueError, match="does not match"):
        _filing_url("9999999999", "0000000001-26-000009", "wrong-issuer.htm")


def test_partial_response_does_not_persist_validators_and_clean_response_can_retry():
    partial_payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    recent = partial_payload["filings"]["recent"]
    recent["accessionNumber"].insert(0, "0000000001-26-000009")
    recent["form"].insert(0, "8-K")
    recent["filingDate"].insert(0, "2026-09-23")
    recent["primaryDocument"].insert(0, "wrong-issuer.htm")
    recent["primaryDocDescription"].insert(0, "Wrong issuer filing")
    etag_headers = {"content-type": "application/json", "etag": '"partial"',
                    "last-modified": "Wed, 23 Sep 2026 12:00:00 GMT"}
    partial = SECEdgarDiscovery(
        sec_source(), http=FakeHttp([response(partial_payload, headers=etag_headers)]),
        now=lambda: STAMP, request_interval=0,
    ).collect()
    assert partial.status == "partial"
    assert len(partial.candidates) == 1
    assert partial.state_updates == {}

    clean_headers = {"content-type": "application/json", "etag": '"clean"'}
    clean = SECEdgarDiscovery(
        sec_source(), http=FakeHttp([response(json.loads(FIXTURE.read_text(encoding="utf-8")),
                                              headers=clean_headers)]),
        now=lambda: STAMP, request_interval=0,
    ).collect()
    assert clean.status == "success"
    assert len(clean.candidates) == 1
    assert clean.state_updates["9999999999"]["etag"] == '"clean"'


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
