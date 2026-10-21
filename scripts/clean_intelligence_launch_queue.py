from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intelligence.events import event_to_dict
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState


CONFIRMATION = "CLEAN-INTELLIGENCE-QUEUE"
REASON = "pre-launch queue reset; preserved for audit and intentionally not published"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def clean_queue(
    *,
    outbox_path: str | Path = "state/intelligence_outbox.json",
    quarantine_path: str | Path = "state/intelligence_quarantine.json",
    apply: bool = False,
    confirmation: str | None = None,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Preview or archive-and-clear the active queue. Never publishes to Discord."""
    outbox = JsonIntelligenceOutboxState(outbox_path)
    quarantine = JsonIntelligenceQuarantineState(quarantine_path)
    queued = outbox.load()
    existing = {item.key: item for item in quarantine.load()}

    # Quarantine uses first-write-wins semantics. Never clear an active item if
    # the audit store already has different content under the same key.
    for item in queued:
        prior = existing.get(item.key)
        if prior is not None and (
            event_to_dict(prior.event) != event_to_dict(item.event)
            or _utc(prior.queued_at) != _utc(item.queued_at)
        ):
            raise ValueError(
                f"quarantine collision for {item.event.event_id!r}; active queue left unchanged"
            )

    if apply and confirmation != CONFIRMATION:
        raise ValueError(
            f"apply requires --confirm {CONFIRMATION}; no state was changed"
        )

    result: dict[str, int | bool] = {
        "pending_before": len(queued),
        "quarantine_before": len(existing),
        "applied": bool(apply),
        "pending_after": len(queued),
    }
    if not apply or not queued:
        return result

    stamp = _utc(now or datetime.now(timezone.utc))
    # Audit first. If writing/removing the active queue fails, the original
    # event is still recoverable from at least one store.
    quarantine.quarantine(queued, reason=REASON, quarantined_at=stamp)
    outbox.remove({item.key for item in queued})
    result["pending_after"] = len(outbox.load())
    result["quarantine_after"] = len(quarantine.load())
    if result["pending_after"] != 0:
        raise RuntimeError("queue cleanup did not leave an empty active outbox")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely archive and clear the intelligence outbox before launch. "
            "This tool never publishes to Discord and never resets delivery history."
        )
    )
    parser.add_argument("--outbox", default="state/intelligence_outbox.json")
    parser.add_argument("--quarantine", default="state/intelligence_quarantine.json")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually archive queued items and clear the active outbox (default is preview only).",
    )
    parser.add_argument("--confirm", help=f"Required with --apply: {CONFIRMATION}")
    args = parser.parse_args()

    try:
        result = clean_queue(
            outbox_path=args.outbox,
            quarantine_path=args.quarantine,
            apply=args.apply,
            confirmation=args.confirm,
        )
    except Exception as exc:
        print(f"Launch queue cleanup refused/failed: {exc}", file=sys.stderr)
        return 2

    print(
        "Launch queue cleanup: "
        f"pending_before={result['pending_before']} "
        f"pending_after={result['pending_after']} "
        f"quarantine_before={result['quarantine_before']} "
        f"applied={str(result['applied']).lower()}"
    )
    if not args.apply:
        print("Preview only: no state changed and no Discord post attempted.")
    elif result["pending_before"] == 0:
        print("Active queue was already empty; no state changed and no Discord post attempted.")
    else:
        print(
            f"Archived {result['pending_before']} queued event(s) before clearing the active queue. "
            "Delivery history and source/dedup state were not modified; no Discord post attempted."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
