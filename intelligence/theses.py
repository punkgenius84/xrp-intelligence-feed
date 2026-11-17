from __future__ import annotations

"""Deterministic, evidence-linked strategic thesis tracking.

This layer does not ask an LLM to invent a thesis. It groups already validated
intelligence events by a conservative theme/entity taxonomy and requires multiple
distinct events and source IDs before opening a thesis.
"""

from hashlib import sha256
import re
from typing import Any

THESIS_SCHEMA_VERSION = 1
MAX_THESES = 200
MAX_EVENTS_PER_THESIS = 40

# Theme, detection terms, mechanism, and future signals. Terms are intentionally
# broad enough to connect developments, but a shared named entity is also required.
_THEMES = {
    "regulated_digital_asset_banking": {
        "terms": ("bank charter", "banking charter", "national trust bank", "trust charter",
                  "master account", "federal reserve account", "bank license", "banking license"),
        "mechanism": "Regulated banking permissions and central-bank access can expand an institution's ability to custody, settle, and move digital-asset or fiat value directly.",
        "watch": ("charter or license approval", "Federal Reserve account or master-account decision",
                  "supervisory conditions and actual service launch"),
    },
    "stablecoin_payment_rails": {
        "terms": ("stablecoin", "stable coin", "tokenized deposit", "digital dollar"),
        "mechanism": "Issuance, reserve custody, distribution, and settlement integrations can turn a token into usable payment infrastructure rather than a standalone asset.",
        "watch": ("live payment or settlement volume", "named bank or payment-network integrations",
                  "reserve, redemption, and regulatory disclosures"),
    },
    "institutional_custody": {
        "terms": ("digital asset custody", "crypto custody", "custody services", "custodian", "staking services"),
        "mechanism": "Institutional custody and servicing capabilities reduce operational barriers for banks, funds, and corporates to hold or use digital assets.",
        "watch": ("regulatory authorization", "named institutional clients", "assets under custody and live service availability"),
    },
    "tokenization_market_infrastructure": {
        "terms": ("tokenization", "tokenised", "tokenized", "digital securities", "tokenized fund"),
        "mechanism": "Tokenized assets require issuance, ownership records, compliance, custody, and settlement infrastructure that can create demand for interoperable ledgers and payment rails.",
        "watch": ("production issuance", "regulated venue or custodian participation", "settlement asset and network named in filings"),
    },
    "cross_border_settlement": {
        "terms": ("cross-border settlement", "cross border settlement", "cross-border payments",
                  "cross border payments", "fx settlement", "foreign exchange settlement",
                  "payment corridor", "remittance settlement"),
        "mechanism": "Repeated corridor and FX settlement deployments can indicate a shift toward faster, more interoperable liquidity and payment infrastructure.",
        "watch": ("production corridor launch", "measured settlement volume and cost", "disclosed liquidity or settlement assets"),
    },
}

