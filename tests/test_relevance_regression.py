"""Regression fixtures for relevance scoring of high-value institutional events.

These cases protect the deterministic relevance gate from drifting toward either
generic finance noise or missing clearly Ripple/XRPL-related institutional news.
They do not authorize publishing or replace freshness/evidence checks.
"""
from datetime import datetime, timezone

import pytest

from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("title", "summary", "url"),
    [
        (
            "OCC Advances U.S. Digital Asset Leadership with Final Rule to Implement GENIUS Act",
            "The Office of the Comptroller of the Currency announced a final rule implementing the GENIUS Act. The rule establishes requirements for permitted payment stablecoin issuers and certain custody activities under OCC jurisdiction, and takes effect January 18, 2027.",
            "https://www.occ.gov/news-issuances/news-releases/2026/nr-occ-2026-92.html",
        ),
        (
            "DriveWealth and Ripple Partner to Bring Institutional Digital Asset Infrastructure to Global Investing",
            "The partnership includes RLUSD stablecoin settlement on the XRP Ledger, Ripple Payments for cross-border acceptance, digital-asset custody, and exploratory equity-financing work.",
            "https://ripple.com/ripple-press/drivewealth-and-ripple-partner-to-bring-institutional-digital-asset-infrastructure-to-global-investing/",
        ),
        (
            "Cross River Embeds Ripple USD Stablecoin on the XRP Ledger into its Core Banking Infrastructure",
            "Cross River announced an integration with Ripple to support RLUSD payments through its core banking platform and API, including real-time settlement and cross-border transactions.",
            "https://www.crossriver.com/newsroom/cross-river-embeds-ripple-usd-stablecoin-on-the-xrp-ledger-into-its-core-banking-infrastructure-expanding-faster-payment-capabilities-for-businesses",
        ),
        (
            "CSD BR and Ripple Use the XRP Ledger Blockchain to Expand Brazil's Financial Market Infrastructure",
            "The first phase uses XRPL to mirror tokenized investment-fund share ownership records. CSD BR's existing systems remain the official source of record for registration, deposit, and settlement.",
            "https://ripple.com/ripple-press/csdbr-and-ripple-use-the-xrpl-blockchain-to-expand-brazils-financial-market-infrastructure/",
        ),
        (
            "Standard Chartered launches RLUSD custody and joins planned collateral integration with Ripple and LMAX Group",
            "Standard Chartered's institutional custody support for RLUSD on XRPL is live. A separate collateral integration with Ripple and LMAX Group is planned.",
            "https://www.sc.com/en/press-release/standard-chartered-launches-rlusd-custody-and-joins-planned-collateral-integration-with-ripple-and-lmax-group/",
        ),
    ],
)
def test_primary_institutional_xrpl_developments_clear_relevance_gate(title, summary, url):
    item = NewsItem(
        title=title,
        summary=summary,
        url=url,
        source="Official announcement",
        source_id="relevance-regression",
        source_quality="primary",
        authority_tier=1,
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert score >= 30, f"expected relevant event to clear gate; score={score}, reasons={reasons}"
    assert reasons
    assert item.relevance_categories
    assert set(item.relevance_categories) & {
        "direct_xrp_xrpl",
        "direct_ripple",
        "rlusd_stablecoin",
        "relevant_regulatory_digital_asset_payments",
    }


@pytest.mark.parametrize(
    ("title", "summary", "source"),
    [
        (
            "OCC Announces Enforcement Actions Against Former Bank Officers",
            "The Office of the Comptroller of the Currency announced enforcement actions involving former bank officers. The release focuses on individual misconduct and compliance obligations.",
            "Office of the Comptroller of the Currency",
        ),
        (
            "Justice Department Announces Charges in Money Laundering Case",
            "The Department of Justice announced charges against individuals accused of laundering proceeds through shell companies and bank accounts.",
            "U.S. Department of Justice",
        ),
        (
            "Treasury Announces New Financial Reporting Rule",
            "The Treasury Department announced a financial reporting rule for covered institutions, including compliance deadlines and reporting obligations.",
            "U.S. Treasury",
        ),
    ],
)
def test_generic_enforcement_or_incidental_context_does_not_clear_relevance_gate(title, summary, source):
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://example.test/official-announcement",
        source=source,
        source_id="relevance-negative-control",
        source_quality="primary",
        authority_tier=1,
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert score < 30, f"generic/incidentally matched item unexpectedly cleared gate: {score}, {reasons}"


def test_observed_relevance_audit_scores_candidates_before_seen_filtering(capsys):
    import main

    direct = NewsItem(
        title="Ripple announces institutional payments integration",
        summary="Ripple announced a new payments integration for institutional customers.",
        url="https://example.test/ripple-payments",
        source="Official Ripple",
        source_id="ripple-press-center",
        authority_tier=1,
        published_at=STAMP,
    )
    generic = NewsItem(
        title="Regional bank updates office hours",
        summary="The bank announced changes to branch opening hours and customer service.",
        url="https://example.test/branch-hours",
        source="Regional bank",
        source_id="generic-bank",
        authority_tier=1,
        published_at=STAMP,
    )

    main._score_observed_relevance([direct, generic], publish_score=30)

    output = capsys.readouterr().out
    assert "observed=2" in output
    assert "score>=30=1" in output
    assert "Ripple announces institutional payments integration" in output
    assert "Relevance audit qualifying candidates by source" in output
    assert direct.relevance_score >= 30
    assert generic.relevance_score < 30


@pytest.mark.parametrize(
    ("title", "summary", "expected_entity"),
    [
        (
            "ECB Announces Stablecoin Settlement Framework",
            "The European Central Bank announced a new framework for stablecoin settlement and digital asset payments.",
            "ECB",
        ),
        (
            "Bank of Canada Proposes Digital Asset Payment Guidance",
            "The Bank of Canada proposed guidance for stablecoin issuers and digital asset payment services.",
            "Bank of Canada",
        ),
    ],
)
def test_central_bank_digital_asset_actions_clear_relevance_gate(title, summary, expected_entity):
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://example.test/central-bank-digital-assets",
        source="Official central bank announcement",
        source_id="central-bank-relevance-regression",
        authority_tier=1,
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert expected_entity in item.detected_entities
    assert "relevant_regulatory_digital_asset_payments" in item.relevance_categories
    assert score >= 30, f"expected central-bank digital-asset action to clear gate; score={score}, reasons={reasons}"


@pytest.mark.parametrize(
    ("title", "summary", "expected_entity", "expected_category"),
    [
        (
            "Visa Announces Tokenization Platform for Financial Institutions",
            "Visa announced a tokenization platform to support financial-market infrastructure.",
            "Visa",
            "institutional_tokenization_action",
        ),
        (
            "ECB Announces Stablecoins Framework for Digital Assets",
            "The European Central Bank announced guidance for stablecoins and digital assets.",
            "ECB",
            "relevant_regulatory_digital_asset_payments",
        ),
    ],
)
def test_official_tokenization_and_plural_asset_actions_clear_gate(
    title, summary, expected_entity, expected_category
):
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://example.test/official-digital-asset-action",
        source="Official announcement",
        source_id="official-digital-asset-regression",
        authority_tier=1,
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert expected_entity in item.detected_entities
    assert expected_category in item.relevance_categories
    assert score >= 30, f"expected official digital-asset action to clear gate; score={score}, reasons={reasons}"


@pytest.mark.parametrize(
    ("title", "summary", "should_clear"),
    [
        (
            "SEC Form 8-K filing: Circle Internet Group, Inc.",
            "Circle Internet Group, Inc. filed SEC Form 8-K. Filing items: 1.01 Entry into a Material Definitive Agreement. Exhibit EX-99.1: Stablecoin settlement partnership announcement.",
            True,
        ),
        (
            "SEC Form 8-K filing: Circle Internet Group, Inc.",
            "Circle Internet Group, Inc. filed SEC Form 8-K. Filing items: Risk Factors. Exhibit EX-99.1: General corporate risk factors discussing stablecoin market volatility and liquidity conditions.",
            False,
        ),
    ],
)
def test_sec_filing_context_requires_a_concrete_digital_asset_action(title, summary, should_clear):
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://www.sec.gov/Archives/edgar/data/1876042/000187604226000246/0001876042-26-000246-index.htm",
        source="U.S. Securities and Exchange Commission",
        source_id="sec-edgar-submissions",
        authority_tier=1,
        category="regulatory",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Circle" in item.detected_entities
    if should_clear:
        assert "institutional_digital_asset_payment" in item.relevance_categories
        assert score >= 30, f"actionable SEC filing context should clear gate: {score}, {reasons}"
    else:
        assert "institutional_digital_asset_payment" not in item.relevance_categories
        assert score < 30, f"generic risk-factor filing should stay below gate: {score}, {reasons}"


@pytest.mark.parametrize(
    ("title", "summary", "source", "source_id", "expected_entity", "expected_category"),
    [
        (
            "Federal Reserve announces modernization of its payment settlement network",
            "The Federal Reserve announced improvements to payment settlement infrastructure.",
            "Board of Governors of the Federal Reserve System",
            "federal-reserve-board-rss",
            "Federal Reserve",
            "relevant_regulatory_payment_action",
        ),
        (
            "U.S. Treasury launches a new cross-border payments platform",
            "The Treasury launched a new cross-border payments platform.",
            "U.S. Department of the Treasury",
            "treasury-press-releases",
            "Treasury",
            "government_payment_action",
        ),
    ],
)
def test_payment_only_public_sector_actions_do_not_claim_digital_asset_link(
    title, summary, source, source_id, expected_entity, expected_category
):
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://example.test/payment-modernization",
        source=source,
        source_id=source_id,
        source_quality="primary",
        authority_tier=1,
        category="government",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert expected_entity in item.detected_entities
    assert expected_category in item.relevance_categories
    assert "relevant_regulatory_digital_asset_payments" not in item.relevance_categories
    assert "government_digital_asset_action" not in item.relevance_categories
    assert score >= 30, f"important payment-system action should clear gate: {score}, {reasons}"


def test_swift_conventional_cross_border_payment_action_does_not_claim_digital_asset_link():
    item = NewsItem(
        title="Swift to launch AI-powered fraud defence for cross-border payments",
        summary="Swift is launching AI-powered fraud defence for cross-border payments.",
        url="https://news.google.com/rss/articles/example-swift-fraud-defence",
        source="Swift",
        source_id="swift-google-news-discovery",
        source_quality="primary",
        authority_tier=1,
        category="institutional",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Swift" in item.detected_entities
    assert "institutional_payment_action" in item.relevance_categories
    assert "institutional_digital_asset_payment" not in item.relevance_categories
    assert score >= 30, f"important payment-network action should clear gate: {score}, {reasons}"


def test_swift_blockchain_shared_ledger_mvp_milestone_clears_relevance_gate():
    item = NewsItem(
        title="Swift’s blockchain-based shared ledger progresses to MVP implementation",
        summary="Swift is progressing its blockchain-based shared ledger to MVP implementation for financial institutions.",
        url="https://www.swift.com/news-events/news/swifts-blockchain-based-shared-ledger-progresses-mvp-implementation",
        source="Swift",
        source_id="swift-google-news-discovery",
        source_quality="primary",
        authority_tier=1,
        category="institutional",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Swift" in item.detected_entities
    assert "institutional_digital_asset_payment" in item.relevance_categories
    assert score >= 30, f"concrete blockchain milestone should clear relevance gate: {score}, {reasons}"


def test_generic_tokenization_commentary_remains_below_relevance_gate():
    item = NewsItem(
        title="Market Research Highlights Tokenization Trends",
        summary="Industry forecasts discuss tokenization trends across financial markets.",
        url="https://example.test/tokenization-commentary",
        source="Market research",
        source_id="generic-tokenization-commentary",
        authority_tier=3,
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "broad_tokenization" in item.relevance_categories
    assert score < 30, f"generic tokenization commentary unexpectedly cleared gate: {score}, {reasons}"


def test_canada_gazette_stablecoin_regulations_clear_relevance_gate():
    item = NewsItem(
        title="Stablecoin Regulations",
        summary="Canada Gazette Part I; Proposed Regulations — Finance, Dept. of — Stablecoin Act and Retail Payment Activities Act",
        url="https://gazette.gc.ca/rp-pr/p1/2026/2026-10-24/html/reg1-eng.html",
        source="Canada Gazette Part I",
        source_id="canada-gazette-part-i",
        authority_tier=1,
        category="regulatory",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Government of Canada" in item.detected_entities
    assert "Department of Finance Canada" in item.detected_entities
    assert "relevant_regulatory_digital_asset_payments" in item.relevance_categories
    assert score >= 30, f"official stablecoin regulations should clear gate: {score}, {reasons}"


def test_bloom_central_bank_settlement_expansion_clears_relevance_gate():
    item = NewsItem(
        title="MAS, Bank Negara Malaysia and Bank of Thailand Partner with Industry to Advance Cross-Border Settlement in Digital Money through BLOOM",
        summary="The initiative includes Ripple and focuses on cross-border use of digital money, tokenized deposits and regulated stablecoins.",
        url="https://www.mas.gov.sg/news/media-releases/2026/mas-bnm-and-bot-partner-with-industry-to-advance-cross-border-settlement-in-digital-money",
        source="Monetary Authority of Singapore",
        source_id="mas-media-releases",
        authority_tier=1,
        category="regulatory",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Monetary Authority of Singapore" in item.detected_entities
    assert "Bank Negara Malaysia" in item.detected_entities
    assert "Bank of Thailand" in item.detected_entities
    assert "Ripple" in item.detected_entities
    assert "relevant_regulatory_digital_asset_payments" in item.relevance_categories
    assert score >= 30, f"BLOOM settlement expansion should clear gate: {score}, {reasons}"


def test_bnm_tokenized_bond_launch_clears_relevance_gate():
    item = NewsItem(
        title="BNM launches SSDS+: Powering digital and tokenised bond and sukuk issuance",
        summary="Bank Negara Malaysia launched a digital securities issuance service supporting tokenised bonds and sukuk.",
        url="https://www.bnm.gov.my/-/ssds-plus",
        source="Bank Negara Malaysia",
        source_id="bnm-press-releases",
        authority_tier=1,
        category="regulatory",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "Bank Negara Malaysia" in item.detected_entities
    assert "regulatory_tokenization_action" in item.relevance_categories
    assert score >= 30, f"central-bank tokenization launch should clear gate: {score}, {reasons}"



def test_xrpl_validator_bug_fix_update_clears_gate_for_secondary_media():
    item = NewsItem(
        title="XRP Ledger bug fix moved to November after validator support dipped for two hours",
        summary="The proposed amendment was delayed after validator support briefly fell below the required threshold.",
        url="https://news.google.com/rss/articles/example-xrpl-validator-bug-fix",
        source="CoinDesk",
        source_id="coindesk-xrpl-technical-google-news-discovery",
        authority_tier=2,
        category="technology",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "XRP" in item.detected_entities
    assert item.source_quality == "high_signal"
    assert "xrp_protocol_development" in item.relevance_categories
    assert score >= 30, f"significant XRPL protocol change should clear the gate: {score}, {reasons}"
    assert any("protocol/validator change" in reason for reason in reasons)


def test_xrp_price_opinion_does_not_receive_xrpl_protocol_bonus():
    item = NewsItem(
        title="XRP has failed to make holders rich like bitcoin and ether. Here's why",
        summary="A market commentary article compares historical returns and offers an opinion about XRP price performance.",
        url="https://news.google.com/rss/articles/example-xrp-opinion",
        source="CoinDesk",
        source_id="coindesk-xrpl-technical-google-news-discovery",
        authority_tier=2,
        category="technology",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "XRP" in item.detected_entities
    assert "xrp_protocol_development" not in item.relevance_categories
    assert score < 30, f"price commentary must not qualify as protocol news: {score}, {reasons}"



def test_franklin_templeton_fund_shares_on_xrpl_clear_gate_for_secondary_media():
    item = NewsItem(
        title="Franklin Templeton adds $150 million in fund shares to XRP Ledger, all in one wallet",
        summary="The report describes fund shares recorded on the XRP Ledger as tokenized investment-fund ownership.",
        url="https://news.google.com/rss/articles/example-franklin-templeton-xrpl-fund-shares",
        source="CoinDesk",
        source_id="coindesk-xrpl-technical-google-news-discovery",
        authority_tier=2,
        category="technology",
        published_at=STAMP,
    )

    detect_entities(item)
    classify_source_quality(item)
    score, reasons = score_relevance(item)

    assert "XRP" in item.detected_entities
    assert item.source_quality == "high_signal"
    assert "xrp_ledger_tokenization_development" in item.relevance_categories
    assert score >= 30, f"material XRPL fund-share tokenization should clear gate: {score}, {reasons}"
    assert any("tokenized fund/share development" in reason for reason in reasons)
