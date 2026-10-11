from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


DEFAULT_CONTEXT_PATH = Path("config/historical_context.json")
MAX_CONTEXT_RECORDS = 4


class HistoricalContextError(ValueError):
    """Raised when the curated historical-context pack is invalid."""


def load_historical_context(path: str | Path = DEFAULT_CONTEXT_PATH) -> list[dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalContextError(f"Cannot load historical context {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HistoricalContextError("historical context must be a schema_version 1 object")
    records = value.get("records")
    if not isinstance(records, list):
        raise HistoricalContextError("historical context records must be a list")
    required = ("id", "date", "entities", "aliases", "relationship", "classification", "status", "source_url")
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or any(not isinstance(record.get(key), str) or not record[key].strip()
                                               for key in ("id", "date", "relationship", "classification", "status", "source_url")):
            raise HistoricalContextError("each historical context record requires string identity, date, relationship, classification, status, and source_url fields")
        if record["id"] in seen_ids:
            raise HistoricalContextError(f"duplicate historical context id: {record['id']}")
        seen_ids.add(record["id"])
        for key in ("entities", "aliases"):
            if not isinstance(record.get(key), list) or not record[key] or any(not isinstance(item, str) or not item.strip() for item in record[key]):
                raise HistoricalContextError(f"historical context {record['id']} field {key} must be a non-empty string list")
        if not record["source_url"].startswith("https://"):
            raise HistoricalContextError(f"historical context {record['id']} source_url must use HTTPS")
    return records


def select_historical_context(
    title: str,
    summary: str,
    path: str | Path = DEFAULT_CONTEXT_PATH,
    max_records: int = MAX_CONTEXT_RECORDS,
) -> list[dict[str, Any]]:
    """Return only sourced records whose aliases occur in the current item.

    Context is historical background, never evidence that a relationship remains active.
    """
    if max_records < 0:
        raise ValueError("max_records must be non-negative")
    text = f"{title}\n{summary}"
    records = load_historical_context(path)
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for record in records:
        matched = {
            alias.casefold()
            for alias in record["aliases"]
            if re.search(r"(?<!\\w)" + re.escape(alias) + r"(?!\\w)", text, flags=re.IGNORECASE)
        }
        if matched:
            matches.append((-len(matched), record["date"], record))
    matches.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    return [record for _, _, record in matches[:max_records]]


def format_historical_context(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    lines = [
        "HISTORICAL BACKGROUND (curated, source-linked; not evidence for current-event claims):"
    ]
    for record in records:
        lines.append(
            f"- [{record['date']}] Entities: {', '.join(record['entities'])}. "
            f"Relationship: {record['relationship']} "
            f"Classification: {record['classification']}. "
            f"Status caution: {record['status']}. Source: {record['source_url']}"
        )
    lines.append(
        "Use this background only to recognize historical connections and explain possible significance. "
        "Never describe a historical relationship as currently active unless the current article says so. "
        "Do not use background as evidence for claims about what the current article reports; claims must cite the supplied current source(s). "
        "Distinguish Ripple (company), XRP (digital asset), and XRPL/XRP Ledger (network)."
    )
    return "\n".join(lines)