_NEGATIVE = re.compile(
    r"\b(rejected|denied|withdrawn|withdrew|terminated|terminates|cancelled|canceled|suspended|suspends|suspend|halted|halts|revoked|revokes|failed|fails)\b",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _event_text(event: dict[str, Any]) -> str:
    parts = [_clean(event.get("summary")), _clean(event.get("significance"))]
    parts.extend(_clean(value) for value in event.get("entities", []) if isinstance(value, str))
    parts.extend(_clean(item.get("title")) for item in event.get("evidence", []) if isinstance(item, dict))
    return " ".join(part for part in parts if part)


def _event_sources(event: dict[str, Any]) -> set[str]:
    return {
        _clean(item.get("source_id"))
        for item in event.get("evidence", [])
        if isinstance(item, dict) and _clean(item.get("source_id"))
    }


def _event_entities(event: dict[str, Any]) -> list[str]:
    return sorted({
        _clean(value) for value in event.get("entities", [])
        if isinstance(value, str) and _clean(value)
    }, key=str.casefold)


def _thesis_id(theme: str, entity: str) -> str:
    digest = sha256(f"{theme}|{entity.casefold()}".encode("utf-8")).hexdigest()[:20]
    return f"thesis-{digest}"


def update_theses(state: dict[str, Any], updated_at: str) -> list[dict[str, Any]]:
    """Refresh thesis records from persisted, validated event records.

    A source ID is the independence boundary: multiple URLs from one publisher do
    not count as multiple independent sources. Event IDs are deduplicated.
    """
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        raise ValueError("intelligence state must contain an events object")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError("updated_at must be a non-empty string")

    theses = state.setdefault("theses", {})
    if not isinstance(theses, dict):
        raise ValueError("intelligence thesis state must be an object")

    buckets: dict[str, dict[str, Any]] = {}
    for event_id, event in state["events"].items():
        if not isinstance(event, dict) or event.get("status", "active") != "active":
            continue
        text = _event_text(event)
        lowered = text.casefold()
        matched_themes = [
            (theme, spec) for theme, spec in _THEMES.items()
            if any(term in lowered for term in spec["terms"])
        ]
        if not matched_themes:
            continue
        entities = _event_entities(event)
        if not entities:
            continue
        for theme, spec in matched_themes:
            for entity in entities:
                thesis_id = _thesis_id(theme, entity)
                bucket = buckets.setdefault(thesis_id, {
                    "theme": theme, "entity": entity, "spec": spec,
                    "events": {}, "sources": set(), "negative_events": set(),
                })
                bucket["events"][event_id] = event
                bucket["sources"].update(_event_sources(event))
                if _NEGATIVE.search(text):
                    bucket["negative_events"].add(event_id)

    changed: list[dict[str, Any]] = []
    for thesis_id, bucket in buckets.items():
        events = bucket["events"]
        source_ids = sorted(bucket["sources"])
        # Require two separate event identities AND two publisher/source IDs.
        if len(events) < 2 or len(source_ids) < 2:
            continue
        event_ids = sorted(events)
        previous = theses.get(thesis_id, {})
        negative_count = len(bucket["negative_events"])
        if negative_count and negative_count >= len(events) / 2:
            status = "weakened"
        elif negative_count:
            status = "mixed"
        else:
            status = "supported"
        primary_sources = {
            _clean(item.get("source_id"))
            for event in events.values()
            for item in event.get("evidence", [])
            if isinstance(item, dict) and item.get("source_quality") == "primary"
            and _clean(item.get("source_id"))
        }
        # Transparent heuristic, not a probability: evidence breadth and source
        # quality determine a bounded evidence-strength score.
        score = min(90, 35 + min(len(events), 5) * 7 + min(len(source_ids), 5) * 4
                    + min(len(primary_sources), 3) * 3 - negative_count * 8)
        record = {
            "thesis_id": thesis_id,
            "theme": bucket["theme"],
            "entity": bucket["entity"],
            "statement": (
                f"{bucket['entity']} has multiple developments consistent with "
                f"{bucket['theme'].replace('_', ' ')}."
            ),
            "mechanism": bucket["spec"]["mechanism"],
            "status": status,
            "evidence_strength": max(0, min(100, score)),
            "strength_label": "high" if score >= 70 else "moderate" if score >= 50 else "early",
            "event_ids": event_ids[-MAX_EVENTS_PER_THESIS:],
            "source_ids": source_ids,
            "independent_event_count": len(events),
            "independent_source_count": len(source_ids),
            "primary_source_count": len(primary_sources),
            "negative_event_ids": sorted(bucket["negative_events"]),
            "watch_for": list(bucket["spec"]["watch"]),
            "first_seen_at": previous.get("first_seen_at", updated_at),
            "updated_at": updated_at,
            "revision": int(previous.get("revision", 0)) + (1 if any(
                previous.get(key) != value for key, value in {
                    "status": status, "event_ids": event_ids[-MAX_EVENTS_PER_THESIS:],
                    "source_ids": source_ids, "evidence_strength": max(0, min(100, score)),
                }.items()
            ) else 0),
        }
        if not previous:
            record["revision"] = 1
        theses[thesis_id] = record
        changed.append(record)

    if len(theses) > MAX_THESES:
        ordered = sorted(
            theses.items(),
            key=lambda pair: pair[1].get("updated_at", ""),
            reverse=True,
        )[:MAX_THESES]
        state["theses"] = dict(ordered)
    return sorted(changed, key=lambda item: item["thesis_id"])
