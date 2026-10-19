from discovery.http import HttpResponse
from scripts.probe_coinbase_product_index import URL, TARGET_PATH, probe_product_index


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _response(body, *, status=200, content_type="text/html", url=URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_product_index_reports_target_date_only_when_card_owns_it():
    html = (
        '<html><body><article>'
        '<time datetime="2026-02-24T12:00:00Z">February 24, 2026</time>'
        f'<a href="https://www.coinbase.com{TARGET_PATH}">Coinbase stock trading</a>'
        '</article></body></html>'
    ).encode()
    http = FakeHttp(_response(html))

    result = probe_product_index(http)

    assert "status=valid_index" in result
    assert "target_route=present" in result
    assert "target_date=2026-02-24T12:00:00+00:00" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == URL
    assert http.calls[0][1]["expected_content_types"] == ("text/html",)


def test_product_index_does_not_borrow_neighbor_date():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-13T12:00:00Z">October 13, 2026</time>'
        '<a href="https://www.coinbase.com/blog/other-story">Other official story</a>'
        '</article>'
        f'<div class="card"><a href="https://www.coinbase.com{TARGET_PATH}">Read More</a></div>'
        '</body></html>'
    ).encode()

    result = probe_product_index(FakeHttp(_response(html)))

    assert "target_route=present" in result
    assert "target_date=unresolved" in result


def test_product_index_reports_http_403_without_raising():
    result = probe_product_index(FakeHttp(_response(b"Forbidden", status=403, content_type="text/plain")))
    assert result == "status=unavailable reason=unexpected_http_status http_status=403"


def test_product_index_rejects_non_official_final_host():
    result = probe_product_index(
        FakeHttp(_response(b"<html></html>", url="https://attacker.example/en-gb/blog/landing/product"))
    )
    assert result == "status=rejected reason=non_official_final_url"


def test_product_index_rejects_wrong_content_type():
    result = probe_product_index(FakeHttp(_response(b"<html></html>", content_type="application/json")))
    assert result == "status=rejected reason=unexpected_content_type content_type=application/json"


def test_product_index_matches_locale_prefixed_target_route():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-15T12:00:00Z">October 15, 2026</time>'
        f'<a href="https://www.coinbase.com/en-gb{TARGET_PATH}">Coinbase stock trading</a>'
        '</article></body></html>'
    ).encode()

    result = probe_product_index(FakeHttp(_response(html)))

    assert "target_route=present" in result
    assert "target_date=2026-10-15T12:00:00+00:00" in result
