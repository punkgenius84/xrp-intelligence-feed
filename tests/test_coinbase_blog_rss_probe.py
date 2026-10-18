from discovery.http import HttpResponse
from scripts.probe_coinbase_blog_rss import (
    RSS_URL,
    TARGET_PATH,
    probe_coinbase_blog_rss,
)


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _response(body, *, status=200, content_type="application/rss+xml", url=RSS_URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def _feed(item):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Coinbase Blog</title>'
        f'{item}</channel></rss>'
    ).encode()


def test_probe_recognizes_official_feed_and_reports_target_date_read_only():
    feed = _feed(
        '<item><title>Coinbase stock trading</title>'
        f'<link>https://www.coinbase.com{TARGET_PATH}</link>'
        '<pubDate>Tue, 24 Feb 2026 12:00:00 GMT</pubDate></item>'
    )
    http = FakeHttp(_response(feed))

    result = probe_coinbase_blog_rss(http)

    assert "status=valid_feed" in result
    assert "format=rss20" in result
    assert "target_route=present" in result
    assert "target_date=2026-02-24T12:00:00Z" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == RSS_URL
    assert http.calls[0][1]["expected_content_types"]


def test_probe_does_not_claim_target_when_feed_omits_it():
    feed = _feed(
        '<item><title>Other official story</title>'
        '<link>https://www.coinbase.com/blog/other-story</link>'
        '<pubDate>Tue, 13 Oct 2026 12:00:00 GMT</pubDate></item>'
    )

    result = probe_coinbase_blog_rss(FakeHttp(_response(feed)))

    assert "status=valid_feed" in result
    assert "target_route=absent" in result
    assert "target_date=not_applicable" in result


def test_probe_rejects_html_even_when_http_succeeds():
    http = FakeHttp(_response(b"<html><body>Access denied</body></html>", content_type="text/html"))

    result = probe_coinbase_blog_rss(http)

    assert "status=rejected" in result
    assert "unexpected_content_type" in result


def test_probe_rejects_malformed_xml():
    result = probe_coinbase_blog_rss(FakeHttp(_response(b"<rss><channel><item>")))

    assert "status=invalid_feed" in result


def test_probe_rejects_non_official_final_host():
    http = FakeHttp(_response(_feed(""), url="https://attacker.example/blog/rss.xml"))

    result = probe_coinbase_blog_rss(http)

    assert result == "status=rejected reason=non_official_final_url"


def test_probe_reports_http_errors_without_raising_or_mutating_state():
    result = probe_coinbase_blog_rss(
        FakeHttp(_response(b"Forbidden", status=403, content_type="text/plain"))
    )

    assert result == "status=unavailable reason=unexpected_http_status http_status=403"
