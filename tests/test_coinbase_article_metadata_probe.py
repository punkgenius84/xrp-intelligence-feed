from discovery.http import HttpResponse
from scripts.probe_coinbase_article_metadata import (
    TARGET_PATH,
    URL,
    LOCALIZED_URL,
    LOCALIZED_TARGET_PATH,
    probe_coinbase_article_metadata,
    probe_coinbase_localized_article_metadata,
)


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _response(body, *, status=200, content_type="text/html", url=URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_article_probe_uses_explicit_official_published_time_metadata():
    html = (
        '<html><head><meta property="article:published_time" '
        'content="2026-02-24T12:00:00Z"></head><body>'
        '<time datetime="2026-10-18T12:00:00Z">unrelated date</time>'
        '</body></html>'
    ).encode()
    http = FakeHttp(_response(html))

    result = probe_coinbase_article_metadata(http)

    assert result == "status=official_metadata_date date=2026-02-24T12:00:00+00:00"
    assert len(http.calls) == 1
    assert http.calls[0][0] == URL
    assert http.calls[0][1]["expected_content_types"] == ("text/html",)


def test_article_probe_accepts_jsonld_only_when_it_owns_exact_article_url():
    html = f"""
    <html><head><script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "BlogPosting",
      "url": "https://www.coinbase.com{TARGET_PATH}",
      "datePublished": "2026-02-24T12:00:00Z"
    }}
    </script></head></html>
    """.encode()

    assert probe_coinbase_article_metadata(FakeHttp(_response(html))) == (
        "status=official_metadata_date date=2026-02-24T12:00:00+00:00"
    )


def test_article_probe_does_not_borrow_jsonld_date_from_another_article():
    html = """
    <html><head><script type="application/ld+json">
    {
      "@type": "BlogPosting",
      "url": "https://www.coinbase.com/blog/another-article",
      "datePublished": "2026-10-18T12:00:00Z"
    }
    </script></head><body>
    <time datetime="2026-10-18T12:00:00Z">unrelated date</time>
    </body></html>
    """.encode()

    assert probe_coinbase_article_metadata(FakeHttp(_response(html))) == (
        "status=official_page_date_unresolved"
    )


def test_article_probe_reports_conflicting_explicit_dates_without_choosing_one():
    html = (
        '<meta property="article:published_time" content="2026-02-24T12:00:00Z">'
        '<meta name="datePublished" content="2026-02-25T12:00:00Z">'
    ).encode()

    assert probe_coinbase_article_metadata(FakeHttp(_response(html))) == (
        "status=conflicting_official_metadata_date distinct_dates=2"
    )


def test_article_probe_rejects_non_official_final_url():
    result = probe_coinbase_article_metadata(
        FakeHttp(_response(b"<html></html>", url="https://attacker.example" + TARGET_PATH))
    )
    assert result == "status=rejected reason=non_official_final_url"


def test_article_probe_reports_http_errors_without_raising():
    result = probe_coinbase_article_metadata(
        FakeHttp(_response(b"Forbidden", status=403, content_type="text/plain"))
    )
    assert result == "status=unavailable reason=unexpected_http_status http_status=403"



def test_article_probe_accepts_official_calendar_date_without_inventing_time_or_zone():
    html = (
        '<meta property="article:published_time" content="2026-02-24">'
    ).encode()

    assert probe_coinbase_article_metadata(FakeHttp(_response(html))) == (
        "status=official_metadata_date date=2026-02-24"
    )


def test_localized_article_probe_accepts_only_exact_official_route_and_owned_date():
    html = (
        '<meta property="article:published_time" content="2026-02-24">'
        '<time datetime="2026-10-18">unrelated page date</time>'
    ).encode()
    http = FakeHttp(_response(html, url=LOCALIZED_URL))

    assert probe_coinbase_localized_article_metadata(http) == (
        "status=localized_official_metadata_date date=2026-02-24"
    )
    assert len(http.calls) == 1
    assert http.calls[0][0] == LOCALIZED_URL


def test_localized_article_probe_rejects_unrelated_official_article_redirect():
    other = "https://www.coinbase.com/en-ca/blog/another-article"
    result = probe_coinbase_localized_article_metadata(
        FakeHttp(_response(b"<meta property='article:published_time' content='2026-02-24'>", url=other))
    )
    assert result == "status=rejected reason=non_official_final_url"


def test_jsonld_can_identify_the_exact_localized_article_route():
    html = f"""
    <script type="application/ld+json">
    {{
      "@type": "BlogPosting",
      "url": "{LOCALIZED_URL}",
      "datePublished": "2026-02-24"
    }}
    </script>
    """.encode()
    assert probe_coinbase_localized_article_metadata(FakeHttp(_response(html, url=LOCALIZED_URL))) == (
        "status=localized_official_metadata_date date=2026-02-24"
    )


def test_coinbase_robots_sitemap_probe_only_accepts_official_sitemaps():
    from scripts.probe_coinbase_official_sitemap import _robots_sitemaps

    assert _robots_sitemaps(
        b"User-agent: *\nSitemap: https://www.coinbase.com/sitemap.xml\n"
        b"Sitemap: https://attacker.example/sitemap.xml\n"
        b"Sitemap: https://www.coinbase.com/sitemap.xml\n"
    ) == ["https://www.coinbase.com/sitemap.xml"]


def test_coinbase_sitemap_requires_exact_target_and_explicit_news_publication_date():
    from scripts.probe_coinbase_official_sitemap import TARGET_URL, _sitemap_target_metadata

    target_path = TARGET_URL.rsplit("/", 1)[-1]
    xml = f'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
      xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url><loc>{TARGET_URL}</loc><lastmod>2026-10-19</lastmod></url>
      <url><loc>https://www.coinbase.com/blog/another-article</loc>
        <news:news><news:publication><news:name>Coinbase</news:name>
        <news:language>en</news:language></news:publication>
        <news:publication_date>2026-02-24</news:publication_date></news:news></url>
      <url><loc>{TARGET_URL}</loc>
        <news:news><news:publication><news:name>Coinbase</news:name>
        <news:language>en</news:language></news:publication>
        <news:publication_date>2026-02-24</news:publication_date></news:news></url>
    </urlset>'''.encode()

    kind, dates = _sitemap_target_metadata(xml)

    assert kind == "urlset"
    assert dates == ["2026-02-24"]


def test_coinbase_sitemap_never_treats_lastmod_as_publication_date():
    from scripts.probe_coinbase_official_sitemap import TARGET_URL, _sitemap_target_metadata

    xml = (
        '<urlset><url><loc>' + TARGET_URL + '</loc>'
        '<lastmod>2026-10-19</lastmod></url></urlset>'
    ).encode()

    assert _sitemap_target_metadata(xml) == ("urlset", [])


def test_coinbase_sitemap_rejects_malformed_or_oversized_xml():
    from scripts.probe_coinbase_official_sitemap import MAX_BYTES, _sitemap_target_metadata

    assert _sitemap_target_metadata(b"<broken") == ("invalid_xml", [])
    assert _sitemap_target_metadata(b"x" * (MAX_BYTES + 1)) == ("oversize", [])
