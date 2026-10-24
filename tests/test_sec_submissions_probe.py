import json
from pathlib import Path

import pytest

from scripts.probe_sec_submissions import summarize_payload


FIXTURE = Path(__file__).parent / "fixtures" / "sec_submissions.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_probe_validates_cik_and_summarizes_official_filing_indexes():
    result = summarize_payload(payload(), "9999999999")
    assert result["issuer_name"] == "Example Issuer Inc."
    assert result["cik"] == "9999999999"
    assert result["recent_rows"] == 3
    assert result["valid_rows"] == 3
    assert result["skipped_rows"] == 0
    assert result["skip_reasons"] == {}
    # Duplicate accessions are reported as returned by SEC; production collection
    # has its own deduplication contract. This probe only diagnoses the response.
    assert result["form_counts"] == {"10-K": 1, "8-K": 2}
    assert result["tracked_rows"] == 3
    assert result["tracked_form_counts"] == {"10-K": 1, "8-K": 2}
    assert result["sample"][0]["filing_index_url"] == (
        "https://www.sec.gov/Archives/edgar/data/9999999999/"
        "999999999926000001/9999999999-26-000001-index.htm"
    )


def test_probe_rejects_wrong_issuer_payload():
    with pytest.raises(ValueError, match="CIK mismatch"):
        summarize_payload(payload(), "1679788")


def test_probe_rejects_missing_recent_arrays():
    with pytest.raises(ValueError, match="missing required arrays"):
        summarize_payload({"cik": "9999999999", "name": "Example", "filings": {"recent": {}}}, "9999999999")


def test_probe_rejects_inconsistent_required_arrays():
    data = payload()
    data["filings"]["recent"]["filingDate"].pop()
    with pytest.raises(ValueError, match="inconsistent lengths"):
        summarize_payload(data, "9999999999")


def test_probe_skips_bad_accession_but_ignores_unused_primary_document_path():
    data = payload()
    recent = data["filings"]["recent"]
    recent["accessionNumber"][0] = "0000000001-26-000001"
    # The probe never constructs a primary-document URL, so a path-like
    # primaryDocument value must not invalidate the official filing-index row.
    recent["primaryDocument"][1] = "xslF345X05/inline-form.htm"
    result = summarize_payload(data, "9999999999")
    assert result["skipped_rows"] == 1
    assert result["skip_reasons"] == {"invalid_accession_or_cik": 1}
    assert result["valid_rows"] == 2
    assert result["tracked_rows"] == 2
    assert all(row["filing_index_url"].startswith("https://www.sec.gov/Archives/edgar/data/") for row in result["sample"])


def test_probe_accepts_missing_optional_document_arrays():
    data = payload()
    recent = data["filings"]["recent"]
    del recent["primaryDocument"]
    del recent["primaryDocDescription"]
    result = summarize_payload(data, "9999999999")
    assert result["valid_rows"] == 3
    assert all(row["description"] == "" for row in result["sample"])
