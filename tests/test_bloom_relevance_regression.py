"""Regression coverage for the high-value 23 October 2026 BLOOM announcement.

The source adapter remains disabled until live runner validation; these tests only
protect event classification and ensure discovery cannot be enabled accidentally.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
BLOOM_URL = (
    "https://www.mas.gov.sg/news/media-releases/2026/"
    "mas-bnm-and-bot-partner-with-industry-to-advance-cross-border-settlement-in-digital-money"
)


def score_item(title: str, summary: str, url: str) -> NewsItem:
    item = NewsItem(
        title=title,
        summary=summary,
        url=url,
        source="Monetary Authority of Singapore",
        source_id="mas-google-news-discovery",
        source_quality="primary",
        authority_tier=1,
        category="regulatory",
        published_at=STAMP,
    )
    detect_entities(item)
    classify_source_quality(item)
    item.relevance_score, item.score_reasons = score_relevance(item)
    return item


def test_bloom_cross_border_digital_money_announcement_clears_relevance_gate():
    item = score_item(
        "MAS, Bank Negara Malaysia and Bank of Thailand Partner with Industry "
        "to Advance Cross-Border Settlement in Digital Money through BLOOM",
        "The Monetary Authority of Singapore announced that Bank Negara Malaysia "
        "and Bank of Thailand joined the BLOOM initiative as policymaker members. "
        "Industry participants including Ripple will work on cross-border digital "
        "money, tokenised deposits, regulated stablecoins, payment settlement, "
        "and interoperability across institutions and jurisdictions.",
        BLOOM_URL,
    )

    assert item.published_at == STAMP
    assert {"Monetary Authority of Singapore", "Bank Negara Malaysia",
            "Bank of Thailand", "Ripple"} <= set(item.detected_entities)
    assert item.relevance_score >= 30, (
        f"BLOOM announcement unexpectedly missed the relevance gate: "
        f"score={item.relevance_score}, reasons={item.score_reasons}"
    )
    assert set(item.relevance_categories) & {
        "direct_ripple",
        "relevant_regulatory_digital_asset_payments",
        "institutional_digital_asset_payment",
    }


def test_thai_stablecoin_public_hearing_clears_relevance_gate_with_labeled_query_context():
    item = score_item(
        "การเปิดรับฟังความคิดเห็นต่อ หลักการกำกับดูแลผู้ประกอบธุรกิจที่เกี่ยวข้องกับ THB Stablecoin - Bank of Thailand",
        "Google News discovery; publisher attribution: Bank of Thailand. "
        "Search context (not article text): public hearing on THB stablecoin regulation.",
        "https://news.google.com/rss/articles/example-thai-stablecoin-hearing",
    )

    assert "Bank of Thailand" in item.detected_entities
    assert item.relevance_score >= 30, (
        f"stablecoin hearing unexpectedly missed the relevance gate: "
        f"score={item.relevance_score}, reasons={item.score_reasons}"
    )
    assert "relevant_regulatory_digital_asset_payments" in item.relevance_categories


def test_coinbase_thunes_stablecoin_payout_connection_clears_relevance_gate():
    item = score_item(
        "Coinbase and Thunes connect stablecoin infrastructure with global fiat payouts",
        "Coinbase and Thunes connected stablecoin infrastructure to global fiat payouts.",
        "https://news.google.com/rss/articles/example-coinbase-thunes",
    )

    assert "Coinbase" in item.detected_entities
    assert item.relevance_score >= 30, (
        f"Coinbase/Thunes stablecoin connection unexpectedly missed the relevance gate: "
        f"score={item.relevance_score}, reasons={item.score_reasons}"
    )
    assert "institutional_digital_asset_payment" in item.relevance_categories


def test_imf_cbdc_cross_border_payment_report_clears_relevance_gate():
    item = NewsItem(
        title=(
            "Common Monetary Area: Technical Assistance Report-Project Sunbird "
            "and Cross-Border Payments with CBDC - International Monetary Fund | IMF"
        ),
        summary="Google News discovery; publisher attribution: International Monetary Fund.",
        url="https://news.google.com/rss/articles/example-imf-cbdc-report",
        source="International Monetary Fund",
        source_id="imf-google-news-discovery",
        source_quality="primary",
        authority_tier=1,
        category="institutional",
        published_at=STAMP,
    )
    detect_entities(item)
    classify_source_quality(item)
    item.relevance_score, item.score_reasons = score_relevance(item)

    assert "International Monetary Fund" in item.detected_entities
    assert item.relevance_score >= 30, (
        f"IMF CBDC cross-border report unexpectedly missed the relevance gate: "
        f"score={item.relevance_score}, reasons={item.score_reasons}"
    )
    assert "institutional_digital_asset_payment" in item.relevance_categories


def test_unrelated_mas_leadership_release_does_not_clear_relevance_gate():
    item = score_item(
        "MAS Announces Senior Leadership Changes",
        "The Monetary Authority of Singapore announced changes to its senior "
        "management team, effective 1 November 2026. The release describes role "
        "transitions and reporting responsibilities.",
        "https://www.mas.gov.sg/news/media-releases/2026/mas-announces-senior-leadership-changes",
    )

    assert item.relevance_score < 30, (
        f"unrelated leadership release incorrectly cleared the relevance gate: "
        f"score={item.relevance_score}, reasons={item.score_reasons}"
    )


def test_only_runner_validated_google_news_sources_are_enabled():
    payload = json.loads(
        (ROOT / "config" / "discovery_sources.json").read_text(encoding="utf-8")
    )
    sources = {source["source_id"]: source for source in payload["sources"]}
    expected = {
        "mas-google-news-discovery",
        "bot-google-news-english-discovery",
        "bot-google-news-thai-stablecoin-hearing",
        "coinbase-google-news-discovery",
        "swift-google-news-discovery",
        "imf-google-news-discovery",
    }

    assert expected <= sources.keys()
    assert sources["mas-google-news-discovery"]["enabled"] is True
    assert sources["bot-google-news-thai-stablecoin-hearing"]["enabled"] is True
    # These queries remain disabled until the live probe proves useful,
    # correctly attributed candidates pass the relevance gate.
    assert sources["bot-google-news-english-discovery"]["enabled"] is False
    # SWIFT's recent candidates were generic; IMF has one validated CBDC
    # cross-border payment report that clears the relevance gate.
    assert sources["swift-google-news-discovery"]["enabled"] is False
    assert sources["imf-google-news-discovery"]["enabled"] is True
    # Coinbase was enabled only after the live collector found a relevant
    # stablecoin-payout story with a defensible RSS publication timestamp.
    assert sources["coinbase-google-news-discovery"]["enabled"] is True
    # Google News RSS returns up to 100 entries by default; avoid self-inflicted
    # truncation/partial health on every run while retaining a hard upper bound.
    assert all(sources[source_id]["max_items"] == 100 for source_id in expected)
