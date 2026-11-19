import json

import pytest

from intelligence.historical_context import (
    HistoricalContextError,
    format_historical_context,
    load_historical_context,
    select_historical_context,
    select_prior_event_context,
    format_prior_event_context,
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


def test_context_captures_recent_regulatory_prime_brokerage_and_treasury_history():
    sec = select_historical_context(
        "SEC and Ripple dismiss appeals in XRP case",
        "The civil enforcement action was resolved.",
    )
    sec_record = [record for record in sec if record["id"] == "ripple-sec-appeals-dismissed-2025"]
    assert sec_record
    assert "final judgment" in sec_record[0]["relationship"]
    assert "not summarize the outcome as an unconditional ruling" in sec_record[0]["status"]

    prime = select_historical_context(
        "Ripple Prime completes Hidden Road acquisition",
        "RLUSD is used as collateral for some prime-brokerage products.",
    )
    prime_record = [record for record in prime if record["id"] == "hidden-road-ripple-prime-acquisition-2025"]
    assert prime_record
    assert "RLUSD collateral" in prime_record[0]["classification"]
    assert "XRP is used as collateral" in prime_record[0]["relationship"]

    treasury = select_historical_context(
        "Ripple Treasury launches digital asset accounts and Unified Treasury",
        "The platform supports XRP and RLUSD balances.",
    )
    treasury_record = [record for record in treasury if record["id"] == "ripple-treasury-native-digital-assets-2026"]
    assert treasury_record
    assert "not evidence that all customers use XRP or RLUSD" in treasury_record[0]["classification"]



def test_prior_event_retrieval_uses_relevant_older_events_as_context():
    events = {
        "evt-old": {
            "status": "superseded",
            "summary": "Ripple announced an institutional payments partnership with a bank.",
            "significance": "The relationship could affect cross-border settlement.",
            "entities": ["Ripple", "institutional payments"],
            "evidence": [{
                "title": "Ripple and bank announce institutional payments partnership",
                "url": "https://example.test/old-partnership",
                "published_at": "2025-06-10T12:00:00+00:00",
            }],
        },
        "evt-unrelated": {
            "status": "active",
            "summary": "Ripple sponsors a sports event.",
            "significance": "A brand marketing activity.",
            "entities": ["Ripple"],
            "evidence": [{
                "title": "Ripple announces sports sponsorship",
                "url": "https://example.test/sports",
                "published_at": "2026-10-01T12:00:00+00:00",
            }],
        },
    }

    records = select_prior_event_context(
        "Ripple expands institutional payments for bank settlement",
        "The service supports cross-border settlement.",
        events,
    )

    assert [record["id"] for record in records] == ["evt-old"]
    assert records[0]["status"] == "superseded"
    assert records[0]["date"] == "2025-06-10"
    rendered = format_prior_event_context(records)
    assert "not current-event evidence" in rendered
    assert "must not cite them for claims about the supplied current articles" in rendered


def test_prior_event_retrieval_excludes_current_source_and_undated_history():
    events = {
        "evt-same-source": {
            "status": "active",
            "summary": "Ripple announced institutional payments partnership settlement.",
            "significance": "Relevant historical background.",
            "entities": ["Ripple"],
            "evidence": [{
                "title": "Ripple institutional payments partnership settlement",
                "url": "https://example.test/current",
                "published_at": "2026-10-20T12:00:00+00:00",
            }],
        },
        "evt-undated": {
            "status": "active",
            "summary": "Ripple announced institutional payments partnership settlement.",
            "significance": "Relevant historical background.",
            "entities": ["Ripple"],
            "evidence": [{
                "title": "Ripple institutional payments partnership settlement",
                "url": "https://example.test/undated",
                "published_at": "",
            }],
        },
    }

    assert select_prior_event_context(
        "Ripple institutional payments partnership settlement",
        "A bank service was announced.",
        events,
        exclude_urls={"https://example.test/current"},
    ) == []



def test_context_pack_contains_xrp_protocol_supply_and_2026_institutional_records():
    records = load_historical_context()
    ids = {record["id"] for record in records}
    assert {
        "xrp-ledger-native-asset-and-consensus",
        "xrp-transaction-fees-and-reserves",
        "xrpl-xrp-auto-bridging-and-dex",
        "xrpl-amm-xls30-liquidity",
        "xrp-escrow-supply-mechanics",
        "xrp-odl-bridge-asset-mechanics",
        "xrpl-issued-tokens-and-rlusd-distinction",
        "xrpl-amendment-governance",
        "ripple-national-trust-bank-2026",
        "bny-xrp-rlusd-institutional-custody-2026",
        "c2fo-xrpl-rlusd-trade-finance-2026",
    } <= ids
    assert all(1 <= record["priority"] <= 5 for record in records)
    assert all(record["topic"] for record in records)


def test_context_selection_prefers_specific_recent_records_and_topic_diversity(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "records": [
            {
                "id": "old-generic", "date": "2016-01-01", "entities": ["Ripple"],
                "aliases": ["Ripple"], "relationship": "Old generic relationship.",
                "classification": "historical", "status": "historical only",
                "source_url": "https://example.test/old", "topic": "old", "priority": 1,
            },
            {
                "id": "new-specific", "date": "2026-10-01", "entities": ["Ripple", "XRP"],
                "aliases": ["Ripple", "XRP", "Ripple National Trust Bank"],
                "relationship": "Recent trust bank charter milestone.",
                "classification": "regulatory", "status": "verify current scope",
                "source_url": "https://example.test/new", "topic": "banking", "priority": 5,
            },
            {
                "id": "new-custody", "date": "2026-10-02", "entities": ["Ripple", "XRP"],
                "aliases": ["Ripple", "XRP", "XRP institutional custody"],
                "relationship": "Recent XRP custody milestone.",
                "classification": "custody", "status": "custody is not settlement",
                "source_url": "https://example.test/custody", "topic": "custody", "priority": 5,
            },
            {
                "id": "new-payment", "date": "2026-10-03", "entities": ["Ripple", "XRP"],
                "aliases": ["Ripple", "XRP", "XRP bridge asset"],
                "relationship": "Recent XRP bridge-asset milestone.",
                "classification": "payments", "status": "check live use",
                "source_url": "https://example.test/payments", "topic": "payments", "priority": 5,
            },
        ],
    }), encoding="utf-8")

    selected = select_historical_context(
        "Ripple XRP institutional custody and National Trust Bank developments",
        "A new XRP bridge asset payment service.",
        path=path,
        max_records=3,
    )
    ids = [record["id"] for record in selected]
    assert "old-generic" not in ids
    assert "new-specific" in ids
    assert "new-custody" in ids or "new-payment" in ids
    assert len({record["topic"] for record in selected}) == len(selected)


