import json

from scripts.probe_citi_index import _embedded_json_diagnostics


def test_citi_embedded_json_probe_inspects_large_bounded_payload_without_dumping_it():
    payload = {
        "releaseItems": [{
            "url": "/global/news/press-release/2026/citi-ripple-partnership",
            "title": "Citi announces a Ripple partnership",
            "publishDate": "2026-10-17",
        }],
        "large_unused_field": "x" * 320_000,
    }
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    ).encode()

    result = _embedded_json_diagnostics(html)

    assert "scripts=1 parsed=1" in result
    assert "releaseItems" in result
    assert "publishdate" in result
    assert "official_route_strings=1" in result
    assert "route_samples=['/global/news/press-release/2026/citi-ripple-partnership']" in result
    assert "iso_date_strings=1" in result
    assert "Citi announces a Ripple partnership" not in result
    assert len(result) < 2000


def test_citi_embedded_json_probe_bounds_oversized_scripts():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"releaseItems": [], "large": "x" * 1_500_001})
        + "</script>"
    ).encode()

    result = _embedded_json_diagnostics(html)

    assert "scripts=1 parsed=0" in result
    assert "skipped_oversize=1" in result


def test_citi_embedded_json_probe_handles_malformed_json():
    result = _embedded_json_diagnostics(
        b'<script id="__NEXT_DATA__" type="application/json">{not-json}</script>'
    )
    assert "scripts=1 parsed=0" in result
    assert "official_route_strings=0" in result
