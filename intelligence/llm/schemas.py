from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EVENT_TYPES = {
    "announcement", "partnership", "regulatory_action", "enforcement", "filing",
    "legislation", "policy_change", "product_launch", "institutional_adoption",
    "funding", "acquisition", "litigation", "executive_action", "other",
}


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    claim_type: str = "reported_fact"
    certainty: str = "unknown"
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IntelligenceAnalysis:
    event_type: str = "other"
    event_summary: str = ""
    significance: str = ""
    entities: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    source_url: str = ""
    model: str = ""


def _strings(value: Any, *, max_items: int = 12, max_length: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:max_length])
    return result


def parse_analysis(raw: str, *, source_url: str, model: str = "") -> IntelligenceAnalysis:
    """Parse and strictly bound LLM JSON. Unrecognized fields are ignored."""
    import json

    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM output is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("LLM output must be a JSON object")

    event_type = value.get("event_type", "other")
    if event_type not in EVENT_TYPES:
        event_type = "other"
    claims: list[Claim] = []
    raw_claims = value.get("claims", [])
    if isinstance(raw_claims, list):
        for raw_claim in raw_claims[:12]:
            if not isinstance(raw_claim, dict):
                continue
            text = raw_claim.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            claim_type = raw_claim.get("claim_type", "reported_fact")
            certainty = raw_claim.get("certainty", "unknown")
            if not isinstance(claim_type, str):
                claim_type = "reported_fact"
            if not isinstance(certainty, str):
                certainty = "unknown"
            claims.append(Claim(
                text=text.strip()[:500],
                claim_type=claim_type.strip()[:80],
                certainty=certainty.strip()[:80],
                evidence=_strings(raw_claim.get("evidence"), max_items=4, max_length=300),
            ))

    return IntelligenceAnalysis(
        event_type=event_type,
        event_summary=str(value.get("event_summary") or "").strip()[:1000],
        significance=str(value.get("significance") or "").strip()[:1000],
        entities=_strings(value.get("entities"), max_items=20, max_length=120),
        claims=claims,
        uncertainties=_strings(value.get("uncertainties"), max_items=12, max_length=400),
        source_url=source_url,
        model=model,
    )
