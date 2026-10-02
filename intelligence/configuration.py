import json
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when an intelligence configuration file is invalid."""


def read_object(path: str | Path, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot load {label} configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} configuration must be a JSON object")
    return value


def validate_word_groups(value: dict, required: tuple[str, ...], label: str) -> dict:
    missing = [key for key in required if key not in value]
    if missing:
        raise ConfigurationError(f"{label} configuration missing required fields: {', '.join(missing)}")
    for group, entries in value.items():
        if not isinstance(entries, dict) or not entries:
            raise ConfigurationError(f"{label}.{group} must be a non-empty object")
        for canonical, aliases in entries.items():
            if not isinstance(canonical, str) or not canonical.strip():
                raise ConfigurationError(f"{label}.{group} entity names must be non-empty strings")
            if (not isinstance(aliases, list) or not aliases
                    or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)):
                raise ConfigurationError(f"{label}.{group}.{canonical} must contain non-empty string aliases")
    return value


def validate_keyword_groups(value: dict) -> dict:
    required = ("high", "medium", "context")
    missing = [key for key in required if key not in value]
    if missing:
        raise ConfigurationError(f"keywords configuration missing required fields: {', '.join(missing)}")
    for group in required:
        entries = value[group]
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item.strip() for item in entries):
            raise ConfigurationError(f"keywords.{group} must be a list of non-empty strings")
    return value


REQUIRED_WEIGHTS = (
    "high_keyword", "medium_keyword", "context_keyword",
    "primary_source_bonus", "multiple_entity_bonus", "title_match_bonus", "institutional_source_bonus",
)


def validate_thresholds(value: dict) -> dict:
    threshold = value.get("publish_score")
    if type(threshold) is not int or not 0 <= threshold <= 100:
        raise ConfigurationError("thresholds.publish_score must be an integer from 0 to 100")
    weights = value.get("weights")
    if not isinstance(weights, dict):
        raise ConfigurationError("thresholds.weights must be an object")
    missing = [key for key in REQUIRED_WEIGHTS if key not in weights]
    if missing:
        raise ConfigurationError(f"thresholds.weights missing required fields: {', '.join(missing)}")
    for key in REQUIRED_WEIGHTS:
        if type(weights[key]) is not int or weights[key] < 0:
            raise ConfigurationError(f"thresholds.weights.{key} must be a non-negative integer")
    return value


# Optional local intelligence runtime settings. These are intentionally separate from
# source/relevance configuration so enabling the LLM never changes discovery scoring.
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntelligenceRuntimeConfig:
    enabled: bool = False
    publish_enabled: bool = False
    model: str = "qwen2.5:7b"
    max_items_per_run: int = 5
    min_relevance_score: int = 50
    timeout_seconds: float = 45.0
    stale_after_days: int = 30


def load_intelligence_config() -> IntelligenceRuntimeConfig:
    def boolean(value: str | None) -> bool:
        return (value or "").strip().lower() in {"1", "true", "yes", "on"}

    max_items = int(os.getenv("INTELLIGENCE_MAX_ITEMS", "5"))
    minimum = int(os.getenv("INTELLIGENCE_MIN_SCORE", "50"))
    timeout = float(os.getenv("INTELLIGENCE_TIMEOUT", "45"))
    if not 0 <= max_items <= 50:
        raise ConfigurationError("INTELLIGENCE_MAX_ITEMS must be between 0 and 50")
    if not 0 <= minimum <= 100:
        raise ConfigurationError("INTELLIGENCE_MIN_SCORE must be between 0 and 100")
    if not 5 <= timeout <= 180:
        raise ConfigurationError("INTELLIGENCE_TIMEOUT must be between 5 and 180 seconds")
    return IntelligenceRuntimeConfig(
        enabled=boolean(os.getenv("INTELLIGENCE_ENABLED")),
        publish_enabled=boolean(os.getenv("INTELLIGENCE_PUBLISH")),
        model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b",
        max_items_per_run=max_items,
        min_relevance_score=minimum,
        timeout_seconds=timeout,
    )
