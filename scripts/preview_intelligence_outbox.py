from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intelligence.event_quality import has_only_generic_evidence_titles
from storage.intelligence_outbox import JsonIntelligenceOutboxState


def _one_line(value: object, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _evidence_freshness(event, now: datetime, stale_after_days: int) -> tuple[str, str, int | None]:
    dates: list[datetime] = []
    for evidence in event.evidence:
        stamp = evidence.published_at
        if not isinstance(stamp, str) or not stamp.strip():
            continue
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        dates.append(parsed.astimezone(timezone.utc))
    if not dates:
        return "undated", "none", None
    latest = max(dates)
    age_days = max(0, int((now - latest).total_seconds() // 86400))
    if latest > now + timedelta(hours=24):
        return "future-dated", latest.date().isoformat(), age_days
    if latest < now - timedelta(days=stale_after_days):
        return "stale", latest.date().isoformat(), age_days
    return "fresh", latest.date().isoformat(), age_days


def _recommended_action(freshness: str, generic_titles_only: bool) -> str:
    # Read-only guidance aligned with the publisher's fail-closed checks.
    if freshness == "future-dated":
        return "hold_future_dated_evidence"
    if freshness == "undated":
        return "hold_missing_or_invalid_date"
    if freshness == "stale":
        return "quarantine_stale_before_publish"
    if generic_titles_only:
        return "quarantine_generic_evidence_titles"
    return "editorial_review_before_launch"


def render_preview(
    path: str | Path = "state/intelligence_outbox.json",
    *,
    limit: int = 20,
    now: datetime | None = None,
    stale_after_days: int = 30,
) -> str:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("preview limit must be between 1 and 100")
    if type(stale_after_days) is not int or stale_after_days < 1:
        raise ValueError("stale_after_days must be a positive integer")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    queued = JsonIntelligenceOutboxState(path).load()
    shown = queued[:limit]
    lines = [
        f"Intelligence outbox preview: pending={len(queued)} showing={len(shown)}",
        "Read-only preview: no state changes and no Discord posts.",
    ]
    for index, item in enumerate(shown, start=1):
        event = item.event
        lines.append(
            f"{index}. event_id={_one_line(event.event_id, 80)} "
            f"type={_one_line(event.event_type, 50)} "
            f"queued_at={item.queued_at.isoformat()}"
        )
        lines.append(f"   summary: {_one_line(event.summary, 300)}")
        lines.append(f"   significance: {_one_line(event.significance, 300)}")
            freshness, latest_date, age_days = _evidence_freshness(event, current, stale_after_days)
        generic_titles_only = has_only_generic_evidence_titles(event)
        action = _recommended_action(freshness, generic_titles_only)
        age_label = f" age_days={age_days}" if age_days is not None else ""
        lines.append(
            f"   review: evidence_freshness={freshness} latest_evidence_date={latest_date}"
            f"{age_label} generic_evidence_titles_only={str(generic_titles_only).lower()}"
            f" recommended_action={action}"
        )
        if not event.evidence:
            lines.append("   evidence: NONE")
            continue
        for evidence in event.evidence[:5]:
            lines.append(
                f"   evidence: source={_one_line(evidence.source, 100)} "
                f"date={_one_line(evidence.published_at, 40)} "
                f"title={_one_line(evidence.title, 180)} "
                f"url={_one_line(evidence.url, 300)}"
            )
        if len(event.evidence) > 5:
            lines.append(f"   evidence: {len(event.evidence) - 5} additional source(s) omitted")
    if len(queued) > len(shown):
        lines.append(f"Preview truncated: {len(queued) - len(shown)} queued event(s) not shown.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only preview of queued intelligence events.")
    parser.add_argument("--path", default="state/intelligence_outbox.json")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(render_preview(args.path, limit=args.limit))


if __name__ == "__main__":
    main()
