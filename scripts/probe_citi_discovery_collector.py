from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.http import BoundedHttpClient
from discovery.institutional_press import InstitutionalPressDiscovery

HOME_URL = "https://www.citigroup.com/global"
NEWS_URL = "https://www.citigroup.com/global/news"


def probe_citi_discovery_collector(http=None) -> str:
    """Compare two official Citi index pages with in-memory activation; never persist state."""
    base_source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi collector diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    lines = []
    for index_name, index_url in (("global_news", NEWS_URL), ("home", HOME_URL)):
        source = {**base_source, "source_url": index_url, "enabled": True, "detail_fallback_limit": 2}
        result = InstitutionalPressDiscovery(
            source,
            http=client,
            now=lambda: datetime.now(timezone.utc),
        ).collect(source_state={})
        lines.append(
            f"Citi collector diagnostic index={index_name}: status={result.status} "
            f"candidates={len(result.candidates)} errors={len(result.errors)} "
            f"state_updates={len(result.state_updates)} observations={len(result.observations)}"
        )
        for candidate in result.candidates[:5]:
            path = urlsplit(candidate.url).path[:180]
            published = candidate.published_at.isoformat() if candidate.published_at else "missing"
            lines.append(
                f"  index={index_name} candidate path={path!r} published_at={published} "
                f"title_length={len(candidate.title)}"
            )
        for error in result.errors[:3]:
            lines.append(f"  index={index_name} diagnostic_error={error[:240]!r}")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_discovery_collector())


if __name__ == "__main__":
    main()
