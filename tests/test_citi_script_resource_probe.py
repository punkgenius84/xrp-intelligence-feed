from scripts.probe_citi_index import _html_resource_diagnostics


def test_citi_resource_probe_reports_official_scripts_and_api_like_paths():
    html = b"""<html><head>
      <script src="/_next/static/chunks/main.js"></script>
      <script src="https://www.citigroup.com/global/assets/news.js?version=1"></script>
      <script src="https://cdn.attacker.example/evil.js"></script>
      <script>const endpoint = "/global/api/news/press-releases";</script>
    </head></html>"""

    result = _html_resource_diagnostics(html)

    assert "script_tags=4" in result
    assert "official_script_srcs=2" in result
    assert "external_script_srcs=1" in result
    assert "/_next/static/chunks/main.js" in result
    assert "/global/assets/news.js" in result
    assert "/global/api/news/press-releases" in result
    assert "attacker.example" not in result


def test_citi_resource_probe_ignores_malformed_external_urls():
    result = _html_resource_diagnostics(
        b'<script src="https://user@attacker.example/evil.js"></script>'
    )
    assert "official_script_srcs=0" in result
    assert "external_script_srcs=1" in result
