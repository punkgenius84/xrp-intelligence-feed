from __future__ import annotations

import json
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


DEFAULT_CONTEXT_PATH = Path("config/historical_context.json")
MAX_CONTEXT_RECORDS = 6


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
        topic = record.get("topic")
        if topic is not None and (not isinstance(topic, str) or not topic.strip()):
            raise HistoricalContextError(f"historical context {record['id']} topic must be a non-empty string")
        priority = record.get("priority")
        if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 5):
            raise HistoricalContextError(f"historical context {record['id']} priority must be an integer from 1 to 5")
        related_urls = record.get("related_source_urls", [])
        if not isinstance(related_urls, list) or any(
            not isinstance(url, str) or not url.startswith("https://") for url in related_urls
        ):
            raise HistoricalContextError(f"historical context {record['id']} related_source_urls must contain HTTPS URLs")
    return records


_GENERIC_CONTEXT_ALIASES = {
    "xrp", "ripple", "xrpl", "xrp ledger", "ripple labs",
}


def _context_alias_weight(alias: str) -> int:
    normalized = " ".join(alias.casefold().split())
    if normalized in _GENERIC_CONTEXT_ALIASES:
        return 1
    words = re.findall(r"[a-z0-9]+", normalized)
    if len(words) >= 2:
        return 3 + min(2, len(words) - 2)
    return 2


def _context_recency_bonus(value: str) -> int:
    match = re.match(r"^(\d{4})", value)
    if not match:
        return 0
    year = int(match.group(1))
    return min(6, max(0, year - 2020))


def select_historical_context(
    title: str,
    summary: str,
    path: str | Path = DEFAULT_CONTEXT_PATH,
    max_records: int = MAX_CONTEXT_RECORDS,
) -> list[dict[str, Any]]:
    """Return a bounded, ranked, topic-diverse set of matching sourced records.

    Specific product/program aliases outrank generic mentions of Ripple or XRP.
    Curated priority and recency prevent old broad matches from displacing newer,
    directly relevant milestones. A small topic-diversity preference preserves useful
    strategic breadth without filling the prompt with unrelated history.
    Context is historical background, never evidence that a relationship remains active.
    """
    if max_records < 0:
        raise ValueError("max_records must be non-negative")
    if max_records == 0:
        return []
    text = f"{title}\n{summary}"
    records = load_historical_context(path)
    matches: list[tuple[int, str, str, dict[str, Any]]] = []
    for record in records:
        matched = {
            alias.casefold()
            for alias in record["aliases"]
            if re.search(r"(?<!\w)" + re.escape(alias.strip()) + r"(?!\w)", text, flags=re.IGNORECASE)
        }
        if not matched:
            continue
        alias_score = sum(_context_alias_weight(alias) for alias in matched)
        priority = record.get("priority", 2)
        score = alias_score * 4 + priority * 3 + _context_recency_bonus(record["date"])
        topic = record.get("topic", "historical")
        matches.append((score, topic, record["date"], record))

    selected: list[dict[str, Any]] = []
    topic_counts: dict[str, int] = {}
    remaining = matches[:]
    while remaining and len(selected) < max_records:
        best_index = max(
            range(len(remaining)),
            key=lambda index: (
                remaining[index][0]
                + (5 if topic_counts.get(remaining[index][1], 0) == 0 else 0)
                - topic_counts.get(remaining[index][1], 0) * 5,
                remaining[index][2],
                remaining[index][3]["id"],
            ),
        )
        _, topic, _, record = remaining.pop(best_index)
        selected.append(record)
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
    return selected


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
            and re.search(r"(?<!\w)" + re.escape(entity.strip()) + r"(?!\w)",
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
        entity_tokens = {
            token
            for entity in entity_matches
            for token in re.findall(r"[a-z0-9]+", entity.casefold())
        }
        lexical_overlap = overlap - entity_tokens
        # An entity alone (especially XRP, Ripple, SEC, or a bank name) is too
        # broad. Require two additional matching terms to avoid unrelated matches.
        if not entity_matches or len(lexical_overlap) < 2:
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
        "as independently verified current facts. You must not cite them for claims about the supplied "
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
            f"- [{record['date']}] topic={record.get('topic', 'historical')} "
            f"priority={record.get('priority', 2)}/5. "
            f"Entities: {', '.join(record['entities'])}. "
            f"Relationship: {record['relationship']} "
            f"Classification: {record['classification']}. "
            f"Status caution: {record['status']}. "
            f"Sources: {'; '.join([record['source_url'], *record.get('related_source_urls', [])])}"
        )
    lines.append(
        "Use this background only to recognize historical connections and explain possible significance. "
        "Never describe a historical relationship as currently active unless the current article says so. "
        "Do not use background as evidence for claims about what the current article reports; claims must cite the supplied current source(s). "
        "Distinguish Ripple (company), XRP (digital asset), and XRPL/XRP Ledger (network)."
    )
    return "\n".join(lines)
