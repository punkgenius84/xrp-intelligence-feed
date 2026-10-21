from __future__ import annotations

import json
from pathlib import Path
import sys

# Running a script by path puts scripts/ at sys.path[0], not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.institutional_press import InstitutionalPressDiscovery


CONFIG_PATH = Path("config/discovery_sources.json")


def report_probe(label: str, source: dict[str, object]) -> None:
    # Enable only this in-memory copy; normal discovery config and state remain untouched.
    probe_source = dict(source)
    probe_source["enabled"] = True
    result = InstitutionalPressDiscovery(probe_source).collect(source_state=None)

    print(f"IMF runner probe: target={label} status={result.status}")
    print(f"source_id={result.source_id} method={result.discovery_method}")
    print(f"candidate_count={len(result.candidates)}")
    for candidate in result.candidates[:5]:
        published = candidate.published_at.isoformat() if candidate.published_at else "undated"
        print(f"candidate: {published} | {candidate.title} | {candidate.url}")
    for error in result.errors[:10]:
        print(f"diagnostic: {error}")
    if len(result.errors) > 10:
        print(f"diagnostic: {len(result.errors) - 10} additional diagnostic(s) omitted")


def public_probe_routes() -> list[dict[str, object]]:
    """Return temporary, read-only source definitions for public IMF routes."""
    return [
        {
            "source_id": "imf-public-news-runner-probe",
            "name": "International Monetary Fund — Public News",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/news",
            "enabled": True,
            "lookback_days": 365,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/news/articles/\\d{4}/\\d{2}/\\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-blogs-runner-probe",
            "name": "International Monetary Fund — Blogs",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/blogs",
            "enabled": True,
            "lookback_days": 180,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/blogs/articles/\\d{4}/\\d{2}/\\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-gfsr-runner-probe",
            "name": "International Monetary Fund — Global Financial Stability Report",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/Publications/GFSR",
            "enabled": True,
            "lookback_days": 365,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/Publications/GFSR/Issues/\\d{4}/\\d{2}/\\d{2}/[^/?#]+",
        },
    ]


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    media_center = next(
        (item for item in config["sources"] if item.get("source_id") == "imf-media-center-news"),
        None,
    )
    if media_center is None:
        raise SystemExit("Missing configured source: imf-media-center-news")

    report_probe("official IMF Media Center", media_center)

    # Probe public IMF News, blogs, and GFSR routes using temporary definitions.
    # None of these sources is enabled in normal discovery configuration.
    for route in public_probe_routes():
        report_probe(route["name"], route)

    # Observational only: keep the workflow green while exposing each actual
    # HTTP/parser result in logs. This script never publishes or persists state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
