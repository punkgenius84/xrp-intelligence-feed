from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.discovery_state import JsonDiscoveryState


def _one_line(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_preview(
    path: str | Path = "state/discovery.json",
    *,
    source_id: str | None = None,
    limit: int = 50,
) -> str:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("preview limit must be between 1 and 200")
    if source_id is not None and (not isinstance(source_id, str) or not source_id.strip()):
        raise ValueError("source ID must be a non-empty string when provided")

    state = JsonDiscoveryState(path).load()
    observations = []
    for current_source, source_state in state["sources"].items():
        if source_id and current_source != source_id:
            continue
        records = source_state.get("observations", {})
        for native_id, record in records.items():
            observations.append({
                "source_id": current_source,
                "native_id": native_id,
                **record,
            })
    observations.sort(
        key=lambda item: (item["last_seen_time"], item["source_id"], item["native_id"]),
        reverse=True,
    )
    shown = observations[:limit]
    lines = [
        f"Undated official-route observations: total={len(observations)} showing={len(shown)}",
        "Read-only preview: these records are for change detection only and are never publishable candidates.",
        "first_seen_time is when this feed first observed the route; it is not the article publication date.",
    ]
    for index, item in enumerate(shown, start=1):
        lines.append(
            f"{index}. source={_one_line(item['source_id'], 80)} "
            f"first_seen={item['first_seen_time']} last_seen={item['last_seen_time']}"
        )
        lines.append(f"   title: {_one_line(item['title'], 300)}")
        lines.append(f"   url: {_one_line(item['url'], 500)}")
    if len(observations) > len(shown):
        lines.append(f"Preview truncated: {len(observations) - len(shown)} observation(s) not shown.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only preview of official article routes that lack reliable publication dates."
    )
    parser.add_argument("--path", default="state/discovery.json")
    parser.add_argument("--source-id", default=None, help="Limit output to one source, e.g. coinbase-blog")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(render_preview(args.path, source_id=args.source_id, limit=args.limit))


if __name__ == "__main__":
    main()
