from discovery.http import HttpResponse
from scripts.probe_citi_index import URL, probe_citi_index


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def response(body, *, status=200, content_type="text/html", url=URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_citi_probe_reports_dated_official_article_without_enabling_source():
    html = b"""<html><body><main><article>
      <time datetime="2026-10-13T12:00:00Z">October 13, 2026</time>
      <a href="/global/news/press-release/2026/citi-third-quarter-2026-results-key-metrics">
        Third Quarter 2026 Results and Key Metrics
      </a>
    </article></main></body></html>"""
    http = FakeHttp(response(html))

    result = probe_citi_index(http)

    assert "status=valid_index" in result
    assert "parsed_items=1" in result
    assert "2026-10-13T12:00:00+00:00" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == URL
    assert http.calls[0][1]["expected_content_types"] == ("text/html", "application/xhtml+xml")


def test_citi_probe_reports_http_403_without_raising():
    result = probe_citi_index(FakeHttp(response(b"Forbidden", status=403, content_type="text/plain")))
    assert result == "Citi official index diagnostic: status=unavailable reason=unexpected_http_status http_status=403"


def test_citi_probe_rejects_non_official_redirect_and_wrong_content_type():
    result = probe_citi_index(FakeHttp(response(
        b"<html></html>", url="https://attacker.example/global/news/press-release"
    )))
    assert result == "Citi official index diagnostic: status=rejected reason=non_official_final_url"

    result = probe_citi_index(FakeHttp(response(b'{"error":"not html"}', content_type="application/json")))
    assert result == "Citi official index diagnostic: status=rejected reason=unexpected_content_type content_type=application/json"


def test_citi_probe_reports_unrecognized_html_structure_without_fetching_details():
    html = b'<html><body><main><a href="/global/news">News</a></main></body></html>'
    http = FakeHttp(response(html))

    result = probe_citi_index(http)

    assert "status=invalid_page" in result
    assert "allowlisted_article_links=0" in result
    assert len(http.calls) == 1
