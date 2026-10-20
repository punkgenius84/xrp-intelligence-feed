from scripts.probe_citi_sitemap import (
    _candidate_article_urls,
    _parse_sitemap,
    _safe_sitemap_url,
)


def test_citi_sitemap_accepts_only_official_global_sitemap_urls():
    assert _safe_sitemap_url("https://www.citigroup.com/global/sitemap.xml") == (
        "https://www.citigroup.com/global/sitemap.xml"
    )
    assert _safe_sitemap_url("https://attacker.example/global/sitemap.xml") is None
    assert _safe_sitemap_url("https://www.citigroup.com.evil.example/global/sitemap.xml") is None
    assert _safe_sitemap_url("https://www.citigroup.com/global/sitemap.xml?next=x") is None
    assert _safe_sitemap_url("http://www.citigroup.com/global/sitemap.xml") is None


def test_citi_sitemap_parser_reads_urlset_and_sitemap_index_with_namespaces():
    urlset = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.citigroup.com/global/news/press-release/2026/example</loc><lastmod>2026-10-19</lastmod></url>
      <url><loc>https://attacker.example/global/news/press-release/2026/nope</loc><lastmod>2026-10-20</lastmod></url>
    </urlset>"""
    kind, entries = _parse_sitemap(urlset)
    assert kind == "urlset"
    assert entries == [
        ("https://www.citigroup.com/global/news/press-release/2026/example", "2026-10-19")
    ]

    index = b"""<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://www.citigroup.com/global/sitemap-news.xml</loc></sitemap>
    </sitemapindex>"""
    kind, entries = _parse_sitemap(index)
    assert kind == "sitemapindex"
    assert entries == [("https://www.citigroup.com/global/sitemap-news.xml", "")]


def test_citi_sitemap_candidate_order_uses_lastmod_only_for_bounded_fetch_priority():
    source = {
        "source_url": "https://www.citigroup.com/global/news",
        "allowed_hosts": ["www.citigroup.com", "citigroup.com"],
        "article_path_regex": r"/global/news/press-release/\d{4}/[^/?#]+",
    }
    entries = [
        ("https://www.citigroup.com/global/news/press-release/2026/older", "2026-10-01"),
        ("https://www.citigroup.com/global/news/press-release/2026/newer", "2026-10-19"),
        ("https://www.citigroup.com/global/news/press-release/2026/undated", ""),
        ("https://www.citigroup.com/global/news/perspectives/2026/not-a-release", "2026-10-20"),
    ]
    assert _candidate_article_urls(source, entries) == [
        "https://www.citigroup.com/global/news/press-release/2026/newer/",
        "https://www.citigroup.com/global/news/press-release/2026/older/",
        "https://www.citigroup.com/global/news/press-release/2026/undated/",
    ]
