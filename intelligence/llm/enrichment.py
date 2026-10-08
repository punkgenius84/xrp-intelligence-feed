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
    r"\b(?:considering|exploring|seeking|seeks|plans?\s+to|may|might|could|would|potentially|possibly)\s+acquir(?:e|ing)\b",
    r"\b(?:potentially|possibly|may|might|could|would)\s+be\s+(?:an?\s+)?acquisition\b",
)

_DOMAIN_GROUNDING_ALIASES = {
    "xrp": ("xrp",),
    "xrpl": ("xrpl", "xrp ledger"),
    "ripple": ("ripple",),
    "rlusd": ("rlusd", "ripple usd"),
}


_GROUNDING_STOPWORDS = {
    "the", "this", "that", "these", "those", "both", "source", "sources",
    "report", "reports", "reported", "according", "after", "before", "during",
    "under", "from", "into", "with", "for", "and", "but", "new", "first",
    "second", "third",
}


def _claim_anchor_tokens(text: str) -> set[str]:
    """Extract high-signal anchors whose invention should fail closed."""
    patterns = re.findall(
        r"\$?\d+(?:[,.]\d+)*(?:%|[A-Za-z]+)?|"
        r"\b[A-Z]{2,}(?:-[A-Z]{2,})?\b|"
        r"\b[A-Z][a-z]{2,}\b",
        text,
    )
    return {
        token.lower().strip(".,:;!?()[]{}")
        for token in patterns
        if token.lower() not in _GROUNDING_STOPWORDS
    }


def _validate_claim_grounding(
    analysis: IntelligenceAnalysis,
    evidence_text: dict[str, str],
) -> IntelligenceAnalysis:
    """Reject claims whose high-signal factual anchors are absent from cited evidence."""
    for claim in analysis.claims:
        cited_text = " ".join(
            evidence_text.get(reference, "")
            for reference in claim.evidence
        ).lower()
        for anchor in _claim_anchor_tokens(claim.text):
            if anchor not in cited_text:
                raise LLMError(
                    "LLM analysis rejected: claim contains an unsupported evidence anchor"
                )
    return analysis


def _validate_event_metadata_grounding(
    analysis: IntelligenceAnalysis,
    source_text: str,
) -> IntelligenceAnalysis:
    """Reject unsupported XRP-family assertions outside the claim array."""
    normalized_source = source_text.lower()
    metadata = " ".join(
        [
            analysis.event_summary,
            analysis.significance,
            " ".join(analysis.entities),
        ]
    ).lower()

    for term, aliases in _DOMAIN_GROUNDING_ALIASES.items():
        if re.search(r"\b" + re.escape(term) + r"\b", metadata):
            if not any(
                re.search(r"\b" + re.escape(alias) + r"\b", normalized_source)
                for alias in aliases
            ):
                raise LLMError(
                    "LLM analysis rejected: event metadata contains an unsupported "
                    f"{term} reference"
                )
    return analysis


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
        evidence_text = {
            "source title": item.title,
            "source summary": item.summary,
        }
        analysis = _validate_claim_grounding(analysis, evidence_text)
        analysis = _validate_event_metadata_grounding(
            analysis,
            f"{item.title}\n{item.summary}",
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
        evidence_text = {}
        for index, item in enumerate(cluster.members, start=1):
            evidence_text[f"source-{index} title"] = item.title
            evidence_text[f"source-{index} summary"] = item.summary
        analysis = _validate_claim_grounding(analysis, evidence_text)
        source_text = "\n".join(f"{item.title}\n{item.summary}" for item in cluster.members)
        analysis = _validate_event_metadata_grounding(analysis, source_text)
        return _validate_event_type(analysis, source_text)
    except ValueError as exc:
        raise LLMError(f"LLM cluster analysis rejected: {exc}") from exc
