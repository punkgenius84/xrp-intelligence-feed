from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.institutional_sitemap import InstitutionalPressSitemapDiscovery


def probe_citi_sitemap_collector(http=None) -> str:
    """Exercise the production sitemap collector in memory; never persist state or publish."""
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    source = {**source, "enabled": True}
    result = InstitutionalPressSitemapDiscovery(source, http=http).collect(source_state={})
    lines = [
        f"Citi sitemap collector diagnostic: status={result.status} candidates={len(result.candidates)} "
        f"errors={len(result.errors)}",
    ]
    for candidate in result.candidates[:10]:
        lines.append(
            f"  candidate path={urlsplit(candidate.url).path[:180]!r} "
            f"published_at={candidate.published_at.isoformat()} title_length={len(candidate.title)}"
        )
    for error in result.errors[:5]:
        lines.append(f"  diagnostic_error={error[:240]!r}")
    lines.append(
        f"  state_updates_not_persisted={len(result.state_updates)} "
        f"pagination_keys={sorted(result.pagination)} persistent_writes=0 publishing=disabled"
    )
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_sitemap_collector())


if __name__ == "__main__":
    main()
