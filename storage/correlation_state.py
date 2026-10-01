from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any


MAX_CORRELATION_CARDS = 500
CORRELATION_RETENTION_HOURS = 72
STATE_SCHEMA_VERSION = 1

_CARD_FIELDS = {
    "candidate_id",
    "source_id",
    "published_at",
    "high_value_entities",
    "title_tokens",
    "content_hash",
    "last_seen",
}


class CorrelationStateError(RuntimeError):
    """Raised when persisted cross-run correlation state cannot be trusted or written."""


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def empty_correlation_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "cards": {}}


class JsonCorrelationState:
    def __init__(
        self,
        path: str | Path = "state/correlation.json",
        *,
        max_cards: int = MAX_CORRELATION_CARDS,
        retention_hours: int = CORRELATION_RETENTION_HOURS,
    ):
        if max_cards < 1 or retention_hours < 1:
            raise ValueError("correlation-state bounds must be positive")
        self.path = Path(path)
        self.max_cards = max_cards
        self.retention_hours = retention_hours

    @staticmethod
    def _validate(value: object) -> dict[str, Any]:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "cards"}
            or value.get("schema_version") != STATE_SCHEMA_VERSION
            or not isinstance(value.get("cards"), dict)
        ):
            raise CorrelationStateError(
                "Correlation state has an unexpected shape or schema version"
            )

        for candidate_id, card in value["cards"].items():
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(card, dict)
                or set(card) != _CARD_FIELDS
                or card.get("candidate_id") != candidate_id
                or not isinstance(card["source_id"], str)
                or not card["source_id"]
                or not _valid_timestamp(card["published_at"])
                or not isinstance(card["high_value_entities"], list)
                or any(not isinstance(item, str) for item in card["high_value_entities"])
                or not isinstance(card["title_tokens"], list)
                or any(not isinstance(item, str) for item in card["title_tokens"])
                or not isinstance(card["content_hash"], str)
                or not _valid_timestamp(card["last_seen"])
            ):
                raise CorrelationStateError("Correlation state contains an invalid card")

        return value

    def load(self) -> dict[str, Any]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_correlation_state()
        except (OSError, UnicodeError) as exc:
            raise CorrelationStateError(
                f"Cannot read correlation state {self.path}: {exc}"
            ) from exc

        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CorrelationStateError(
                f"Correlation state {self.path} contains malformed JSON: {exc}"
            ) from exc
        return self._validate(value)

    def save(self, value: dict[str, Any], *, now: datetime | None = None) -> None:
        self.load()
        validated = self._validate(value)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        cutoff = current - timedelta(hours=self.retention_hours)

        retained: list[tuple[str, dict[str, Any]]] = []
        for candidate_id, card in validated["cards"].items():
            published_at = datetime.fromisoformat(
                card["published_at"].replace("Z", "+00:00")
            )
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if published_at.astimezone(timezone.utc) >= cutoff:
                retained.append((candidate_id, card))

        retained.sort(
            key=lambda entry: (
                datetime.fromisoformat(
                    entry[1]["last_seen"].replace("Z", "+00:00")
                ).astimezone(timezone.utc),
                entry[0],
            )
        )
        retained = retained[-self.max_cards :]

        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "cards": {key: card for key, card in retained},
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
            raise CorrelationStateError(
                f"Cannot atomically save correlation state {self.path}: {exc}"
            ) from exc

    @staticmethod
    def upsert_card(
        state: dict[str, Any],
        *,
        candidate_id: str,
        source_id: str,
        published_at: datetime,
        high_value_entities: list[str],
        title_tokens: list[str],
        content_hash: str,
        last_seen: datetime,
    ) -> None:
        if not candidate_id:
            raise ValueError("correlation card candidate_id cannot be empty")
        if not source_id:
            raise ValueError("correlation card source_id cannot be empty")
        state["cards"][candidate_id] = {
            "candidate_id": candidate_id,
            "source_id": source_id,
            "published_at": published_at.astimezone(timezone.utc).isoformat(),
            "high_value_entities": sorted(set(high_value_entities)),
            "title_tokens": sorted(set(title_tokens)),
            "content_hash": content_hash,
            "last_seen": last_seen.astimezone(timezone.utc).isoformat(),
        }


    @staticmethod
    def touch_card(state: dict[str, Any], *, candidate_id: str, last_seen: datetime) -> None:
        """Refresh observation time without changing the bounded card contents."""
        card = state["cards"].get(candidate_id)
        if card is None:
            return
        card["last_seen"] = last_seen.astimezone(timezone.utc).isoformat()
