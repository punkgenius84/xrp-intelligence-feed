import json

from scripts.probe_citi_index import _embedded_json_diagnostics


def test_citi_embedded_json_diagnostics_show_only_bounded_official_paths():
    payload = {
        "releaseItems": [{
            "url": "https://www.citigroup.com/global/news/press-release/2026/citi-ripple-partnership?tracking=secret",
            "link": "/global/news/press-release/2026/citi-ripple-partnership",
            "title": "Citi announces a Ripple partnership",
            "publishDate": "2026-10-17",
        }]
    }
    html = ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>").encode()

    result = _embedded_json_diagnostics(html)

    assert "official_route_strings=1" in result
    assert "press_release_like_strings=2" in result
    assert "/global/news/press-release/2026/citi-ripple-partnership" in result
    assert "tracking=secret" not in result
    assert "Citi announces a Ripple partnership" not in result


def test_citi_embedded_json_diagnostics_omit_external_url_samples():
    payload = {"link": "https://attacker.example/global/news/press-release/2026/fake"}
    html = ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>").encode()

    result = _embedded_json_diagnostics(html)

    assert "attacker.example" not in result
    assert "url_samples=[]" in result
