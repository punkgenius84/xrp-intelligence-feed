from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Running ``python scripts/verify_intelligence_test_result.py`` places the
# scripts directory, not the repository root, on sys.path. Add the root so
# the CLI can import the same project packages that pytest can import.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from storage.intelligence_delivery_history import (
    IntelligenceDeliveryHistoryError,
    JsonIntelligenceDeliveryHistory,
)
from storage.intelligence_outbox import (
    IntelligenceOutboxError,
    JsonIntelligenceOutboxState,
)


def verify_intelligence_test_result(
    event_id: str,
    *,
    history_store=None,
    outbox_store=None,
) -> None:
    """Fail closed unless the selected test event is recorded as delivered and no longer queued."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("an exact intelligence event ID is required")

    history = history_store or JsonIntelligenceDeliveryHistory()
    outbox = outbox_store or JsonIntelligenceOutboxState()
    try:
        delivered_ids = history.load()
        queued = outbox.load()
    except (IntelligenceDeliveryHistoryError, IntelligenceOutboxError) as exc:
        raise RuntimeError(f"cannot verify intelligence test state: {exc}") from exc

    if event_id not in delivered_ids:
        raise RuntimeError(
            f"selected event {event_id} is missing from intelligence delivery history"
        )

    remaining = sum(item.event.event_id == event_id for item in queued)
    if remaining:
        raise RuntimeError(
            f"selected event {event_id} remains in the active intelligence outbox "
            f"({remaining} matching entries)"
        )

    print(
        "Guarded intelligence delivery verified: selected event is in delivery history "
        "and absent from the active outbox."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify postconditions for a guarded one-event intelligence publication."
    )
    parser.add_argument("--event-id", required=True, help="Exact event ID selected for the test")
    parser.add_argument(
        "--history-path",
        default="state/intelligence_delivery_history.json",
        help="Delivery history JSON path (default: state/intelligence_delivery_history.json)",
    )
    parser.add_argument(
        "--outbox-path",
        default="state/intelligence_outbox.json",
        help="Active intelligence outbox JSON path (default: state/intelligence_outbox.json)",
    )
    args = parser.parse_args()

    try:
        verify_intelligence_test_result(
            args.event_id,
            history_store=JsonIntelligenceDeliveryHistory(Path(args.history_path)),
            outbox_store=JsonIntelligenceOutboxState(Path(args.outbox_path)),
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Intelligence test postcondition failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
