import json
from pathlib import Path

from scripts.probe_imf_media_center import main as _main, report_probe

ROOT = Path(__file__).resolve().parents[1]


def test_imf_public_route_probes_use_official_hosts_and_dated_article_paths():
    config = json.loads((ROOT / "config" / "discovery_sources.json").read_text(encoding="utf-8"))
    configured = {item["source_id"]: item for item in config["sources"]}
    assert configured["imf-blogs"]["enabled"] is False
    assert configured["imf-global-financial-stability-report"]["enabled"] is False
    assert configured["imf-media-center-news"]["enabled"] is False

    blog = {
        "source_id": "imf-blogs-runner-probe",
        "source_url": "https://www.imf.org/en/blogs",
        "enabled": True,
        "allowed_hosts": ["www.imf.org", "imf.org"],
        "article_path_regex": r"/en/blogs/articles/\\d{4}/\\d{2}/\\d{2}/[^/?#]+",
    }
    gfsr = {
        "source_id": "imf-gfsr-runner-probe",
        "source_url": "https://www.imf.org/en/Publications/GFSR",
        "enabled": True,
        "allowed_hosts": ["www.imf.org", "imf.org"],
        "article_path_regex": r"/en/Publications/GFSR/Issues/\\d{4}/\\d{2}/\\d{2}/[^/?#]+",
    }
    assert blog["source_url"].startswith("https://www.imf.org/")
    assert gfsr["source_url"].startswith("https://www.imf.org/")
    assert all(host in {"www.imf.org", "imf.org"} for host in blog["allowed_hosts"] + gfsr["allowed_hosts"])
    assert "blogs/articles" in blog["article_path_regex"]
    assert "Publications/GFSR/Issues" in gfsr["article_path_regex"]
