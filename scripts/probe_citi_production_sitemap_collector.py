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


def probe_citi_production_sitemap_collector(http=None) -> str:
    """Exercise the real collector against Citi's official sitemap without saving state."""
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    # In-memory enable only; the production registry keeps Citi disabled until this passes live.
    source = {**source, "enabled": True, "sitemap_detail_limit": 3}
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi sitemap collector diagnostic)",
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
        f"Citi production sitemap collector: status={result.status} "
        f"candidates={len(result.candidates)} errors={len(result.errors)} "
        f"state_updates={len(result.state_updates)} observations={len(result.observations)} "
        f"pagination_keys={sorted(result.pagination.keys()) if isinstance(result.pagination, dict) else []}"
    ]
    for candidate in result.candidates[:5]:
        lines.append(
            f"  candidate path={urlsplit(candidate.url).path[:180]!r} "
            f"published_at={candidate.published_at.isoformat() if candidate.published_at else 'missing'} "
            f"title_length={len(candidate.title)}"
        )
    for error in result.errors[:5]:
        lines.append(f"  diagnostic_error={error[:240]!r}")
    lines.append("  persistent_state_saved=false publishing=disabled")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_production_sitemap_collector())


if __name__ == "__main__":
    main()
