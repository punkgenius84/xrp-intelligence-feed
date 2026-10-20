from __future__ import annotations

from intelligence.events import IntelligenceEvent


GENERIC_EVIDENCE_TITLES = frozenset({
    "read more",
    "learn more",
    "click here",
    "view more",
    "more",
    "details",
})


def has_only_generic_evidence_titles(event: IntelligenceEvent) -> bool:
    """Return true only when every evidence title is blank or generic navigation text."""
    evidence = event.evidence
    return bool(evidence) and all(
        str(item.title or "").strip().casefold() in GENERIC_EVIDENCE_TITLES
        or not str(item.title or "").strip()
        for item in evidence
    )
