from discovery.http import HttpResponse
from scripts.probe_swift_press_index import INDEX_URLS, probe_swift_indexes


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(body, *, status=200, content_type="text/html", url=None):
    return HttpResponse(
        status,
        {"content-type": content_type},
        body,
        url or INDEX_URLS[0],
    )


def test_swift_probe_reports_valid_official_index_and_dated_article():
    body = b"""<html><body><main><article>
      <time datetime="2026-10-08T12:00:00Z">8 October 2026</time>
      <a href="/news-events/press-releases/swift-announces-new-supervisory-board-members">
        Swift announces new Supervisory Board members
      </a>
    </article></main></body></html>"""
    http = FakeHttp([response(body, url=url) for url in INDEX_URLS])

    result = probe_swift_indexes(http)

    assert result.count("status=valid_index") == len(INDEX_URLS)
    assert "dated_items=1" in result
    assert "2026-10-08T12:00:00+00:00" in result
    assert len(http.calls) == len(INDEX_URLS)
    assert all(call[1]["expected_content_types"] == ("text/html", "application/xhtml+xml") for call in http.calls)


def test_swift_probe_reports_http_failure_without_raising():
    http = FakeHttp([
        response(b"Forbidden", status=403, content_type="text/plain", url=url)
        for url in INDEX_URLS
    ])

    result = probe_swift_indexes(http)

    assert result.count("status=unavailable") == len(INDEX_URLS)
    assert "http_status=403" in result
    assert len(http.calls) == len(INDEX_URLS)


def test_swift_probe_rejects_non_official_redirect_and_wrong_content_type():
    http = FakeHttp([
        response(b"<html></html>", url="https://attacker.example/news-events/press-releases"),
        response(b'{"error":"not html"}', content_type="application/json", url=INDEX_URLS[1]),
        *[response(b"<html></html>", url=url) for url in INDEX_URLS[2:]],
    ])

    result = probe_swift_indexes(http)

    assert "status=rejected reason=non_official_final_url" in result
    assert "status=rejected reason=unexpected_content_type content_type=application/json" in result
    assert len(http.calls) == len(INDEX_URLS)


def test_swift_probe_rejects_unrecognized_official_listing_shape():
    body = b'<html><body><main><a href="/news-events/press-releases">Press releases</a></main></body></html>'
    result = probe_swift_indexes(FakeHttp([response(body, url=url) for url in INDEX_URLS]))

    assert result.count("status=invalid_page") == len(INDEX_URLS)
