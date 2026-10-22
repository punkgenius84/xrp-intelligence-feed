from discovery.http import HttpResponse
from scripts.probe_coinbase_landing_index import URL, TARGET_PATH, probe_landing_index
from scripts.probe_coinbase_yahoo_index import YAHOO_TARGET_PATH


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _response(body, *, status=200, content_type="text/html", url=URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_landing_index_reports_target_date_when_its_card_owns_the_date():
    html = (
        '<html><body><article>'
        '<time datetime="2026-02-24T12:00:00Z">February 24, 2026</time>'
        f'<a href="https://www.coinbase.com{TARGET_PATH}">Coinbase stock trading</a>'
        '</article></body></html>'
    ).encode()
    http = FakeHttp(_response(html))

    result = probe_landing_index(http)

    assert "status=valid_index" in result
    assert "target_route=present" in result
    assert "target_date=2026-02-24T12:00:00+00:00" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == URL


def test_landing_index_does_not_borrow_a_neighbor_card_date():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-13T12:00:00Z">October 13, 2026</time>'
        '<a href="https://www.coinbase.com/blog/other-story">Other official story</a>'
        '</article>'
        f'<div class="card"><a href="https://www.coinbase.com{TARGET_PATH}">Read More</a></div>'
        '</body></html>'
    ).encode()
    result = probe_landing_index(FakeHttp(_response(html)))

    assert "target_route=present" in result
    assert "target_date=unresolved" in result


def test_landing_index_reports_http_403_without_raising():
    result = probe_landing_index(
        FakeHttp(_response(b"Forbidden", status=403, content_type="text/plain"))
    )
    assert result == "status=unavailable reason=unexpected_http_status http_status=403"


def test_landing_index_rejects_non_official_final_host():
    result = probe_landing_index(
        FakeHttp(_response(b"<html></html>", url="https://attacker.example/en-gb/blog/landing"))
    )
    assert result == "status=rejected reason=non_official_final_url"


def test_landing_index_matches_locale_prefixed_target_route():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-15T12:00:00Z">October 15, 2026</time>'
        f'<a href="https://www.coinbase.com/en-gb{TARGET_PATH}">Coinbase stock trading</a>'
        '</article></body></html>'
    ).encode()

    result = probe_landing_index(FakeHttp(_response(html)))

    assert "target_route=present" in result
    assert "target_date=2026-10-15T12:00:00+00:00" in result


def test_landing_index_checks_yahoo_finance_route_instead_of_only_current_canary():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-19T12:00:00Z">October 19, 2026</time>'
        f'<a href="https://www.coinbase.com{YAHOO_TARGET_PATH}">Coinbase stock trading with Yahoo Finance</a>'
        '</article></body></html>'
    ).encode()

    result = probe_landing_index(FakeHttp(_response(html)), target_path=YAHOO_TARGET_PATH)

    assert "target_route=present" in result
    assert "target_date=2026-10-19T12:00:00+00:00" in result


def test_landing_index_does_not_assign_neighbor_date_to_yahoo_finance_route():
    html = (
        '<html><body><article>'
        '<time datetime="2026-10-13T12:00:00Z">October 13, 2026</time>'
        '<a href="https://www.coinbase.com/blog/other-story">Other official story</a>'
        '</article>'
        f'<div class="card"><a href="https://www.coinbase.com{YAHOO_TARGET_PATH}">Read More</a></div>'
        '</body></html>'
    ).encode()

    result = probe_landing_index(FakeHttp(_response(html)), target_path=YAHOO_TARGET_PATH)

    assert "target_route=present" in result
    assert "target_date=unresolved" in result
