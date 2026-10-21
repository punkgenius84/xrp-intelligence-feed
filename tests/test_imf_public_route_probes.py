import json
import re
from pathlib import Path

from scripts.probe_imf_media_center import public_probe_routes

ROOT = Path(__file__).resolve().parents[1]


def test_imf_public_routes_are_probed_but_remain_disabled_in_production_config():
    config = json.loads((ROOT / "config" / "discovery_sources.json").read_text(encoding="utf-8"))
    configured = {item["source_id"]: item for item in config["sources"]}
    assert configured["imf-blogs"]["enabled"] is False
    assert configured["imf-global-financial-stability-report"]["enabled"] is False
    assert configured["imf-media-center-news"]["enabled"] is False

    routes = {item["source_id"]: item for item in public_probe_routes()}
    assert set(routes) == {
        "imf-public-news-runner-probe",
        "imf-blogs-runner-probe",
        "imf-gfsr-runner-probe",
    }
    samples = {
        "imf-public-news-runner-probe": "/en/news/articles/2026/10/21/sample-news",
        "imf-blogs-runner-probe": "/en/blogs/articles/2026/10/21/sample-blog",
        "imf-gfsr-runner-probe": "/en/Publications/GFSR/Issues/2026/10/21/sample-report",
    }
    for source_id, route in routes.items():
        assert route["enabled"] is True  # in-memory probe only
        assert route["source_url"].startswith("https://www.imf.org/")
        assert set(route["allowed_hosts"]) <= {"www.imf.org", "imf.org"}
        assert re.search(route["article_path_regex"], samples[source_id])
