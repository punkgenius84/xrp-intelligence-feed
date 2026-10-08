from __future__ import annotations

from models import NewsItem

from ..evidence import build_evidence_bundle
from ..event_clustering import EventCluster
from ..events import IntelligenceEvent
from .base import LLMError, LLMProvider
from .prompts import SYSTEM_PROMPT, build_cluster_user_prompt, build_user_prompt
from .schemas import IntelligenceAnalysis, parse_analysis


def _validate_event_type(analysis: IntelligenceAnalysis, source_text: str) -> IntelligenceAnalysis:
    """Fail closed when a high-specificity event type is unsupported by source text."""
    if analysis.event_type == "acquisition":
        acquisition_terms = (
            "acquire", "acquired", "acquires", "acquisition",
            "takeover", "merger", "merged", "purchase", "purchased",
            "bought", "buyout",
        )
        if not any(term in source_text.lower() for term in acquisition_terms):
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
