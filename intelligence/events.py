from __future__ import annotations

from dataclasses import dataclass, field
from .evidence import Evidence
from .llm.schemas import IntelligenceAnalysis

@dataclass(slots=True)
class IntelligenceEvent:
    event_id: str
    event_type: str
    summary: str
    significance: str
    entities: list[str] = field(default_factory=list)
    claims: list = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    model: str = ""

def event_from_analysis(event_id: str, analysis: IntelligenceAnalysis, evidence: list[Evidence]) -> IntelligenceEvent:
    return IntelligenceEvent(event_id=event_id, event_type=analysis.event_type, summary=analysis.event_summary, significance=analysis.significance, entities=list(analysis.entities), claims=list(analysis.claims), uncertainties=list(analysis.uncertainties), evidence=list(evidence), model=analysis.model)


def event_to_dict(event: IntelligenceEvent) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "summary": event.summary,
        "significance": event.significance,
        "entities": list(event.entities),
        "claims": [
            {"text": claim.text, "claim_type": claim.claim_type, "certainty": claim.certainty, "evidence": list(claim.evidence)}
            for claim in event.claims
        ],
        "uncertainties": list(event.uncertainties),
        "evidence": [
            {"source_id": item.source_id, "source": item.source, "url": item.url,
             "published_at": item.published_at, "title": item.title, "source_quality": item.source_quality}
            for item in event.evidence
        ],
        "model": event.model,
    }
