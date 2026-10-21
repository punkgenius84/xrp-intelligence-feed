from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intelligence.events import event_to_dict
from storage.intelligence_outbox import JsonIntelligenceOutboxState, QueuedIntelligence
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState


CONFIRMATION = "QUARANTINE-STALE-INTELLIGENCE"
REASON = "latest evidence is older than the 30-day publication window; held out of active queue for editorial refresh"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def evidence_freshness(
    item: QueuedIntelligence,
    now: datetime,
    stale_after_days: int = 30,
) -> tuple[str, str, int | None]:
    dates: list[datetime] = []
    for evidence in item.event.evidence:
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
    age_days = max(0, int((_utc(now) - latest).total_seconds() // 86400))
    if latest > _utc(now) + timedelta(hours=24):
        return "future-dated", latest.date().isoformat(), age_days
    if latest < _utc(now) - timedelta(days=stale_after_days):
        return "stale", latest.date().isoformat(), age_days
    return "fresh", latest.date().isoformat(), age_days


def find_stale_items(
    queued: list[QueuedIntelligence],
    *,
    now: datetime,
    stale_after_days: int = 30,
) -> list[QueuedIntelligence]:
    if type(stale_after_days) is not int or stale_after_days < 1:
        raise ValueError("stale_after_days must be a positive integer")
    if len({item.key for item in queued}) != len(queued):
        raise ValueError("active intelligence outbox contains duplicate keys; refusing to mutate it")
    return [
        item for item in queued
        if evidence_freshness(item, now, stale_after_days)[0] == "stale"
    ]


def quarantine_stale_queue(
    *,
    outbox_path: str | Path = "state/intelligence_outbox.json",
    quarantine_path: str | Path = "state/intelligence_quarantine.json",
    apply: bool = False,
    confirmation: str | None = None,
    now: datetime | None = None,
    stale_after_days: int = 30,
) -> dict[str, int | bool]:
    """Archive only stale-evidence events, then remove those keys from the active queue."""
    current_time = _utc(now or datetime.now(timezone.utc))
    outbox = JsonIntelligenceOutboxState(outbox_path)
    quarantine = JsonIntelligenceQuarantineState(quarantine_path)
    queued = outbox.load()
    stale = find_stale_items(queued, now=current_time, stale_after_days=stale_after_days)
    existing = {item.key: item for item in quarantine.load()}

    for item in stale:
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
        "stale_count": len(stale),
        "pending_after": len(queued),
        "quarantine_before": len(existing),
        "applied": bool(apply),
    }
    if not apply or not stale:
        return result

    quarantine.quarantine(stale, reason=REASON, quarantined_at=current_time)
    outbox.remove({item.key for item in stale})
    remaining = outbox.load()
    result["pending_after"] = len(remaining)
    result["quarantine_after"] = len(quarantine.load())
    expected_after = len(queued) - len(stale)
    if result["pending_after"] != expected_after:
        raise RuntimeError(
            f"stale quarantine expected {expected_after} active events, found {result['pending_after']}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or quarantine only stale-evidence intelligence events. "
            "Fresh, undated, and future-dated events remain active. Never publishes to Discord."
        )
    )
    parser.add_argument("--outbox", default="state/intelligence_outbox.json")
    parser.add_argument("--quarantine", default="state/intelligence_quarantine.json")
    parser.add_argument("--stale-after-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Archive stale events and remove them from the active queue.")
    parser.add_argument("--confirm", help=f"Required with --apply: {CONFIRMATION}")
    args = parser.parse_args()

    try:
        result = quarantine_stale_queue(
            outbox_path=args.outbox,
            quarantine_path=args.quarantine,
            apply=args.apply,
            confirmation=args.confirm,
            stale_after_days=args.stale_after_days,
        )
    except Exception as exc:
        print(f"Stale queue quarantine refused/failed: {exc}", file=sys.stderr)
        return 2

    print(
        "Stale queue quarantine: "
        f"pending_before={result['pending_before']} stale_count={result['stale_count']} "
        f"pending_after={result['pending_after']} quarantine_before={result['quarantine_before']} "
        f"applied={str(result['applied']).lower()}"
    )
    if not args.apply:
        print("Preview only: no state changed and no Discord post attempted.")
    elif result["stale_count"] == 0:
        print("No stale-evidence events found; no state changed and no Discord post attempted.")
    else:
        print(
            f"Archived {result['stale_count']} stale-evidence event(s) before removing them from the active queue. "
            "Fresh, undated and future-dated events remain queued; delivery history and dedup state were not modified."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
