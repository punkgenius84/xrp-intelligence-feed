from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

MAX_HISTORY = 2_000
RETENTION_DAYS = 35
SCHEMA_VERSION = 1


class DeliveryHistoryError(RuntimeError):
    """Raised when posted Discord delivery history cannot be trusted."""


@dataclass(frozen=True, slots=True)
class PostedItem:
    key: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    posted_at: datetime
    relevance_score: int
    score_reason: str


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


class JsonDeliveryHistory:
    def __init__(self, path: str | Path = "state/delivery_history.json"):
        self.path = Path(path)

    def load(self) -> list[PostedItem]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise DeliveryHistoryError(f"Cannot read delivery history: {exc}") from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeliveryHistoryError(f"Delivery history contains malformed JSON: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise DeliveryHistoryError("Delivery history has an unsupported schema")
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            raise DeliveryHistoryError("Delivery history items must be a list")
        result = []
        try:
            for item in raw_items:
                if not isinstance(item, dict):
                    raise ValueError("history item must be an object")
                result.append(
                    PostedItem(
                        key=str(item["key"]),
                        source=str(item["source"]),
                        title=str(item["title"]),
                        url=str(item["url"]),
                        published_at=_parse_stamp(item.get("published_at")),
                        posted_at=_parse_stamp(item["posted_at"]),
                        relevance_score=int(item["relevance_score"]),
                        score_reason=str(item.get("score_reason", "")),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise DeliveryHistoryError(f"Invalid delivery history item: {exc}") from exc
        if any(item.posted_at is None for item in result):
            raise DeliveryHistoryError("Delivery history posted_at is required")
        return [item for item in result if item.posted_at is not None]

    def record(self, items: list[Any], posted_keys: set[str], *, posted_at: datetime | None = None) -> None:
        now = posted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current = self.load()
        existing = {item.key for item in current}
        for item in items:
            key = getattr(item, "candidate_id", "") or getattr(item, "fingerprint", "")
            content_hash = getattr(item, "content_hash", "") or getattr(item, "fingerprint", "")
            publication_key = f"discord|{key}|{content_hash}"
            if publication_key not in posted_keys or publication_key in existing:
                continue
            current.append(
                PostedItem(
                    key=publication_key,
                    source=str(item.source),
                    title=str(item.title),
                    url=str(item.url),
                    published_at=item.published_at,
                    posted_at=now,
                    relevance_score=int(item.relevance_score),
                    score_reason=str(item.score_reasons[0]) if item.score_reasons else "",
                )
            )
        self.save(current)

    def save(self, items: list[PostedItem], *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=RETENTION_DAYS)
        retained = [
            item for item in items
            if item.posted_at.astimezone(timezone.utc) >= cutoff
        ]
        retained.sort(key=lambda item: (item.posted_at, item.key))
        retained = retained[-MAX_HISTORY:]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "items": [
                {
                    "key": item.key,
                    "source": item.source,
                    "title": item.title,
                    "url": item.url,
                    "published_at": _stamp(item.published_at),
                    "posted_at": _stamp(item.posted_at),
                    "relevance_score": item.relevance_score,
                    "score_reason": item.score_reason,
                }
                for item in retained
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DeliveryHistoryError(f"Cannot save delivery history: {exc}") from exc
