from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

REQUIRED_FIELDS = {
    "source_id", "name", "authority_tier", "category", "url",
    "entity_coverage", "enabled", "collection_type",
}


class SourceRegistryError(ValueError):
    """Raised when source registry configuration is invalid."""


def validate_sources(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "sources"}:
        raise SourceRegistryError("Registry must contain schema_version and sources only")
    if payload["schema_version"] != 1:
        raise SourceRegistryError("Unsupported source registry schema_version")
    sources = payload["sources"]
    if not isinstance(sources, list):
        raise SourceRegistryError("sources must be a list")

    seen_ids: set[str] = set()
    for index, source in enumerate(sources):
        label = f"sources[{index}]"
        if not isinstance(source, dict) or set(source) != REQUIRED_FIELDS:
            raise SourceRegistryError(f"{label} must have exactly {sorted(REQUIRED_FIELDS)}")
        if not all(isinstance(source[key], str) and source[key].strip()
                   for key in ("source_id", "name", "category", "url", "collection_type")):
            raise SourceRegistryError(f"{label} text fields must be non-empty strings")
        if source["source_id"] in seen_ids:
            raise SourceRegistryError(f"duplicate source_id: {source['source_id']}")
        seen_ids.add(source["source_id"])
        if type(source["authority_tier"]) is not int or source["authority_tier"] not in (1, 2, 3):
            raise SourceRegistryError(f"{label}.authority_tier must be 1, 2, or 3")
        if type(source["enabled"]) is not bool:
            raise SourceRegistryError(f"{label}.enabled must be boolean")
        if not isinstance(source["entity_coverage"], list) or not source["entity_coverage"]:
            raise SourceRegistryError(f"{label}.entity_coverage must be a non-empty list")
        if any(not isinstance(entity, str) or not entity.strip()
               for entity in source["entity_coverage"]):
            raise SourceRegistryError(f"{label}.entity_coverage values must be non-empty strings")
        parsed = urlparse(source["url"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SourceRegistryError(f"{label}.url must be an absolute HTTP(S) URL")
        if source["collection_type"] != "rss":
            raise SourceRegistryError(f"{label}.collection_type is not supported")
    return sources


def load_registry(path: str | Path = "config/sources.json") -> list[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"Cannot load source registry {path}: {exc}") from exc
    return validate_sources(payload)


def enabled_sources(path: str | Path = "config/sources.json") -> list[dict]:
    return [source for source in load_registry(path) if source["enabled"]]
