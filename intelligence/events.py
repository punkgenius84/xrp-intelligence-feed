from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import Evidence
from .llm.schemas import Claim, IntelligenceAnalysis


@dataclass(slots=True)
class IntelligenceEvent:
    event_id: str
    event_type: str
    summary: str
    significance: str
    entities: list[str] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    model: str = ""


def event_from_analysis(
    event_id: str,
    analysis: IntelligenceAnalysis,
    evidence: list[Evidence],
) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type=analysis.event_type,
        summary=analysis.event_summary,
        significance=analysis.significance,
        entities=list(analysis.entities),
        member_ids=[],
        supersedes=[],
        claims=list(analysis.claims),
        conflicts=list(analysis.conflicts),
        uncertainties=list(analysis.uncertainties),
        evidence=list(evidence),
        model=analysis.model,
    )


def event_to_dict(event: IntelligenceEvent) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "summary": event.summary,
        "significance": event.significance,
        "entities": list(event.entities),
        "member_ids": list(event.member_ids),
        "supersedes": list(event.supersedes),
        "conflicts": list(event.conflicts),
        "claims": [
            {
                "text": claim.text,
                "claim_type": claim.claim_type,
                "certainty": claim.certainty,
                "evidence": list(claim.evidence),
            }
            for claim in event.claims
        ],
        "uncertainties": list(event.uncertainties),
        "evidence": [
            {
                "source_id": evidence_item.source_id,
                "source": evidence_item.source,
                "url": evidence_item.url,
                "published_at": evidence_item.published_at,
                "title": evidence_item.title,
                "source_quality": evidence_item.source_quality,
            }
            for evidence_item in event.evidence
        ],
        "model": event.model,
    }


def event_from_dict(value: dict) -> IntelligenceEvent:
    if not isinstance(value, dict):
        raise ValueError("intelligence event must be an object")

    event_id = value.get("event_id")
    event_type = value.get("event_type")
    summary = value.get("summary")
    significance = value.get("significance")
    if not all(isinstance(item, str) for item in (event_id, event_type, summary, significance)):
        raise ValueError("intelligence event identity fields must be strings")

    conflicts = value.get("conflicts", [])
    if not isinstance(conflicts, list) or any(not isinstance(item, str) for item in conflicts):
        raise ValueError("intelligence event conflicts must be a string list")

    claims: list[Claim] = []
    raw_claims = value.get("claims", [])
    if not isinstance(raw_claims, list):
        raise ValueError("intelligence event claims must be a list")
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict):
            raise ValueError("intelligence event claim must be an object")
        text = raw_claim.get("text")
        if not isinstance(text, str):
            raise ValueError("intelligence event claim text must be a string")
        claim_type = raw_claim.get("claim_type", "reported_fact")
        certainty = raw_claim.get("certainty", "unknown")
        evidence_refs = raw_claim.get("evidence", [])
        if not isinstance(claim_type, str) or not isinstance(certainty, str):
            raise ValueError("intelligence event claim metadata must be strings")
        if not isinstance(evidence_refs, list) or any(not isinstance(item, str) for item in evidence_refs):
            raise ValueError("intelligence event claim evidence must be a string list")
        claims.append(
            Claim(
                text=text,
                claim_type=claim_type,
                certainty=certainty,
                evidence=list(evidence_refs),
            )
        )

    entities = value.get("entities", [])
    member_ids = value.get("member_ids", [])
    supersedes = value.get("supersedes", [])
    uncertainties = value.get("uncertainties", [])
    if not isinstance(entities, list) or any(not isinstance(item, str) for item in entities):
        raise ValueError("intelligence event entities must be a string list")
    if not isinstance(member_ids, list) or any(not isinstance(item, str) for item in member_ids):
        raise ValueError("intelligence event member_ids must be a string list")
    if not isinstance(supersedes, list) or any(not isinstance(item, str) for item in supersedes):
        raise ValueError("intelligence event supersedes must be a string list")
    if not isinstance(uncertainties, list) or any(not isinstance(item, str) for item in uncertainties):
        raise ValueError("intelligence event uncertainties must be a string list")

    evidence: list[Evidence] = []
    raw_evidence = value.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise ValueError("intelligence event evidence must be a list")
    for raw_evidence_item in raw_evidence:
        if not isinstance(raw_evidence_item, dict):
            raise ValueError("intelligence event evidence must contain objects")
        fields = (
            "source_id",
            "source",
            "url",
            "published_at",
            "title",
            "source_quality",
        )
        if any(not isinstance(raw_evidence_item.get(field), str) for field in fields):
            raise ValueError("intelligence event evidence fields must be strings")
        evidence.append(
            Evidence(
                source_id=raw_evidence_item["source_id"],
                source=raw_evidence_item["source"],
                url=raw_evidence_item["url"],
                published_at=raw_evidence_item["published_at"],
                title=raw_evidence_item["title"],
                source_quality=raw_evidence_item["source_quality"],
            )
        )

    model = value.get("model", "")
    if not isinstance(model, str):
        raise ValueError("intelligence event model must be a string")

    return IntelligenceEvent(
        event_id=event_id,
        event_type=event_type,
        summary=summary,
        significance=significance,
        entities=list(entities),
        member_ids=list(member_ids),
        supersedes=list(supersedes),
        claims=claims,
        conflicts=list(conflicts),
        uncertainties=list(uncertainties),
        evidence=evidence,
        model=model,
    )
