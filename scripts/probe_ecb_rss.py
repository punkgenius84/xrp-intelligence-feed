from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.ecb_rss import ECBRSSDiscovery
from discovery.http import BoundedHttpClient


def probe_ecb_rss() -> str:
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "ecb-press-releases"
    )
    # In-memory activation only. The production registry remains disabled until this passes.
    source = {**source, "enabled": True}
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only official ECB RSS diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    result = ECBRSSDiscovery(
        source,
        http=client,
        now=lambda: datetime.now(timezone.utc),
    ).collect(source_state={})
    lines = [
        f"ECB RSS runner probe: status={result.status} candidates={len(result.candidates)} "
        f"errors={len(result.errors)} state_updates={len(result.state_updates)}",
    ]
    for candidate in result.candidates[:10]:
        lines.append(
            f"  candidate path={urlsplit(candidate.url).path[:180]!r} "
            f"published_at={candidate.published_at.isoformat() if candidate.published_at else 'missing'} "
            f"title={candidate.title[:180]!r}"
        )
    for error in result.errors[:5]:
        lines.append(f"  diagnostic_error={error[:240]!r}")
    lines.append("  persistent_state_saved=false publishing=disabled source_enabled_in_production=false")
    return "\n".join(lines)


if __name__ == "__main__":
    print(probe_ecb_rss())
