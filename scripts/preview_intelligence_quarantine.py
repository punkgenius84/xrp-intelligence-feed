from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.intelligence_quarantine import JsonIntelligenceQuarantineState


def _one_line(value: object, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def render_preview(
    path: str | Path = "state/intelligence_quarantine.json",
    *,
    limit: int = 20,
) -> str:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("preview limit must be between 1 and 100")

    quarantined = JsonIntelligenceQuarantineState(path).load()
    shown = quarantined[:limit]
    lines = [
        f"Intelligence quarantine preview: total={len(quarantined)} showing={len(shown)}",
        "Read-only preview: no state changes and no Discord posts.",
    ]
    for index, item in enumerate(shown, start=1):
        lines.append(
            f"{index}. event_id={_one_line(item.event.event_id, 80)} "
            f"quarantined_at={item.quarantined_at.isoformat()}"
        )
        lines.append(f"   reason: {_one_line(item.reason, 240)}")
        lines.append(f"   queued_at: {item.queued_at.isoformat()}")
        lines.append(f"   summary: {_one_line(item.event.summary, 300)}")
        lines.append(f"   significance: {_one_line(item.event.significance, 300)}")
        if not item.event.evidence:
            lines.append("   evidence: NONE")
            continue
        for evidence in item.event.evidence[:5]:
            lines.append(
                f"   evidence: source={_one_line(evidence.source, 100)} "
                f"date={_one_line(evidence.published_at, 40)} "
                f"title={_one_line(evidence.title, 180)} "
                f"url={_one_line(evidence.url, 300)}"
            )
        if len(item.event.evidence) > 5:
            lines.append(f"   evidence: {len(item.event.evidence) - 5} additional source(s) omitted")
    if len(quarantined) > len(shown):
        lines.append(f"Preview truncated: {len(quarantined) - len(shown)} quarantined event(s) not shown.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only preview of stale intelligence held for review."
    )
    parser.add_argument("--path", default="state/intelligence_quarantine.json")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(render_preview(args.path, limit=args.limit))


if __name__ == "__main__":
    main()
