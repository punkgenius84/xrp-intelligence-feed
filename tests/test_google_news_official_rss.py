from datetime import datetime, timezone
import warnings

import feedparser

import pytest

from discovery.google_news_official_rss import (
    GoogleNewsOfficialRSSDiscovery, METHOD, _published, validate_google_news_source,
)
from discovery.dispatch import DiscoveryRegistryError, collect_source, validate_discovery_sources
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.source_quality import classify_source_quality

STAMP = datetime(2026, 10, 24, 3, tzinfo=timezone.utc)
MAS_URL = "https://news.google.com/rss/search?q=site%3Amas.gov.sg%2Fnews%2Fmedia-releases&hl=en-SG&gl=SG&ceid=SG%3Aen"
SOURCE = {
    "source_id": "mas-google-news-discovery",
    "name": "Monetary Authority of Singapore (Google News discovery)",
    "authority_tier": 1,
    "category": "regulatory",
    "discovery_method": METHOD,
    "source_url": MAS_URL,
    "enabled": True,
    "lookback_days": 30,
    "max_items": 25,
    "publisher_host": "mas.gov.sg",
    "publisher_name": "Monetary Authority of Singapore",
}
LINK = "https://news.google.com/rss/articles/CBMiQ0FVX3lxTE5ExampleStableArticleKey?oc=5"
BOT_THAI_SOURCE = {
    "source_id": "bot-google-news-thai-stablecoin-hearing",
    "name": "Bank of Thailand (Google News Thai stablecoin hearing discovery)",
    "authority_tier": 1,
    "category": "regulatory",
    "discovery_method": METHOD,
    "source_url": "https://news.google.com/rss/search?q=site%3Abot.or.th%2Fth%2Flaws-and-rules%2Fpublic-hearing+stablecoin&hl=th&gl=TH&ceid=TH%3Ath",
    "enabled": True,
    "lookback_days": 30,
    "max_items": 100,
    "publisher_host": "bot.or.th",
    "publisher_name": "Bank of Thailand",
    "context_hint": "public hearing on THB stablecoin regulation",
}


def rss(*items):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>Google News</title>'
            + "".join(items) + "</channel></rss>").encode()


def item(title="MAS announces cross-border settlement initiative",
         link=LINK, date="Fri, 23 Oct 2026 09:00:00 GMT",
         publisher="Monetary Authority of Singapore",
         href="https://www.mas.gov.sg/"):
    return (f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
            f"<source url=\"{href}\">{publisher}</source>"
            "<description>Official publisher discovery result.</description></item>")


class FakeHttp:
    def __init__(self, body=None, error=None, status=200):
        self.body = body or rss(item())
        self.error = error
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return HttpResponse(self.status, {"content-type": "application/rss+xml"}, self.body, url)


def run(http, config=None):
    return collect_source(config or SOURCE, http=http, now=lambda: STAMP)


@pytest.mark.parametrize("query", [
    "site:mas.gov.sg.evil.example stablecoin",
    "site:mas.gov.sg OR site:evil.example",
    "site:mas.gov.sg stablecoin OR XRP",
    "site:mas.gov.sg -site:evil.example",
])
def test_google_news_source_rejects_broadened_or_misleading_site_filters(query):
    from urllib.parse import quote

    invalid = {**SOURCE, "source_url": f"https://news.google.com/rss/search?q={quote(query)}"}
    with pytest.raises((ValueError, DiscoveryRegistryError)):
        validate_google_news_source(invalid)


def test_secondary_crypto_news_discovery_probes_are_restricted_and_selectively_enabled():
    from discovery.dispatch import load_discovery_sources
    from urllib.parse import unquote_plus

    configured = load_discovery_sources()
    by_id = {item["source_id"]: item for item in configured}
    expected = {
        "coindesk-google-news-discovery": "coindesk.com",
        "coindesk-xrpl-technical-google-news-discovery": "coindesk.com",
        "cointelegraph-google-news-discovery": "cointelegraph.com",
        "theblock-google-news-discovery": "theblock.co",
        "blockworks-google-news-discovery": "blockworks.co",
        "dlnews-google-news-discovery": "dlnews.com",
    }
    for source_id, host in expected.items():
        source = by_id[source_id]
        assert source["enabled"] is (source_id == "theblock-google-news-discovery")
        assert source["authority_tier"] == 2
        assert source["publisher_host"] == host
        assert validate_google_news_source(source) == source
        query = unquote_plus(source["source_url"].split("q=", 1)[1].split("&", 1)[0])
        assert query.startswith(f"site:{host} ")
        assert "XRP" in query and "stablecoin" in query and "blockchain" in query


def test_swift_google_news_fallback_is_publisher_restricted_and_enabled_after_live_validation():
    from discovery.dispatch import load_discovery_sources
    from urllib.parse import unquote_plus

    configured = load_discovery_sources()
    source = next(item for item in configured if item["source_id"] == "swift-google-news-discovery")
    assert source["enabled"] is True
    assert source["publisher_host"] == "swift.com"
    assert validate_google_news_source(source) == source
    query = unquote_plus(source["source_url"].split("q=", 1)[1].split("&", 1)[0])
    assert query.startswith("site:swift.com ")
    assert "blockchain" in query
    assert "tokenized deposits" in query
    assert "cross-border payments" in query


