import json
from pathlib import Path

import pytest

from scripts.probe_sec_submissions import select_index_targets, summarize_efts_payload, summarize_payload


FIXTURE = Path(__file__).parent / "fixtures" / "sec_submissions.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_efts_probe_summarizes_filing_context_and_items():
    result = summarize_efts_payload({
        "hits": {
            "total": {"value": 2, "relation": "eq"},
            "hits": [
                {
                    "_id": "0001193125-26-000267:filing.htm",
                    "_source": {
                        "file_date": "2026-10-20",
                        "form_type": "8-K",
                        "entity_name": "Example Stablecoin Issuer",
                        "file_description": "Current report",
                        "items": ["1.01", "8.01", "9.01"],
                    },
                    "highlight": {"doc_text": ["Stablecoin settlement agreement and issuer liquidity arrangements."]},
                },
                {
                    "_id": "malformed-accession:filing.htm",
                    "_source": {"file_date": "2026-10-19", "form_type": "8-K"},
                },
            ],
        },
    })
    assert result["total_hits"] == 2
    assert result["returned_hits"] == 2
    assert result["sample"] == [{
        "accession": "0001193125-26-000267",
        "file_date": "2026-10-20",
        "form": "8-K",
        "entity_name": "Example Stablecoin Issuer",
        "file_description": "Current report",
        "items": "1.01, 8.01, 9.01",
        "hit_keys": "_id,_source,highlight",
        "source_keys": "entity_name,file_date,file_description,form_type,items",
        "highlight_keys": "doc_text",
        "snippet": "Stablecoin settlement agreement and issuer liquidity arrangements.",
    }]


def test_efts_probe_supports_alternate_field_names_and_highlights():
    result = summarize_efts_payload({
        "hits": {
            "total": 1,
            "hits": [{
                "_id": "0001193125-26-000268:ex99.htm",
                "_source": {
                    "file_date": "2026-10-21",
                    "form": "8-K",
                    "display_names": ["Example Payments Inc."],
                    "file_description": "EX-99.1",
                    "items": ["7.01"],
                },
                "highlight": {"content": ["Payment stablecoin partnership announcement."]},
            }],
        },
    })
    row = result["sample"][0]
    assert row["form"] == "8-K"
    assert row["entity_name"] == "Example Payments Inc."
    assert row["snippet"] == "Payment stablecoin partnership announcement."
    assert row["highlight_keys"] == "content"


def test_efts_probe_rejects_missing_hit_structure():
    with pytest.raises(ValueError, match="missing hits.hits"):
        summarize_efts_payload({"hits": []})


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
    assert result["skip_reasons"] == {"accession_cik_mismatch": 1}
    assert result["foreign_filer_prefix_counts"] == {"0000000001": 1}
    assert result["valid_rows"] == 2
    assert result["tracked_rows"] == 2
    assert all(row["filing_index_url"].startswith("https://www.sec.gov/Archives/edgar/data/") for row in result["sample"])


def test_probe_distinguishes_bad_accession_format_from_foreign_filer_prefix():
    data = payload()
    recent = data["filings"]["recent"]
    recent["accessionNumber"][0] = "not-an-accession"
    recent["accessionNumber"][1] = "0000000001-26-000002"
    result = summarize_payload(data, "9999999999")
    assert result["skip_reasons"] == {
        "accession_cik_mismatch": 1,
        "invalid_accession_format": 1,
    }
    assert result["foreign_filer_prefix_counts"] == {"0000000001": 1}


def test_probe_accepts_missing_optional_document_arrays():
    data = payload()
    recent = data["filings"]["recent"]
    del recent["primaryDocument"]
    del recent["primaryDocDescription"]
    result = summarize_payload(data, "9999999999")
    assert result["valid_rows"] == 3
    assert all(row["description"] == "" for row in result["sample"])




def test_probe_summary_preserves_latest_8k_even_when_general_sample_has_only_other_forms():
    data = payload()
    recent = data["filings"]["recent"]
    recent["accessionNumber"] = [
        "9999999999-26-000010",
        "9999999999-26-000009",
        "9999999999-26-000008",
        "9999999999-26-000007",
        "9999999999-26-000006",
    ]
    recent["form"] = ["10-Q", "10-K", "10-Q", "8-K", "8-K"]
    recent["filingDate"] = [
        "2026-10-10", "2026-10-09", "2026-10-08", "2026-10-07", "2026-10-06"
    ]
    result = summarize_payload(data, "9999999999")
    assert [row["form"] for row in result["sample"]] == ["10-Q", "10-K", "10-Q"]
    assert result["latest_8k"]["form"] == "8-K"
    assert result["latest_8k"]["date"] == "2026-10-07"
    assert result["latest_8k"]["form"] == "8-K"


def test_filing_index_probe_selects_at_most_two_8k_pages():
    summaries = [
        ("Coinbase", "1679788", {"sample": [
            {"form": "10-Q", "filing_index_url": "https://www.sec.gov/q"},
            {"form": "10-K", "filing_index_url": "https://www.sec.gov/k"},
            {"form": "10-Q", "filing_index_url": "https://www.sec.gov/q2"},
        ], "latest_8k": {"form": "8-K", "accession": "a", "filing_index_url": "https://www.sec.gov/a"}}),
        ("Circle", "1876042", {"sample": [
            {"form": "10-Q", "filing_index_url": "https://www.sec.gov/q"},
        ], "latest_8k": {"form": "8-K", "accession": "b", "filing_index_url": "https://www.sec.gov/b"}}),
        ("Visa", "1403161", {"sample": [
            {"form": "10-Q", "filing_index_url": "https://www.sec.gov/q"},
        ], "latest_8k": {"form": "8-K", "accession": "c", "filing_index_url": "https://www.sec.gov/c"}}),
    ]
    targets = select_index_targets(summaries)
    assert [(label, row["accession"]) for label, _, row in targets] == [
        ("Coinbase", "a"), ("Circle", "b")
    ]


def test_filing_index_probe_limit_is_hard_capped_at_two():
    with pytest.raises(ValueError, match="from 0 to 2"):
        select_index_targets([], limit=3)


def test_filing_index_probe_can_be_disabled_for_contract_tests():
    summaries = [("Circle", "1876042", {"sample": [
        {"form": "8-K", "accession": "b", "filing_index_url": "https://www.sec.gov/b"},
    ]})]
    assert select_index_targets(summaries, limit=0) == []
