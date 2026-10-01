from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MAX_EVENTS = 500
MAX_HISTORY = 10
_VALID_STATUSES = {"active", "superseded", "stale"}


class IntelligenceStateError(RuntimeError):
    pass


def _event_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"updated_at", "status", "revision", "history", "superseded_by"}
    }


def _payload_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(
        _event_payload(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _migrate(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") == SCHEMA_VERSION:
        return value
    if value.get("schema_version") != 1 or not isinstance(value.get("events"), dict):
        raise IntelligenceStateError("invalid intelligence state schema")

    migrated = copy.deepcopy(value)
    migrated["schema_version"] = SCHEMA_VERSION
    for event in migrated["events"].values():
        event.setdefault("status", "active")
        event.setdefault("revision", 1)
        event.setdefault("history", [])
        event.setdefault("superseded_by", [])
    return migrated


def _validate(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("events"), dict)
        or value.get("schema_version") not in {1, SCHEMA_VERSION}
    ):
        raise IntelligenceStateError("invalid intelligence state schema")

    value = _migrate(value)
    for key, event in value["events"].items():
        if not isinstance(key, str) or not isinstance(event, dict):
            raise IntelligenceStateError("invalid intelligence event record")
        updated_at = event.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise IntelligenceStateError("intelligence event updated_at must be a non-empty string")
        status = event.get("status", "active")
        if status not in _VALID_STATUSES:
            raise IntelligenceStateError("invalid intelligence event status")
        revision = event.get("revision", 1)
        if not isinstance(revision, int) or revision < 1:
            raise IntelligenceStateError("intelligence event revision must be a positive integer")
        history = event.get("history", [])
        if not isinstance(history, list) or len(history) > MAX_HISTORY:
            raise IntelligenceStateError("intelligence event history is invalid")
        superseded_by = event.get("superseded_by", [])
        if not isinstance(superseded_by, list) or any(
            not isinstance(item, str) for item in superseded_by
        ):
            raise IntelligenceStateError("intelligence event superseded_by must be a string list")
    return value


class JsonIntelligenceState:
    def __init__(self, path: str | Path = "state/intelligence.json"):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "events": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return _validate(json.load(handle))
        except (OSError, json.JSONDecodeError, IntelligenceStateError) as exc:
            raise IntelligenceStateError(f"cannot load intelligence state: {exc}") from exc

    @staticmethod
    def upsert(
        state: dict[str, Any],
        event_id: str,
        event: dict[str, Any],
        updated_at: str,
    ) -> None:
        if not isinstance(event_id, str) or not event_id.strip():
            raise IntelligenceStateError("event_id must be a non-empty string")
        if not isinstance(event, dict):
            raise IntelligenceStateError("event must be an object")
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise IntelligenceStateError("updated_at must be a non-empty string")

        existing = state["events"].get(event_id)
        record = dict(event)
        if existing is None:
            revision = 1
            history: list[dict[str, Any]] = []
            status = "active"
            superseded_by: list[str] = []
        else:
            revision = int(existing.get("revision", 1))
            history = list(existing.get("history", []))
            status = existing.get("status", "active")
            superseded_by = list(existing.get("superseded_by", []))
            if _payload_hash(existing) != _payload_hash(record):
                history.append(
                    {
                        "revision": revision,
                        "updated_at": existing.get("updated_at", ""),
                        "status": status,
                        "event": _event_payload(existing),
                    }
                )
                history = history[-MAX_HISTORY:]
                revision += 1

        record["updated_at"] = updated_at
        record["status"] = status
        record["revision"] = revision
        record["history"] = history
        record["superseded_by"] = superseded_by
        state["events"][event_id] = record

    @staticmethod
    def mark_superseded(
        state: dict[str, Any],
        event_id: str,
        replacement_event_id: str,
        updated_at: str,
    ) -> None:
        if not isinstance(replacement_event_id, str) or not replacement_event_id.strip():
            raise IntelligenceStateError("replacement_event_id must be a non-empty string")
        existing = state["events"].get(event_id)
        if existing is None:
            return

        replacements = list(existing.get("superseded_by", []))
        if existing.get("status") == "superseded" and replacement_event_id in replacements:
            existing["updated_at"] = updated_at
            return

        history = list(existing.get("history", []))
        history.append(
            {
                "revision": existing.get("revision", 1),
                "updated_at": existing.get("updated_at", ""),
                "status": existing.get("status", "active"),
                "event": _event_payload(existing),
            }
        )
        existing["history"] = history[-MAX_HISTORY:]
        existing["revision"] = int(existing.get("revision", 1)) + 1
        existing["status"] = "superseded"
        existing["updated_at"] = updated_at
        replacements = list(existing.get("superseded_by", []))
        if replacement_event_id not in replacements:
            replacements.append(replacement_event_id)
        existing["superseded_by"] = replacements

    @staticmethod
    def mark_stale(
        state: dict[str, Any],
        event_id: str,
        updated_at: str,
    ) -> None:
        existing = state["events"].get(event_id)
        if existing is None:
            return
        existing["status"] = "stale"
        existing["updated_at"] = updated_at

    @staticmethod
    def remove(state: dict[str, Any], event_id: str) -> None:
        # Retained for compatibility with callers that explicitly want hard deletion.
        state["events"].pop(event_id, None)

    def save(self, state: dict[str, Any]) -> None:
        state = _validate(state)
        events = state["events"]
        if len(events) > MAX_EVENTS:
            ordered = sorted(
                events.items(),
                key=lambda pair: str(pair[1].get("updated_at", "")),
                reverse=True,
            )[:MAX_EVENTS]
            state["events"] = dict(sorted(ordered, key=lambda pair: pair[0]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise IntelligenceStateError(f"cannot save intelligence state: {exc}") from exc
