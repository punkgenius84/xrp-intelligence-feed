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
