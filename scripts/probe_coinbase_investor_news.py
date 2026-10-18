from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.http import BoundedHttpClient
from discovery.institutional_press import InstitutionalPressDiscovery


def probe_coinbase_investor_news(http=None, *, now=None) -> str:
    """Read the official IR index once; never saves state or enables the source."""
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "coinbase-investor-news"
    )
    source = dict(source)
    source["enabled"] = True
    # This is an index-only probe: no article-detail fallback requests.
    source.pop("detail_fallback_limit", None)
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Coinbase investor-news diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    collector = InstitutionalPressDiscovery(
        source,
        http=client,
        now=now or (lambda: datetime.now(timezone.utc)),
    )
    result = collector.collect()
    lines = [
        "Coinbase investor news diagnostic: "
        f"status={result.status} candidates={len(result.candidates)} "
        "detail_fallback=disabled"
    ]
    for item in result.candidates[:10]:
        published = item.published_at.isoformat() if item.published_at else "missing"
        lines.append(f"  candidate date={published} title={item.title[:240]!r} url={item.url}")
    for error in result.errors[:3]:
        lines.append(f"  diagnostic: {error[:600]}")
    return "\n".join(lines)


def main() -> None:
    print(probe_coinbase_investor_news())


if __name__ == "__main__":
    main()
