from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable

from intelligence.events import IntelligenceEvent, event_from_dict, event_to_dict

MAX_INTELLIGENCE_OUTBOX_ENTRIES = 250
SCHEMA_VERSION = 1


class IntelligenceOutboxError(RuntimeError):
    """Raised when the durable intelligence Discord outbox cannot be trusted."""


@dataclass(frozen=True, slots=True)
class QueuedIntelligence:
    key: str
    event: IntelligenceEvent
    queued_at: datetime


def intelligence_publication_key(event: IntelligenceEvent) -> str:
    return f"intelligence|{event.event_id}"


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("queued_at must be a timestamp string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_record(item: QueuedIntelligence) -> dict[str, Any]:
    return {
        "key": item.key,
        "event": event_to_dict(item.event),
        "queued_at": _stamp(item.queued_at),
    }


def _from_record(value: object) -> QueuedIntelligence:
    if not isinstance(value, dict) or set(value) != {"key", "event", "queued_at"}:
        raise ValueError("intelligence outbox item has an unexpected shape")
    if not isinstance(value["key"], str):
        raise ValueError("intelligence outbox key must be a string")
    return QueuedIntelligence(
        key=value["key"],
        event=event_from_dict(value["event"]),
        queued_at=_parse_stamp(value["queued_at"]),
    )


class JsonIntelligenceOutboxState:
    def __init__(self, path: str | Path = "state/intelligence_outbox.json"):
        self.path = Path(path)

    def load(self) -> list[QueuedIntelligence]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise IntelligenceOutboxError(
                f"Cannot read intelligence outbox {self.path}: {exc}"
            ) from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise IntelligenceOutboxError(
                f"Intelligence outbox {self.path} contains malformed JSON: {exc}"
            ) from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "items"}:
            raise IntelligenceOutboxError(
                f"Intelligence outbox {self.path} has an unexpected shape"
            )
        if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["items"], list):
            raise IntelligenceOutboxError(
                f"Intelligence outbox {self.path} has an unsupported schema"
            )
        try:
            return [_from_record(item) for item in value["items"]]
        except (TypeError, ValueError) as exc:
            raise IntelligenceOutboxError(
                f"Intelligence outbox {self.path} contains an invalid item: {exc}"
            ) from exc

    def save(self, items: Iterable[QueuedIntelligence]) -> None:
        unique = {item.key: item for item in items}
        if len(unique) > MAX_INTELLIGENCE_OUTBOX_ENTRIES:
            raise IntelligenceOutboxError(
                f"Intelligence outbox capacity exceeded: {len(unique)} queued events "
                f"would exceed the hard limit of {MAX_INTELLIGENCE_OUTBOX_ENTRIES}; "
                "refusing to drop undelivered intelligence events"
            )
        ordered = sorted(unique.values(), key=lambda item: (item.queued_at, item.key))
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
            raise IntelligenceOutboxError(
                f"Cannot save intelligence outbox {self.path}: {exc}"
            ) from exc

    def enqueue(
        self,
        events: Iterable[IntelligenceEvent],
        *,
        queued_at: datetime | None = None,
    ) -> list[QueuedIntelligence]:
        now = queued_at or datetime.now(timezone.utc)
        current = {item.key: item for item in self.load()}
        for event in events:
            key = intelligence_publication_key(event)
            current[key] = QueuedIntelligence(key=key, event=event, queued_at=now)
        result = list(current.values())
        self.save(result)
        return result

    def remove(self, keys: set[str]) -> list[QueuedIntelligence]:
        remaining = [item for item in self.load() if item.key not in keys]
        self.save(remaining)
        return remaining
