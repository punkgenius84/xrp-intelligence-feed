from scripts.probe_asian_central_bank_feeds import TARGETS, _page_metadata


def test_probe_targets_known_official_bloom_and_thai_stablecoin_pages():
    targets = {label: (url, host) for label, url, host in TARGETS}
    assert targets["MAS BLOOM initiative"] == (
        "https://www.mas.gov.sg/schemes-and-initiatives/BLOOM",
        "www.mas.gov.sg",
    )
    assert targets["MAS media release"][1] == "www.mas.gov.sg"
    assert targets["BOT stablecoin hearing"] == (
        "https://www.bot.or.th/th/laws-and-rules/public-hearing/"
        "public-hearing-20261020-Stablecoin.html",
        "www.bot.or.th",
    )


def test_page_metadata_reports_article_owned_dates_and_same_host_canonical():
    html = """
    <html><head>
      <title>BLOOM announcement</title>
      <link rel="canonical" href="https://www.mas.gov.sg/news/media-releases/2026/bloom">
      <meta property="og:title" content="BLOOM announcement">
      <meta property="article:published_time" content="2026-10-23T00:00:00+08:00">
      <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"NewsArticle",
         "headline":"BLOOM announcement","datePublished":"2026-10-23T00:00:00+08:00"}
      </script>
    </head><body><time datetime="2026-10-23T00:00:00+08:00">23 October 2026</time></body></html>
    """
    metadata = _page_metadata(
        html, "https://www.mas.gov.sg/news/media-releases/2026/bloom", "www.mas.gov.sg"
    )
    assert metadata["canonical"] == [
        "https://www.mas.gov.sg/news/media-releases/2026/bloom"
    ]
    assert metadata["article:published_time"] == ["2026-10-23T00:00:00+08:00"]
    assert metadata["jsonld.datePublished"] == ["2026-10-23T00:00:00+08:00"]
    assert metadata["jsonld.headline"] == ["BLOOM announcement"]
    assert metadata["time.datetime"] == ["2026-10-23T00:00:00+08:00"]


def test_page_metadata_rejects_external_canonical_and_deduplicates_values():
    html = """
    <html><head>
      <link rel="canonical" href="https://example.invalid/bloom">
      <meta property="og:title" content="BLOOM announcement">
      <meta name="twitter:title" content="BLOOM announcement">
    </head></html>
    """
    metadata = _page_metadata(html, "https://www.mas.gov.sg/news", "www.mas.gov.sg")
    assert "canonical" not in metadata
    assert metadata["og:title"] == ["BLOOM announcement"]
    assert metadata["twitter:title"] == ["BLOOM announcement"]
