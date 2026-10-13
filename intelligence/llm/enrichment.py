from __future__ import annotations

import re
from dataclasses import replace

from models import NewsItem

from ..evidence import build_evidence_bundle
from ..event_clustering import EventCluster
from ..events import IntelligenceEvent
from ..historical_context import format_historical_context, select_historical_context
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
        for anchor in sorted(_claim_anchor_tokens(claim.text)):
            if anchor not in cited_text:
                excerpt = " ".join(claim.text.split())[:180]
                raise LLMError(
                    "LLM analysis rejected: claim contains an unsupported evidence anchor "
                    f"{anchor!r}; claim={excerpt!r}; evidence={claim.evidence!r}"
                )
    return analysis


def _validate_event_metadata_grounding(
    analysis: IntelligenceAnalysis,
    source_text: str,
    historical_context: str = "",
) -> IntelligenceAnalysis:
    """Reject XRP-family metadata assertions unsupported by source or matched background."""
    normalized_source = f"{source_text}\n{historical_context}".lower()
    metadata = " ".join(
        [
            analysis.event_summary,
            analysis.significance,
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
    """Apply narrow, source-explicit corrections, then fail closed on unsupported acquisitions."""
    normalized = " ".join(source_text.lower().split())
    title = source_text.splitlines()[0].strip().lower() if source_text.splitlines() else ""

    # Some small local models repeatedly label an explicit launch as a generic
    # announcement despite the prompt. Correct only when the headline itself
    # states a completed launch; "to launch" / planned launches are excluded.
    if (
        analysis.event_type == "announcement"
        and re.search(r"\blaunch(?:es|ed)?\b", title)
        and not re.search(r"\bto launch\b|\bplans? to launch\b|\bplanned launch\b", title)
    ):
        return replace(analysis, event_type="product_launch")

    # A custody press release is not a regulatory filing. If the model chooses
    # "filing" but the supplied text describes an organization being selected
    # as custodian and contains no formal filing language, use partnership.
    formal_filing_terms = (
        r"\bfiled\b", r"\bfiling\b", r"\bsubmitted\b",
        r"\bpetition\b", r"\bcomplaint\b", r"\bmotion\b",
        r"\bregistration statement\b", r"\bform [a-z0-9-]+\b",
    )
    has_formal_filing_language = any(re.search(pattern, normalized) for pattern in formal_filing_terms)
    describes_custody_selection = (
        re.search(r"\b(selects?|selected|appoints?|appointed)\b", normalized)
        and re.search(r"\b(custody|custodian|custodial)\b", normalized)
    )
    if (
        analysis.event_type == "filing"
        and describes_custody_selection
        and not has_formal_filing_language
    ):
        return replace(analysis, event_type="partnership")

    if analysis.event_type != "acquisition":
        return analysis
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
    try:
        context_records = select_historical_context(item.title, item.summary)
        historical_context = format_historical_context(context_records)
        response = provider.generate(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(
                title=item.title,
                summary=item.summary,
                source=item.source,
                historical_context=historical_context,
            ),
            options={"temperature": 0},
        )
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
            historical_context,
        )
        return _validate_event_type(analysis, f"{item.title}\n{item.summary}")
    except ValueError as exc:
        raise LLMError(f"LLM analysis rejected: {exc}") from exc


def analyze_cluster(
    cluster: EventCluster,
    provider: LLMProvider,
) -> IntelligenceAnalysis:
    primary = cluster.primary
    try:
        combined_text = "\n".join(f"{item.title}\n{item.summary}" for item in cluster.members)
        context_records = select_historical_context(
            "\n".join(item.title for item in cluster.members), combined_text
        )
        historical_context = format_historical_context(context_records)
        response = provider.generate(
            system=SYSTEM_PROMPT,
            user=build_cluster_user_prompt(cluster.members, historical_context),
            options={"temperature": 0},
        )
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
        source_text = combined_text
        analysis = _validate_event_metadata_grounding(analysis, source_text, historical_context)
        return _validate_event_type(analysis, source_text)
    except ValueError as exc:
        raise LLMError(f"LLM cluster analysis rejected: {exc}") from exc
