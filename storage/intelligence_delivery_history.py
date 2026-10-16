from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

MAX_HISTORY_ENTRIES = 2_000
RETENTION_DAYS = 35
SCHEMA_VERSION = 1


class IntelligenceDeliveryHistoryError(RuntimeError):
    """Raised when intelligence delivery history cannot be trusted."""


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("posted_at must be a timestamp string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class JsonIntelligenceDeliveryHistory:
    """Durable short-retention record of intelligence events already sent to Discord."""

    def __init__(self, path: str | Path = "state/intelligence_delivery_history.json"):
        self.path = Path(path)

    def _load_records(self) -> list[dict[str, str]]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise IntelligenceDeliveryHistoryError(
                f"Cannot read intelligence delivery history: {exc}"
            ) from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise IntelligenceDeliveryHistoryError(
                f"Intelligence delivery history contains malformed JSON: {exc}"
            ) from exc
        if (not isinstance(value, dict) or set(value) != {"schema_version", "items"}
                or value.get("schema_version") != SCHEMA_VERSION
                or not isinstance(value.get("items"), list)):
            raise IntelligenceDeliveryHistoryError(
                "Intelligence delivery history has an unsupported shape or schema"
            )
        records: list[dict[str, str]] = []
        try:
            for item in value["items"]:
                if not isinstance(item, dict) or set(item) != {"event_id", "posted_at"}:
                    raise ValueError("history item has an unexpected shape")
                event_id = item["event_id"]
                if not isinstance(event_id, str) or not event_id.strip():
                    raise ValueError("event_id must be a non-empty string")
                posted_at = _parse_stamp(item["posted_at"])
                records.append({"event_id": event_id, "posted_at": _stamp(posted_at)})
        except (TypeError, ValueError) as exc:
            raise IntelligenceDeliveryHistoryError(
                f"Intelligence delivery history contains an invalid item: {exc}"
            ) from exc
        return records

    def load(self, *, now: datetime | None = None) -> set[str]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=RETENTION_DAYS)
        return {
            item["event_id"] for item in self._load_records()
            if _parse_stamp(item["posted_at"]) >= cutoff
        }

    def record(self, event_ids: set[str], *, posted_at: datetime | None = None) -> None:
        if not event_ids:
            return
        now = posted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(days=RETENTION_DAYS)
        current = {
            item["event_id"]: item
            for item in self._load_records()
            if _parse_stamp(item["posted_at"]) >= cutoff
        }
        for event_id in event_ids:
            if not isinstance(event_id, str) or not event_id.strip():
                raise IntelligenceDeliveryHistoryError("event_id must be a non-empty string")
            current.setdefault(event_id, {"event_id": event_id, "posted_at": _stamp(now)})
        ordered = sorted(current.values(), key=lambda item: (item["posted_at"], item["event_id"]))
        ordered = ordered[-MAX_HISTORY_ENTRIES:]
        payload = {"schema_version": SCHEMA_VERSION, "items": ordered}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise IntelligenceDeliveryHistoryError(
                f"Cannot save intelligence delivery history: {exc}"
            ) from exc
