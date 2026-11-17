from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

MAX_HISTORY_ENTRIES = 2_000
RETENTION_DAYS = 35
SIGNATURE_RETENTION_HOURS = 72
SCHEMA_VERSION = 2
_GENERIC_TITLES = {"read more", "click here", "learn more", "home", "article", "news"}


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


def event_signature_keys(event: object) -> set[str]:
    """Create conservative exact-title fingerprints; never fuzzy-match follow-up headlines."""
    signatures: set[str] = set()
    for evidence in getattr(event, "evidence", []) or []:
        title = getattr(evidence, "title", "")
        if not isinstance(title, str):
            continue
        normalized = unicodedata.normalize("NFKC", title).casefold()
        normalized = re.sub(r"https?://\S+", " ", normalized)
        normalized = " ".join(re.findall(r"[\w]+", normalized))
        words = normalized.split()
        if len(words) < 5 or normalized in _GENERIC_TITLES:
            continue
        signatures.add(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
    return signatures


class JsonIntelligenceDeliveryHistory:
    """Durable event-ID and conservative cross-ID duplicate history."""

    def __init__(self, path: str | Path = "state/intelligence_delivery_history.json"):
        self.path = Path(path)

    def _load_records(self) -> list[dict]:
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
        if not isinstance(value, dict) or value.get("schema_version") not in {1, SCHEMA_VERSION} or not isinstance(value.get("items"), list):
            raise IntelligenceDeliveryHistoryError(
                "Intelligence delivery history has an unsupported shape or schema"
            )
        allowed_top = {"schema_version", "items"}
        if value.get("schema_version") == SCHEMA_VERSION:
            allowed_top.add("event_signatures")
            if not isinstance(value.get("event_signatures", []), list):
                raise IntelligenceDeliveryHistoryError("event_signatures must be a list")
        if set(value) != allowed_top:
            raise IntelligenceDeliveryHistoryError("Intelligence delivery history has an unexpected shape")
        records: list[dict] = []
        try:
            for item in value["items"]:
                if not isinstance(item, dict) or set(item) != {"event_id", "posted_at"}:
                    raise ValueError("history item has an unexpected shape")
                event_id = item["event_id"]
                if not isinstance(event_id, str) or not event_id.strip():
                    raise ValueError("event_id must be a non-empty string")
                records.append({"event_id": event_id, "posted_at": _stamp(_parse_stamp(item["posted_at"]))})
            signatures = value.get("event_signatures", [])
            for item in signatures:
                if not isinstance(item, dict) or set(item) != {"fingerprint", "posted_at"}:
                    raise ValueError("event signature has an unexpected shape")
                fingerprint = item["fingerprint"]
                if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                    raise ValueError("event signature fingerprint is invalid")
                records.append({"fingerprint": fingerprint, "posted_at": _stamp(_parse_stamp(item["posted_at"]))})
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
            if "event_id" in item and _parse_stamp(item["posted_at"]) >= cutoff
        }

    def has_duplicate_event(self, event: object, *, now: datetime | None = None) -> bool:
        signatures = event_signature_keys(event)
        if not signatures:
            return False
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(hours=SIGNATURE_RETENTION_HOURS)
        known = {
            item["fingerprint"] for item in self._load_records()
            if "fingerprint" in item and _parse_stamp(item["posted_at"]) >= cutoff
        }
        return bool(signatures & known)

    def record(self, event_ids: set[str], *, posted_at: datetime | None = None) -> None:
        self._write(event_ids, set(), posted_at=posted_at)

    def record_events(self, events: list[object], *, posted_at: datetime | None = None) -> None:
        event_ids = set()
        fingerprints: set[str] = set()
        for event in events:
            event_id = getattr(event, "event_id", None)
            if isinstance(event_id, str) and event_id.strip():
                event_ids.add(event_id)
            fingerprints.update(_event_signatures(event))
        self._write(event_ids, fingerprints, posted_at=posted_at)

    def _write(self, event_ids: set[str], fingerprints: set[str], *, posted_at: datetime | None) -> None:
        if not event_ids and not fingerprints:
            return
        now = posted_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(days=RETENTION_DAYS)
        signature_cutoff = now - timedelta(hours=SIGNATURE_RETENTION_HOURS)
        records = self._load_records()
        current_ids = {
            item["event_id"]: item for item in records
            if "event_id" in item and _parse_stamp(item["posted_at"]) >= cutoff
        }
        current_signatures = {
            item["fingerprint"]: item for item in records
            if "fingerprint" in item and _parse_stamp(item["posted_at"]) >= signature_cutoff
        }
        for event_id in event_ids:
            if not isinstance(event_id, str) or not event_id.strip():
                raise IntelligenceDeliveryHistoryError("event_id must be a non-empty string")
            current_ids.setdefault(event_id, {"event_id": event_id, "posted_at": _stamp(now)})
        for fingerprint in fingerprints:
            current_signatures.setdefault(fingerprint, {"fingerprint": fingerprint, "posted_at": _stamp(now)})
        ordered_ids = sorted(current_ids.values(), key=lambda item: (item["posted_at"], item["event_id"]))[-MAX_HISTORY_ENTRIES:]
        ordered_signatures = sorted(current_signatures.values(), key=lambda item: (item["posted_at"], item["fingerprint"]))[-MAX_HISTORY_ENTRIES:]
        payload = {"schema_version": SCHEMA_VERSION, "items": ordered_ids, "event_signatures": ordered_signatures}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise IntelligenceDeliveryHistoryError(
                f"Cannot save intelligence delivery history: {exc}"
            ) from exc
