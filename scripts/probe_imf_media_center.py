from __future__ import annotations

import json
from pathlib import Path

from discovery.institutional_press import InstitutionalPressDiscovery


SOURCE_ID = "imf-media-center-news"
CONFIG_PATH = Path("config/discovery_sources.json")


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    source = next(
        (item for item in config["sources"] if item.get("source_id") == SOURCE_ID),
        None,
    )
    if source is None:
        raise SystemExit(f"Missing configured source: {SOURCE_ID}")

    # Exercise the existing production collector against the official index,
    # without enabling the source in normal feed runs or persisting any state.
    source = dict(source)
    source["enabled"] = True
    result = InstitutionalPressDiscovery(source).collect(source_state=None)

    print(f"IMF Media Center runner probe: status={result.status}")
    print(f"source_id={result.source_id} method={result.method}")
    print(f"candidate_count={len(result.candidates)}")
    for candidate in result.candidates[:5]:
        published = candidate.published_at.isoformat() if candidate.published_at else "undated"
        print(f"candidate: {published} | {candidate.title} | {candidate.url}")
    for error in result.errors[:10]:
        print(f"diagnostic: {error}")
    if len(result.errors) > 10:
        print(f"diagnostic: {len(result.errors) - 10} additional diagnostic(s) omitted")

    # This is an observational probe, not a release gate. Keep its workflow
    # green while making the collector's real status visible in the logs.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