def test_context_includes_current_occ_bny_and_c2fo_status_cautions():
    bank = select_historical_context(
        "Ripple National Trust Bank receives final OCC approval",
        "Ripple National Trust Bank is effective October 26, 2026.",
    )
    assert any(record["id"] == "ripple-national-trust-bank-2026" and
               "not proof of a full-service commercial deposit-taking bank" in record["relationship"]
               for record in bank)

    custody = select_historical_context(
        "BNY now supports XRP and RLUSD on its Digital Asset Custody platform",
        "Ripple and BNY also explore tokenized trade finance.",
    )
    assert any(record["id"] == "bny-xrp-rlusd-institutional-custody-2026" and
               "Custody support establishes safekeeping capability" in record["relationship"]
               for record in custody)

    c2fo = select_historical_context(
        "C2FO Instant Pay moves trade finance to XRPL",
        "RLUSD is the specified settlement asset.",
    )
    assert any(record["id"] == "c2fo-xrpl-rlusd-trade-finance-2026" and
               "not evidence of XRP as settlement or bridge asset" in record["classification"]
               for record in c2fo)


def test_context_validation_checks_optional_priority_and_related_sources(tmp_path):
    path = tmp_path / "context.json"
    base_record = {
        "id": "bad-priority", "date": "2026-01-01", "entities": ["XRP"],
        "aliases": ["XRP"], "relationship": "Foundational XRP context.",
        "classification": "foundation", "status": "verify current state",
        "source_url": "https://example.test/source", "priority": 9,
    }
    path.write_text(json.dumps({"schema_version": 1, "records": [base_record]}), encoding="utf-8")
    with pytest.raises(HistoricalContextError, match="priority"):
        load_historical_context(path)

    base_record["id"] = "bad-related-source"
    base_record["priority"] = 5
    base_record["related_source_urls"] = ["http://example.test/insecure"]
    path.write_text(json.dumps({"schema_version": 1, "records": [base_record]}), encoding="utf-8")
    with pytest.raises(HistoricalContextError, match="related_source_urls"):
        load_historical_context(path)


def test_format_historical_context_includes_topic_priority_and_corroborating_sources():
    records = select_historical_context(
        "Ripple expands XRP custody with BNY",
        "XRP is supported on BNY's Digital Asset Custody platform.",
    )
    rendered = format_historical_context(records)
    assert "topic=institutional_custody" in rendered
    assert "priority=5/5" in rendered
    assert "https://ripple.com/ripple-press/ripple-expands-collaboration-with-bny-across-custody-and-tokenized-trade-finance/" in rendered



def test_context_pack_includes_spot_xrp_etp_market_access_with_product_caveats():
    records = load_historical_context()
    etp = [record for record in records if record["id"] == "xrp-spot-etf-market-access-2025"]
    assert len(etp) == 1
    assert "Bitwise ETF" in etp[0]["relationship"]
    assert "Grayscale XRP Trust ETF" in etp[0]["relationship"]
    assert "exact structure" in etp[0]["relationship"]
    assert "futures-based" in etp[0]["status"]
    assert len(etp[0]["related_source_urls"]) == 2


def test_spot_xrp_etf_record_is_selected_for_product_specific_news():
    records = select_historical_context(
        "Bitwise spot XRP ETF starts trading and Grayscale GXRP trust lists",
        "Franklin Templeton launched its XRPZ exchange-traded product.",
    )
    assert any(record["id"] == "xrp-spot-etf-market-access-2025" for record in records)
