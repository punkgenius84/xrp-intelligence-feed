from discovery.http import HttpResponse
from scripts.probe_coinbase_article_metadata import (
    TARGET_PATH,
    URL,
    probe_coinbase_article_metadata,
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
