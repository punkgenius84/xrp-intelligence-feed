from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient
from discovery.institutional_press import InstitutionalPressDiscovery


SOURCE = {
    "source_id": "mas-news-runner-probe",
    "name": "Monetary Authority of Singapore",
    "authority_tier": 1,
    "category": "institutional",
    "discovery_method": "institutional_press_html",
    "source_url": "https://www.mas.gov.sg/news?content_type=Enforcement+Actions+Media+Releases&content_type=Media+Releases&page=1",
    "enabled": True,
    "lookback_days": 180,
    "max_items": 50,
    "allowed_hosts": ["www.mas.gov.sg", "mas.gov.sg"],
    "article_path_regex": r"/news/media-releases/\d{4}/[^/?#]+",
    "track_undated_observations": True,
}


def probe_mas_listing() -> str:
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only official MAS listing diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    result = InstitutionalPressDiscovery(
        SOURCE,
        http=client,
        now=lambda: datetime.now(timezone.utc),
    ).collect(source_state={})
    lines = [
        f"MAS official listing probe: status={result.status} candidates={len(result.candidates)} "
        f"errors={len(result.errors)} observations={len(result.observations)} state_updates={len(result.state_updates)}",
    ]
    for candidate in result.candidates[:10]:
        lines.append(
            f"  candidate path={urlsplit(candidate.url).path[:180]!r} "
            f"published_at={candidate.published_at.isoformat() if candidate.published_at else 'missing'} "
            f"title={candidate.title[:180]!r}"
        )
    for observation in result.observations[:5]:
        lines.append(
            f"  undated_observation path={urlsplit(observation.url).path[:180]!r} "
            f"title={observation.title[:160]!r}"
        )
    for error in result.errors[:5]:
        lines.append(f"  diagnostic_error={error[:240]!r}")
    lines.append("  persistent_state_saved=false publishing=disabled production_source_config_changed=false")
    return "\n".join(lines)


if __name__ == "__main__":
    print(probe_mas_listing())