def test_google_news_source_requires_domain_restricted_query():
    assert validate_google_news_source(SOURCE) == SOURCE
    # The hostname label "or" must not be mistaken for an OR search operator.
    assert validate_google_news_source(BOT_THAI_SOURCE) == BOT_THAI_SOURCE
    invalid = {**SOURCE, "source_url": "https://news.google.com/rss/search?q=xrp"}
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [invalid]})


@pytest.mark.parametrize("changes", [
    {"publisher_host": "mas.gov.sg.evil.example"},
    {"source_url": "https://evil.example/rss/search?q=site%3Amas.gov.sg"},
    {"enabled": "yes"},
    {"authority_tier": 3},
    {"max_items": 101},
])
def test_invalid_source_contract_fails_closed(changes):
    with pytest.raises((ValueError, DiscoveryRegistryError)):
        validate_google_news_source({**SOURCE, **changes})



def test_query_context_is_labeled_as_context_not_article_text():
    title = "การเปิดรับฟังความคิดเห็นต่อ หลักการกำกับดูแลผู้ประกอบธุรกิจที่เกี่ยวข้องกับ THB Stablecoin - Bank of Thailand"
    http = FakeHttp(rss(item(
        title=title,
        publisher="Bank of Thailand",
        href="https://www.bot.or.th/",
    )))
    result = run(http, BOT_THAI_SOURCE)

    assert result.status == "success"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert "Search context (not article text): public hearing on THB stablecoin regulation." in candidate.summary
    assert candidate.source_native_metadata["query_context_hint"] == "public hearing on THB stablecoin regulation"


def test_disabled_source_does_not_make_request():
    http = FakeHttp()
    result = run(http, {**SOURCE, "enabled": False})
    assert result.status == "not_configured"
    assert http.calls == []


def test_preserves_google_clickthrough_and_publisher_attribution_without_claiming_canonical_url():
    http = FakeHttp(rss(item()))
    result = run(http)
    assert http.calls[0][0] == MAS_URL
    assert result.status == "success"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.title == "MAS announces cross-border settlement initiative"
    assert candidate.url == LINK
    assert candidate.primary_url == LINK
    assert candidate.discovery_lead_url == LINK
    assert candidate.published_at == datetime(2026, 10, 23, 9, tzinfo=timezone.utc)
    assert candidate.source == "Monetary Authority of Singapore"
    assert candidate.source_native_metadata["publisher_attribution"] == "Monetary Authority of Singapore"
    assert candidate.source_native_metadata["publisher_href"] == "https://www.mas.gov.sg/"
    assert candidate.source_native_metadata["article_url_status"] == "google_news_clickthrough_unverified"
    assert candidate.provenance == [MAS_URL, LINK]


@pytest.mark.parametrize("bad_link", [
    "https://evil.example/rss/articles/CBMiQ0",
    "http://news.google.com/rss/articles/CBMiQ0",
    "https://news.google.com.evil.example/rss/articles/CBMiQ0",
    "https://user@news.google.com/rss/articles/CBMiQ0",
    "https://news.google.com/other/CBMiQ0",
])
def test_rejects_non_google_or_non_article_links(bad_link):
    result = run(FakeHttp(rss(item(link=bad_link))))
    assert result.status == "failed"
    assert result.candidates == []


def test_rejects_missing_publisher_attribution_and_invalid_dates():
    no_publisher = item().replace(
        '<source url="https://www.mas.gov.sg/">Monetary Authority of Singapore</source>', "")
    result = run(FakeHttp(rss(no_publisher)))
    assert result.status == "failed"
    assert "missing publisher attribution" in result.errors[0]

    result = run(FakeHttp(rss(item(date=""))))
    assert result.status == "failed"
    assert result.candidates == []


def test_ignores_old_items_and_reports_http_failures():
    old = item(date="Mon, 01 Jan 2024 10:00:00 GMT")
    assert run(FakeHttp(rss(old))).status == "empty"
    failed = run(FakeHttp(error=DiscoveryHttpError("blocked", kind="http_error", status_code=403)))
    assert failed.status == "failed"
    assert "403" in failed.errors[0]


def test_future_dated_items_are_rejected():
    result = run(FakeHttp(rss(item(date="Sun, 25 Oct 2026 12:00:00 GMT"))))
    assert result.status == "failed"
    assert result.candidates == []



def test_updated_only_atom_date_does_not_use_feedparser_deprecated_alias():
    payload = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example feed</title>
      <entry>
        <title>Updated-only item</title>
        <updated>2026-10-23T09:00:00Z</updated>
      </entry>
    </feed>"""
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        parsed = feedparser.parse(payload)
        stamp = _published(parsed.entries[0])

    assert stamp == datetime(2026, 10, 23, 9, tzinfo=timezone.utc)
    assert not [warning for warning in captured if issubclass(warning.category, DeprecationWarning)]



def test_secondary_google_news_candidate_preserves_tier_two_source_quality():
    from discovery.dispatch import load_discovery_sources

    source = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "theblock-google-news-discovery"
    )
    http = FakeHttp(rss(item(
        title="Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        publisher="The Block",
        href="https://www.theblock.co/",
    )))
    result = run(http, source)

    assert result.status == "success"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.authority_tier == 2
    assert classify_source_quality(candidate) == "high_signal"
