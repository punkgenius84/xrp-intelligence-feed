from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intelligence.event_quality import has_only_generic_evidence_titles
from intelligence.events import event_to_dict
from storage.intelligence_outbox import (
    IntelligenceOutboxError,
    JsonIntelligenceOutboxState,
)
from storage.intelligence_quarantine import (
    IntelligenceQuarantineError,
    JsonIntelligenceQuarantineState,
)


REASON = "all evidence titles are empty or generic navigation text"


def quarantine_event(
    event_id: str,
    *,
    outbox_path: str | Path = "state/intelligence_outbox.json",
    quarantine_path: str | Path = "state/intelligence_quarantine.json",
    now: datetime | None = None,
) -> str:
    """Quarantine one explicitly selected malformed event without publishing it."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("an exact non-empty event ID is required")

    outbox = JsonIntelligenceOutboxState(outbox_path)
    quarantine = JsonIntelligenceQuarantineState(quarantine_path)
    queued = outbox.load()
    matches = [item for item in queued if item.event.event_id == event_id]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one active queue item for event ID {event_id!r}; "
            f"found {len(matches)}"
        )

    item = matches[0]
    if not has_only_generic_evidence_titles(item.event):
        raise ValueError(
            f"event {event_id!r} has descriptive evidence; refusing to quarantine it"
        )

    # An existing audit record with the same key must be an exact match. The
    # quarantine store intentionally preserves the first record for a key, so
    # removing changed queue content on a key collision could lose that content.
    existing = {record.key: record for record in quarantine.load()}
    prior = existing.get(item.key)
    if prior is not None and (
        event_to_dict(prior.event) != event_to_dict(item.event)
        or prior.queued_at != item.queued_at
    ):
        raise ValueError(
            f"quarantine already contains different content for event {event_id!r}; "
            "active queue was left unchanged"
        )

    # Audit persistence must succeed before active-queue removal. If removal fails,
    # the event remains queued as well as quarantined, which is recoverable and safe.
    quarantine.quarantine(
        [item],
        reason=REASON,
        quarantined_at=now or datetime.now(timezone.utc),
    )
    outbox.remove({item.key})
    return event_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move one exact, malformed intelligence event from the active outbox "
            "to audit quarantine. This command never publishes to Discord."
        )
    )
    parser.add_argument(
        "--event-id",
        required=True,
        help="Exact event ID from a read-only outbox preview",
    )
    parser.add_argument("--outbox", default="state/intelligence_outbox.json")
    parser.add_argument("--quarantine", default="state/intelligence_quarantine.json")
    args = parser.parse_args()

    try:
        event_id = quarantine_event(
            args.event_id,
            outbox_path=args.outbox,
            quarantine_path=args.quarantine,
        )
    except (ValueError, IntelligenceOutboxError, IntelligenceQuarantineError) as exc:
        print(f"Quarantine refused: {exc}", file=sys.stderr)
        return 2

    print(
        f"Quarantined exactly one malformed event ({event_id}); "
        "full event/evidence preserved; no Discord publication attempted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
