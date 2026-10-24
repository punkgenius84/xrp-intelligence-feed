from __future__ import annotations

import json
import re
from pathlib import Path
from datetime import datetime, timezone
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
            if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, flags=re.IGNORECASE)
        }
        if matched:
            matches.append((-len(matched), record["date"], record))
    matches.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    return [record for _, _, record in matches[:max_records]]



_PRIOR_CONTEXT_STOPWORDS = {
    "about", "after", "again", "also", "among", "announced", "announcement",
    "before", "being", "could", "from", "have", "into", "more", "over",
    "reported", "reports", "said", "says", "that", "their", "there", "these",
    "they", "this", "those", "through", "under", "using", "with", "would",
}


def select_prior_event_context(
    title: str,
    summary: str,
    events: dict[str, Any] | None,
    *,
    exclude_urls: set[str] | None = None,
    max_records: int = 3,
) -> list[dict[str, Any]]:
    """Select relevant, source-linked prior intelligence as context, not evidence.

    Superseded and stale events remain eligible as historical background. Their
    lifecycle status is preserved so a past event is never presented as current.
    """
    if max_records < 0:
        raise ValueError("max_records must be non-negative")
    if not isinstance(events, dict) or max_records == 0:
        return []

    current_text = f"{title}\n{summary}"
    current_lower = current_text.casefold()
    current_tokens = {
        token for token in re.findall(r"[a-z0-9]+", current_lower)
        if len(token) > 2 and token not in _PRIOR_CONTEXT_STOPWORDS
    }
    excluded = {url.strip() for url in (exclude_urls or set()) if isinstance(url, str)}
    matches: list[tuple[int, str, str, dict[str, Any]]] = []

    for event_id, record in events.items():
        if not isinstance(event_id, str) or not isinstance(record, dict):
            continue
        status = record.get("status", "active")
        if status not in {"active", "superseded", "stale"}:
            continue
        evidence = record.get("evidence", [])
        if not isinstance(evidence, list):
            continue

        dated_evidence: list[dict[str, str]] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            evidence_title = item.get("title")
            published_at = item.get("published_at")
            if not all(isinstance(value, str) and value.strip()
                       for value in (url, evidence_title, published_at)):
                continue
            if not url.startswith("https://") or url in excluded:
                continue
            try:
                parsed = datetime.fromisoformat(
                    published_at.strip().replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            dated_evidence.append({
                "title": evidence_title.strip(),
                "url": url.strip(),
                "published_at": parsed.date().isoformat(),
            })
        if not dated_evidence:
            continue

        summary_text = record.get("summary", "")
        significance = record.get("significance", "")
        entities = record.get("entities", [])
        if not isinstance(summary_text, str) or not isinstance(significance, str):
            continue
        if not isinstance(entities, list):
            entities = []
        entity_matches = {
            entity.casefold()
            for entity in entities
            if isinstance(entity, str) and entity.strip()
            and re.search(r"(?<!\\w)" + re.escape(entity.strip()) + r"(?!\\w)",
                          current_text, flags=re.IGNORECASE)
        }
        historical_text = " ".join(
            [summary_text, significance, " ".join(str(x) for x in entities),
             " ".join(item["title"] for item in dated_evidence)]
        ).casefold()
        historical_tokens = {
            token for token in re.findall(r"[a-z0-9]+", historical_text)
            if len(token) > 2 and token not in _PRIOR_CONTEXT_STOPWORDS
        }
        overlap = current_tokens & historical_tokens
        # An entity alone (especially XRP, Ripple, SEC, or a bank name) is too
        # broad. Require additional lexical evidence to avoid unrelated matches.
        if not entity_matches or len(overlap) < 2:
            continue
        latest_date = max(item["published_at"] for item in dated_evidence)
        score = len(entity_matches) * 3 + len(overlap)
        matches.append((score, latest_date, event_id, {
            "id": event_id,
            "date": latest_date,
            "status": status,
            "summary": summary_text,
            "significance": significance,
            "entities": [x for x in entities if isinstance(x, str)],
            "evidence": dated_evidence[:3],
            "overlap_count": len(overlap),
        }))

    matches.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [record for _, _, _, record in matches[:max_records]]


def format_prior_event_context(records: list[dict[str, Any]]) -> str:
    if not records:
        return ""
    lines = [
        "PRIOR INTELLIGENCE HISTORY (source-linked historical context; not current-event evidence):"
    ]
    for record in records:
        evidence = "; ".join(
            f"[{item['published_at']}] {item['title']} ({item['url']})"
            for item in record["evidence"]
        )
        lines.append(
            f"- Event {record['id']} | status={record['status']} | latest source date={record['date']}. "
            f"Prior summary: {record['summary']} Significance recorded then: "
            f"{record['significance']} Entities: {', '.join(record['entities'])}. "
            f"Historical sources: {evidence}"
        )
    lines.append(
        "These are prior model-generated event records, not new reporting. Use them only to identify "
        "continuity, change, or possible significance. Do not treat their summaries or significance "
        "as independently verified current facts. Do not cite them for claims about the supplied "
        "current articles, and do not imply an old relationship or status remains active without "
        "current-source confirmation. Prefer a clearly described change over repeating an old event."
    )
    return "\n".join(lines)


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
