from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState

CONFIRMATION = "POST-ONE-INTELLIGENCE-EVENT"


def _has_fresh_evidence(event, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    cutoff = current - timedelta(days=30)
    future_cutoff = current + timedelta(days=1)
    for evidence in event.evidence:
        try:
            parsed_url = urlsplit(str(evidence.url or "").strip())
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                continue
            stamp = evidence.published_at
            if not isinstance(stamp, str) or not stamp.strip():
                continue
            published_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            published_at = published_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if cutoff <= published_at <= future_cutoff:
            return True
    return False


def prepare_event(
    event_id: str,
    confirmation: str,
    *,
    outbox=None,
    quarantine=None,
    history=None,
    now: datetime | None = None,
) -> str:
    """Restore one explicitly approved, fresh quarantined event to the active outbox."""
    if confirmation != CONFIRMATION:
        raise ValueError(f"explicit confirmation required: {CONFIRMATION}")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("an exact event ID is required")

    outbox = outbox or JsonIntelligenceOutboxState()
    quarantine = quarantine or JsonIntelligenceQuarantineState()
    history = history or JsonIntelligenceDeliveryHistory()

    delivered = history.load(now=now)
    if event_id in delivered:
        raise ValueError("event is already in delivery history; refusing to requeue")

    queued = [item for item in outbox.load() if item.event.event_id == event_id]
    if len(queued) > 1:
        raise ValueError("event ID matches multiple active queue entries; refusing to continue")
    if queued:
        if not _has_fresh_evidence(queued[0].event, now=now):
            raise ValueError("active queue event lacks fresh, dated HTTP(S) evidence")
        return f"Event already queued; no change: {event_id}"

    matches = [item for item in quarantine.load() if item.event.event_id == event_id]
    if len(matches) != 1:
        raise ValueError(
            "exact event ID must match one quarantined event when not already queued; "
            f"matches={len(matches)}"
        )
    event = matches[0].event
    if not _has_fresh_evidence(event, now=now):
        raise ValueError("quarantined event lacks fresh, dated HTTP(S) evidence within 30 days")

    outbox.enqueue([event], queued_at=now)
    return f"Requeued exactly one quarantined event for the guarded one-event test: {event_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Requeue one explicitly approved fresh quarantined event for the guarded one-event test."
    )
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        print(prepare_event(args.event_id, args.confirm))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"Intelligence test preparation refused: {exc}") from exc


if __name__ == "__main__":
    main()
