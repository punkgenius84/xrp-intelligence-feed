import json

import pytest

from intelligence.historical_context import (
    HistoricalContextError,
    format_historical_context,
    load_historical_context,
    select_historical_context,
)
from intelligence.llm.prompts import build_user_prompt


def test_context_selects_matching_historical_relationships():
    records = select_historical_context(
        "New developments involving MoneyGram",
        "The company announced a payments product.",
    )
    assert records
    moneygram = [record for record in records if record["id"] == "moneygram-xrp-odl-2018-2021"]
    assert moneygram
    assert "suspended in February 2021" in moneygram[0]["status"]
    assert "March 8, 2021" in moneygram[0]["status"]
    assert "Ripple statement page dated March 9" in moneygram[0]["status"]
    assert "committed to revisiting" in moneygram[0]["status"]
    assert "committed to revisiting" in moneygram[0]["relationship"]


def test_context_distinguishes_ripplenet_from_xrp_usage():
    records = select_historical_context(
        "American Express and Santander expand business payments",
        "The new payment channel uses Ripple's network.",
    )
    amex = [record for record in records if record["id"] == "amex-santander-ripplenet-2017"]
    assert amex
    assert "XRP use not established" in amex[0]["classification"]


def test_historical_context_is_explicitly_not_current_article_evidence():
    records = select_historical_context("MoneyGram and XRP", "Historical payment relationship")
    context = format_historical_context(records)
    prompt = build_user_prompt(
        title="MoneyGram announces a new service",
        summary="The company announced a new service.",
        source="Example",
        historical_context=context,
    )
    assert "HISTORICAL BACKGROUND" in prompt
    assert "not evidence for current-event claims" in prompt
    assert "must not be cited as evidence for current-article claims" in prompt


def test_context_pack_validates_unique_ids_and_source_urls(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "records": [{
            "id": "bad", "date": "2020-01-01", "entities": ["A"], "aliases": ["A"],
            "relationship": "Historical relationship", "classification": "historical",
            "status": "historical only", "source_url": "http://example.com",
        }],
    }), encoding="utf-8")
    with pytest.raises(HistoricalContextError, match="HTTPS"):
        load_historical_context(path)


def test_historical_context_captures_xrp_specific_sbi_odl_deployment():
    records = select_historical_context(
        "SBI Remit expands Japan remittances with Coins.ph",
        "The service connects with SBI VC Trade.",
    )
    sbi = [record for record in records if record["id"] == "sbi-remit-coinsph-odl-japan-2021"]
    assert sbi
    assert "XRP" in sbi[0]["relationship"]
    assert "current corridor" in sbi[0]["status"]


def test_stablecoin_and_tokenization_context_does_not_conflate_rlusd_with_xrp():
    records = select_historical_context(
        "DBS and Franklin Templeton announce sgBENJI and RLUSD plans",
        "The initiative involves the XRP Ledger.",
    )
    dbs = [record for record in records if record["id"] == "dbs-franklin-tokenized-fund-rlusd-xrpl-2025"]
    assert dbs
    assert "not evidence that XRP itself" in dbs[0]["classification"]
    assert "exploratory" in dbs[0]["status"]


def test_context_pack_includes_source_linked_institutional_history():
    records = load_historical_context()
    ids = {record["id"] for record in records}
    assert "bny-rlusd-reserve-custody-2025" in ids
    assert "securitize-buidl-vbill-rlusd-xrpl-2025" in ids
    assert all(record["source_url"].startswith("https://") for record in records)
