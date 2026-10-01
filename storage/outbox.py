from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable

MAX_OUTBOX_ENTRIES = 250
SCHEMA_VERSION = 1


class OutboxError(RuntimeError):
    """Raised when the persistent Discord delivery outbox cannot be trusted."""


@dataclass(slots=True)
class QueuedPublication:
    key: str
    candidate_id: str
    content_hash: str
    title: str
    url: str
    source: str
    published_at: datetime | None
    collected_at: datetime
    relevance_score: int
    detected_entities: list[str]
    score_reasons: list[str]


def publication_key(item: Any) -> str:
    identity = getattr(item, "candidate_id", "") or getattr(item, "fingerprint", "")
    content_hash = getattr(item, "content_hash", "") or getattr(item, "fingerprint", "")
    return f"discord|{identity}|{content_hash}"


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string or null")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_record(item: Any) -> dict[str, Any]:
    return {
        "key": publication_key(item),
        "candidate_id": str(getattr(item, "candidate_id", "") or ""),
        "content_hash": str(getattr(item, "content_hash", "") or ""),
        "title": str(item.title),
        "url": str(item.url),
        "source": str(item.source),
        "published_at": _stamp(item.published_at),
        "collected_at": _stamp(item.collected_at),
        "relevance_score": int(item.relevance_score),
        "detected_entities": list(item.detected_entities),
        "score_reasons": list(item.score_reasons),
    }


def from_record(value: object) -> QueuedPublication:
    if not isinstance(value, dict):
        raise ValueError("outbox item must be an object")
    required = {
        "key", "candidate_id", "content_hash", "title", "url", "source",
        "published_at", "collected_at", "relevance_score",
        "detected_entities", "score_reasons",
    }
    if set(value) != required:
        raise ValueError("outbox item has an unexpected shape")
    if not all(isinstance(value[key], str) for key in ("key", "candidate_id", "content_hash", "title", "url", "source")):
        raise ValueError("outbox item string fields are invalid")
    if type(value["relevance_score"]) is not int or not 0 <= value["relevance_score"] <= 100:
        raise ValueError("outbox relevance_score is invalid")
    for key in ("detected_entities", "score_reasons"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError(f"outbox {key} is invalid")
    published_at = _parse_stamp(value["published_at"])
    collected_at = _parse_stamp(value["collected_at"])
    if collected_at is None:
        raise ValueError("outbox collected_at is required")
    return QueuedPublication(
        key=value["key"],
        candidate_id=value["candidate_id"],
        content_hash=value["content_hash"],
        title=value["title"],
        url=value["url"],
        source=value["source"],
        published_at=published_at,
        collected_at=collected_at,
        relevance_score=value["relevance_score"],
        detected_entities=list(value["detected_entities"]),
        score_reasons=list(value["score_reasons"]),
    )


class JsonOutboxState:
    def __init__(self, path: str | Path = "state/outbox.json"):
        self.path = Path(path)

    def _read(self) -> list[QueuedPublication]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise OutboxError(f"Cannot read Discord outbox {self.path}: {exc}") from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OutboxError(f"Discord outbox {self.path} contains malformed JSON: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "items"}:
            raise OutboxError(f"Discord outbox {self.path} has an unexpected shape")
        if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["items"], list):
            raise OutboxError(f"Discord outbox {self.path} has an unsupported schema")
        try:
            return [from_record(item) for item in value["items"]]
        except (TypeError, ValueError) as exc:
            raise OutboxError(f"Discord outbox {self.path} contains an invalid item: {exc}") from exc

    def load(self) -> list[QueuedPublication]:
        return self._read()

    def save(self, items: Iterable[QueuedPublication]) -> None:
        unique = {item.key: item for item in items}
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item.published_at or item.collected_at,
                item.key,
            ),
        )[-MAX_OUTBOX_ENTRIES:]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "items": [
                {
                    "key": item.key,
                    "candidate_id": item.candidate_id,
                    "content_hash": item.content_hash,
                    "title": item.title,
                    "url": item.url,
                    "source": item.source,
                    "published_at": _stamp(item.published_at),
                    "collected_at": _stamp(item.collected_at),
                    "relevance_score": item.relevance_score,
                    "detected_entities": item.detected_entities,
                    "score_reasons": item.score_reasons,
                }
                for item in ordered
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise OutboxError(f"Cannot save Discord outbox {self.path}: {exc}") from exc

    def enqueue(self, items: Iterable[Any]) -> list[QueuedPublication]:
        current = {item.key: item for item in self._read()}
        for item in items:
            record = from_record(to_record(item))
            current[record.key] = record
        result = list(current.values())
        self.save(result)
        return result

    def remove(self, keys: set[str]) -> list[QueuedPublication]:
        remaining = [item for item in self._read() if item.key not in keys]
        self.save(remaining)
        return remaining
