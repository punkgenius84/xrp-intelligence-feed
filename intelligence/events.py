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
