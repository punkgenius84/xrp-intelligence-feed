from datetime import datetime, timezone

from discovery.bny_rss import BNYRSSDiscovery, METHOD, validate_bny_source
from discovery.http import HttpResponse
from discovery.dispatch import load_discovery_sources

STAMP = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)

SOURCE = {
    "source_id": "bny-newsroom",
    "name": "BNY",
    "authority_tier": 1,
    "category": "institutional",
    "discovery_method": METHOD,
    "source_url": "https://www.bny.com/bin/bnymellon/rssFeedGeneratorServlet.report",
    "enabled": True,
    "lookback_days": 180,
    "max_items": 50,
}

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>BNY Newsroom</title>
<item>
<title>BNY and Galaxy Collaborate to Advance Digital Asset Infrastructure</title>
<link>https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/bny-and-galaxy-collaborate-to-advance-digital-asset-infrastructure.html</link>
<pubDate>Tue, 04 Aug 2026 12:00:00 GMT</pubDate>
</item>
<item>
<title>Not an Official Article</title>
<link>https://example.com/fake</link>
<pubDate>Fri, 02 Oct 2026 12:00:00 GMT</pubDate>
</item>
</channel></rss>"""

class FakeHttp:
    def get(self, url, **kwargs):
        return HttpResponse(200, {"content-type": "application/rss+xml"}, RSS, url)

def test_bny_source_is_registered_as_strict_rss():
    sources = load_discovery_sources()
    source = next(item for item in sources if item["source_id"] == "bny-newsroom")
    assert source["discovery_method"] == METHOD
    assert validate_bny_source(source)["source_url"].endswith("rssFeedGeneratorServlet.report")

def test_bny_rss_keeps_only_official_articles():
    result = BNYRSSDiscovery(SOURCE, http=FakeHttp(), now=lambda: STAMP).collect()
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.errors
    item = result.candidates[0]
    assert item.title.startswith("BNY and Galaxy")
    assert item.published_at == datetime(2026, 8, 4, 12, tzinfo=timezone.utc)
    assert item.url == "https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/bny-and-galaxy-collaborate-to-advance-digital-asset-infrastructure.html"

def test_bny_rss_accepts_official_legacy_content_paths_and_canonicalizes_them():
    legacy = RSS.replace(
        b"https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/"
        b"bny-and-galaxy-collaborate-to-advance-digital-asset-infrastructure.html",
        b"https://www.bny.com/content/bnymellon/global/en/about-us/newsroom/press-release/"
        b"bny-and-galaxy-collaborate-to-advance-digital-asset-infrastructure.html",
    )
    class LegacyHttp:
        def get(self, url, **kwargs):
            return HttpResponse(200, {"content-type": "application/rss+xml"}, legacy, url)
    result = BNYRSSDiscovery(SOURCE, http=LegacyHttp(), now=lambda: STAMP).collect()
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].url == (
        "https://www.bny.com/corporate/global/en/about-us/newsroom/press-release/"
        "bny-and-galaxy-collaborate-to-advance-digital-asset-infrastructure.html"
    )
    assert result.errors


def test_bny_rss_rejects_non_bny_article_urls():
    bad = RSS.replace(b"https://example.com/fake", b"https://evil.example/corporate/global/en/about-us/newsroom/press-release/fake")
    class BadHttp:
        def get(self, url, **kwargs):
            return HttpResponse(200, {"content-type": "application/rss+xml"}, bad, url)
    result = BNYRSSDiscovery(SOURCE, http=BadHttp(), now=lambda: STAMP).collect()
    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.errors
