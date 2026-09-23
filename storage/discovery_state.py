from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any


MAX_DISCOVERY_CANDIDATES = 10_000
DISCOVERY_RETENTION_DAYS = 90
STATE_SCHEMA_VERSION = 1


class DiscoveryStateError(RuntimeError):
    """Raised when persisted discovery state cannot safely be used or written."""


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def empty_discovery_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "sources": {}, "candidates": {}}


class JsonDiscoveryState:
    def __init__(self, path: str | Path = "state/discovery.json", *,
                 max_candidates: int = MAX_DISCOVERY_CANDIDATES,
                 retention_days: int = DISCOVERY_RETENTION_DAYS):
        if max_candidates < 1 or retention_days < 1:
            raise ValueError("discovery-state bounds must be positive")
        self.path = Path(path)
        self.max_candidates = max_candidates
        self.retention_days = retention_days

    @staticmethod
    def _validate(value: object) -> dict[str, Any]:
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "sources", "candidates"}
                or value.get("schema_version") != STATE_SCHEMA_VERSION
                or not isinstance(value.get("sources"), dict)
                or not isinstance(value.get("candidates"), dict)):
            raise DiscoveryStateError("Discovery state has an unexpected shape or schema version")
        for source_id, source in value["sources"].items():
            if not isinstance(source_id, str) or not source_id or not isinstance(source, dict):
                raise DiscoveryStateError("Discovery state sources must map IDs to objects")
            if set(source) - {"requests", "last_successful_fetch", "watermark", "pagination"}:
                raise DiscoveryStateError(f"Discovery state source {source_id!r} has unknown fields")
            requests = source.get("requests", {})
            if not isinstance(requests, dict):
                raise DiscoveryStateError(f"Discovery state source {source_id!r}.requests must be an object")
            for request_id, validators in requests.items():
                if (not isinstance(request_id, str) or not isinstance(validators, dict)
                        or set(validators) - {"etag", "last_modified"}
                        or any(not isinstance(v, str) for v in validators.values())):
                    raise DiscoveryStateError(f"Discovery state source {source_id!r} has invalid validators")
            last_success = source.get("last_successful_fetch")
            if last_success is not None and not _valid_timestamp(last_success):
                raise DiscoveryStateError(f"Discovery state source {source_id!r} has invalid last_successful_fetch")
            for field in ("watermark", "pagination"):
                if field in source and not isinstance(source[field], (dict, str, int, float, type(None))):
                    raise DiscoveryStateError(f"Discovery state source {source_id!r}.{field} has invalid shape")
        for candidate_id, candidate in value["candidates"].items():
            if (not isinstance(candidate_id, str) or not candidate_id
                    or not isinstance(candidate, dict)
                    or set(candidate) != {"content_hash", "first_seen_time", "last_seen_time"}
                    or not isinstance(candidate["content_hash"], str)
                    or not _valid_timestamp(candidate["first_seen_time"])
                    or not _valid_timestamp(candidate["last_seen_time"])):
                raise DiscoveryStateError("Discovery state contains an invalid candidate record")
        return value

    def load(self) -> dict[str, Any]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_discovery_state()
        except (OSError, UnicodeError) as exc:
            raise DiscoveryStateError(f"Cannot read discovery state {self.path}: {exc}") from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DiscoveryStateError(f"Discovery state {self.path} contains malformed JSON: {exc}") from exc
        return self._validate(value)

    def save(self, value: dict[str, Any], *, now: datetime | None = None) -> None:
        # Refuse to replace a corrupt on-disk file even if the caller has an in-memory value.
        self.load()
        validated = self._validate(value)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        cutoff = current.astimezone(timezone.utc) - timedelta(days=self.retention_days)
        retained: list[tuple[str, dict[str, str]]] = []
        for candidate_id, candidate in validated["candidates"].items():
            last_seen = datetime.fromisoformat(candidate["last_seen_time"].replace("Z", "+00:00"))
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            if last_seen.astimezone(timezone.utc) >= cutoff:
                retained.append((candidate_id, candidate))
        retained.sort(key=lambda entry: (
            datetime.fromisoformat(entry[1]["last_seen_time"].replace("Z", "+00:00")).astimezone(timezone.utc),
            entry[0],
        ))
        retained = retained[-self.max_candidates:]
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "sources": {key: validated["sources"][key] for key in sorted(validated["sources"])},
            "candidates": {key: item for key, item in retained},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise DiscoveryStateError(f"Cannot atomically save discovery state {self.path}: {exc}") from exc

    @staticmethod
    def request_validators(state: dict[str, Any], source_id: str,
                           request_id: str) -> dict[str, str]:
        source = state.get("sources", {}).get(source_id, {})
        return dict(source.get("requests", {}).get(request_id, {}))

    @staticmethod
    def record_success(state: dict[str, Any], source_id: str, fetched_at: datetime,
                       request_updates: dict[str, dict[str, str]], *,
                       watermark: dict[str, str] | None = None,
                       pagination: dict[str, Any] | None = None) -> None:
        source = state["sources"].setdefault(source_id, {"requests": {}})
        requests_state = source.setdefault("requests", {})
        for request_id, validators in request_updates.items():
            requests_state[request_id] = {
                key: validators[key] for key in ("etag", "last_modified") if validators.get(key)
            }
        source["last_successful_fetch"] = fetched_at.astimezone(timezone.utc).isoformat()
        if watermark:
            previous = source.get("watermark", {})
            combined = dict(previous) if isinstance(previous, dict) else {}
            combined.update(watermark)
            source["watermark"] = dict(sorted(combined.items()))
        if pagination:
            previous_pagination = source.get("pagination", {})
            combined_pagination = dict(previous_pagination) if isinstance(previous_pagination, dict) else {}
            combined_pagination.update(pagination)
            source["pagination"] = dict(sorted(combined_pagination.items()))

    @staticmethod
    def observe_candidate(state: dict[str, Any], candidate_id: str, content_hash: str,
                          observed_at: datetime) -> None:
        timestamp = observed_at.astimezone(timezone.utc).isoformat()
        previous = state["candidates"].get(candidate_id)
        state["candidates"][candidate_id] = {
            "content_hash": content_hash,
            "first_seen_time": previous["first_seen_time"] if previous else timestamp,
            "last_seen_time": timestamp,
        }
