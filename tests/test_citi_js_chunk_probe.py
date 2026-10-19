from discovery.http import HttpResponse
from scripts.probe_citi_index import (
    URL,
    _inspect_citi_js_asset,
    _official_script_asset_urls,
    _probe_citi_js_assets,
)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def response(body, *, status=200, content_type="application/javascript", url):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_citi_script_selector_limits_to_two_official_next_bundles():
    html = b"""<script src="/global/citigroup-ui/_next/static/chunks/main-a.js"></script>
    <script src="/global/citigroup-ui/_next/static/chunks/pages/%5B...params%5D-b.js"></script>
    <script src="https://attacker.example/_next/static/chunks/evil.js"></script>
    <script src="/global/citigroup-ui/_next/static/chunks/pages/other.js"></script>"""

    urls = _official_script_asset_urls(html, URL)

    assert len(urls) == 2
    assert all("www.citigroup.com" in url for url in urls)
    assert any("/chunks/pages/%5B...params%5D-b.js" in url for url in urls)
    assert all("attacker.example" not in url for url in urls)


def test_citi_js_asset_inspection_reports_api_and_release_route_without_source_text():
    source = (
        'const q="query GetPressReleaseList";'
        'const endpoint="/graphql";'
        'const article="/global/news/press-release/2026/citi-ripple-partnership";'
    ).encode()

    result = _inspect_citi_js_asset(source, "https://www.citigroup.com/global/chunks/main.js")

    assert "official_route_strings=1" in result
    assert "/global/news/press-release/2026/citi-ripple-partnership" in result
    assert "/graphql" in result
    assert "GetPressReleaseList" in result
    assert "const q" not in result


def test_citi_js_probe_fetches_only_selected_official_assets():
    html = b'<script src="/_next/static/chunks/main-a.js"></script>'
    url = "https://www.citigroup.com/_next/static/chunks/main-a.js"
    http = FakeHttp([response(b'const endpoint="/graphql";', url=url)])

    result = _probe_citi_js_assets(http, html, URL)

    assert "js_asset_probe selected=1" in result
    assert "/graphql" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == url
