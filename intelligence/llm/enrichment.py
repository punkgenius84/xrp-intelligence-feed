from __future__ import annotations

import re

from models import NewsItem

from ..evidence import build_evidence_bundle
from ..event_clustering import EventCluster
from ..events import IntelligenceEvent
from .base import LLMError, LLMProvider
from .prompts import SYSTEM_PROMPT, build_cluster_user_prompt, build_user_prompt
from .schemas import IntelligenceAnalysis, parse_analysis


_ACQUISITION_TERMS = (
    "acquire", "acquired", "acquires", "acquisition",
    "takeover", "merger", "merged", "purchase", "purchased",
    "bought", "buyout",
)

# These constructions describe a proposed, speculative, or otherwise incomplete
# ownership change. They must not be allowed to satisfy the acquisition event type
# merely because an acquisition keyword is present.
_NON_FINAL_ACQUISITION_PATTERNS = (
    r"\bpotential(?:ly)?\s+(?:an?\s+)?acquisition\b",
    r"\bpossible\s+(?:an?\s+)?acquisition\b",
    r"\bproposed\s+(?:an?\s+)?acquisition\b",
    r"\b(?:rumou?red|speculated)\s+(?:an?\s+)?acquisition\b",
    r"\bacquisition\s+(?:talks|discussions|negotiations)\b",
    r"\b(?:talks|discussions|negotiations)\s+to\s+acquire\b",
    r"\b(?:considering|exploring|seeking|seeks|plans?\s+to|may|might|could|would)\s+acquir(?:e|ing)\b",
)


def _validate_event_type(analysis: IntelligenceAnalysis, source_text: str) -> IntelligenceAnalysis:
    """Fail closed when a high-specificity event type is unsupported by source text."""
    if analysis.event_type != "acquisition":
        return analysis

    normalized = " ".join(source_text.lower().split())
    if any(re.search(pattern, normalized) for pattern in _NON_FINAL_ACQUISITION_PATTERNS):
        raise LLMError(
            "LLM analysis rejected: acquisition event type is unsupported by source text"
        )

    if not any(term in normalized for term in _ACQUISITION_TERMS):
        raise LLMError(
            "LLM analysis rejected: acquisition event type is unsupported by source text"
        )

    return analysis


def analyze_item(item: NewsItem, provider: LLMProvider) -> IntelligenceAnalysis:
    response = provider.generate(
        system=SYSTEM_PROMPT,
        user=build_user_prompt(
            title=item.title,
            summary=item.summary,
            source=item.source,
        ),
    )
    try:
        analysis = parse_analysis(
            response.content,
            source_url=item.url,
            model=response.model,
            allowed_evidence={"source title", "source summary"},
        )
        return _validate_event_type(analysis, f"{item.title}\n{item.summary}")
    except ValueError as exc:
        raise LLMError(f"LLM analysis rejected: {exc}") from exc


def analyze_cluster(
    cluster: EventCluster,
    provider: LLMProvider,
) -> IntelligenceAnalysis:
    primary = cluster.primary
    response = provider.generate(
        system=SYSTEM_PROMPT,
        user=build_cluster_user_prompt(cluster.members),
    )
    try:
        analysis = parse_analysis(
            response.content,
            source_url=primary.url,
            model=response.model,
            allowed_evidence=({f"source-{index} title" for index in range(1, len(cluster.members) + 1)} | {f"source-{index} summary" for index in range(1, len(cluster.members) + 1)}),
        )
        source_text = "\n".join(f"{item.title}\n{item.summary}" for item in cluster.members)
        return _validate_event_type(analysis, source_text)
    except ValueError as exc:
        raise LLMError(f"LLM cluster analysis rejected: {exc}") from exc
