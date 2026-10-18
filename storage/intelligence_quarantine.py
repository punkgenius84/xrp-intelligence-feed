from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterable, Any

from intelligence.events import IntelligenceEvent, event_from_dict, event_to_dict
from storage.intelligence_outbox import QueuedIntelligence

MAX_QUARANTINE_ENTRIES = 1_000
SCHEMA_VERSION = 1


class IntelligenceQuarantineError(RuntimeError):
    """Raised when stale intelligence cannot be safely preserved for review."""


@dataclass(frozen=True, slots=True)
class QuarantinedIntelligence:
    key: str
    event: IntelligenceEvent
    queued_at: datetime
    quarantined_at: datetime
    reason: str


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timestamp string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_record(item: QuarantinedIntelligence) -> dict[str, Any]:
    return {
        "key": item.key,
        "event": event_to_dict(item.event),
        "queued_at": _stamp(item.queued_at),
        "quarantined_at": _stamp(item.quarantined_at),
        "reason": item.reason,
    }


def _from_record(value: object) -> QuarantinedIntelligence:
    expected = {"key", "event", "queued_at", "quarantined_at", "reason"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("quarantine item has an unexpected shape")
    if not isinstance(value["key"], str) or not value["key"].strip():
        raise ValueError("key must be a non-empty string")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise ValueError("reason must be a non-empty string")
    return QuarantinedIntelligence(
        key=value["key"],
        event=event_from_dict(value["event"]),
        queued_at=_parse_stamp(value["queued_at"], "queued_at"),
        quarantined_at=_parse_stamp(value["quarantined_at"], "quarantined_at"),
        reason=value["reason"],
    )


class JsonIntelligenceQuarantineState:
    """Durable, auditable holding area for stale events excluded from publication."""

    def __init__(self, path: str | Path = "state/intelligence_quarantine.json"):
        self.path = Path(path)

    def load(self) -> list[QuarantinedIntelligence]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise IntelligenceQuarantineError(
                f"Cannot read intelligence quarantine {self.path}: {exc}"
            ) from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise IntelligenceQuarantineError(
                f"Intelligence quarantine {self.path} contains malformed JSON: {exc}"
            ) from exc
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "items"}
                or value.get("schema_version") != SCHEMA_VERSION
                or not isinstance(value.get("items"), list)):
            raise IntelligenceQuarantineError(
                f"Intelligence quarantine {self.path} has an unsupported shape or schema"
            )
        try:
            records = [_from_record(item) for item in value["items"]]
        except (TypeError, ValueError) as exc:
            raise IntelligenceQuarantineError(
                f"Intelligence quarantine {self.path} contains an invalid item: {exc}"
            ) from exc
        if len({item.key for item in records}) != len(records):
            raise IntelligenceQuarantineError(
                f"Intelligence quarantine {self.path} contains duplicate keys"
            )
        return records

    def quarantine(
        self,
        items: Iterable[QueuedIntelligence],
        *,
        reason: str,
        quarantined_at: datetime | None = None,
    ) -> list[QuarantinedIntelligence]:
        if not isinstance(reason, str) or not reason.strip():
            raise IntelligenceQuarantineError("quarantine reason must be non-empty")
        now = quarantined_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        current = {item.key: item for item in self.load()}
        for queued in items:
            if not queued.key.strip():
                raise IntelligenceQuarantineError("queued item key must be non-empty")
            # Preserve the first quarantined copy as an audit record; retries are idempotent.
            current.setdefault(
                queued.key,
                QuarantinedIntelligence(
                    key=queued.key,
                    event=queued.event,
                    queued_at=queued.queued_at,
                    quarantined_at=now,
                    reason=reason.strip(),
                ),
            )
        if len(current) > MAX_QUARANTINE_ENTRIES:
            raise IntelligenceQuarantineError(
                f"Quarantine capacity exceeded: {len(current)} entries would exceed "
                f"the hard limit of {MAX_QUARANTINE_ENTRIES}; refusing to discard records"
            )
        ordered = sorted(current.values(), key=lambda item: (item.quarantined_at, item.key))
        payload = {
            "schema_version": SCHEMA_VERSION,
            "items": [_to_record(item) for item in ordered],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise IntelligenceQuarantineError(
                f"Cannot save intelligence quarantine {self.path}: {exc}"
            ) from exc
        return ordered
