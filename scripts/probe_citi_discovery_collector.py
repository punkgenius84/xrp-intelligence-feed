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


def probe_citi_discovery_collector(http=None) -> str:
    """Exercise the production parser with Citi enabled in memory; never persist state."""
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    # The source remains disabled in configuration. This diagnostic override is local to
    # this process and proves whether the existing bounded detail fallback can parse it.
    source = {**source, "source_url": "https://www.citigroup.com/global/news", "enabled": True, "detail_fallback_limit": 2}
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi collector diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    result = InstitutionalPressDiscovery(
        source,
        http=client,
        now=lambda: datetime.now(timezone.utc),
    ).collect(source_state={})
    lines = [
        f"Citi collector diagnostic: status={result.status} candidates={len(result.candidates)} "
        f"errors={len(result.errors)} state_updates={len(result.state_updates)} observations={len(result.observations)}"
    ]
    for candidate in result.candidates[:5]:
        path = urlsplit(candidate.url).path[:180]
        published = candidate.published_at.isoformat() if candidate.published_at else "missing"
        lines.append(
            f"  candidate path={path!r} published_at={published} title_length={len(candidate.title)}"
        )
    for error in result.errors[:5]:
        lines.append(f"  diagnostic_error={error[:240]!r}")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_discovery_collector())


if __name__ == "__main__":
    main()
