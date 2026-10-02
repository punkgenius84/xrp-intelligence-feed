from __future__ import annotations

from intelligence.events import IntelligenceEvent

MAX_INTELLIGENCE_MESSAGE_LENGTH = 2_000
MAX_CLAIMS = 4
MAX_EVIDENCE = 4
MAX_CONFLICTS = 3
MAX_UNCERTAINTIES = 3


def _clean(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    for character in ("\\", "*", "_", "~", "`", "|", ">"):
        text = text.replace(character, "\\" + character)
    text = text.replace("@", "@\\u200b")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _sources(event: IntelligenceEvent) -> list[str]:
    values = {
        evidence.source.strip()
        for evidence in event.evidence
        if evidence.source.strip()
    }
    return sorted(values, key=str.casefold)


def format_intelligence_event(
    event: IntelligenceEvent,
    *,
    status: str = "active",
    superseded_by: list[str] | None = None,
) -> str:
    """Render an intelligence event for Discord without adding interpretation."""
    lines = [
        f"**{_clean(event.event_type.replace('_', ' ').title(), 80)}**",
        _clean(event.summary, 500),
        f"**Significance:** {_clean(event.significance, 400)}",
        f"**Status:** {_clean(status, 40)}",
    ]

    if event.entities:
        entities = sorted({value.strip() for value in event.entities if value.strip()}, key=str.casefold)
        if entities:
            lines.append("**Entities:** " + _clean(", ".join(entities[:8]), 300))

    if event.claims:
        lines.append("**Claims:**")
        for claim in event.claims[:MAX_CLAIMS]:
            certainty = _clean(claim.certainty, 30)
            lines.append(f"• [{certainty}] {_clean(claim.text, 300)}")

    if event.conflicts:
        lines.append("**Conflicts:**")
        for conflict in event.conflicts[:MAX_CONFLICTS]:
            lines.append(f"• {_clean(conflict, 300)}")

    if event.uncertainties:
        lines.append("**Uncertainties:**")
        for uncertainty in event.uncertainties[:MAX_UNCERTAINTIES]:
            lines.append(f"• {_clean(uncertainty, 300)}")

    sources = _sources(event)
    if sources:
        lines.append("**Sources:** " + _clean(", ".join(sources[:MAX_EVIDENCE]), 300))

    if event.evidence:
        lines.append("**Evidence:**")
        for evidence in event.evidence[:MAX_EVIDENCE]:
            label = _clean(evidence.title, 180)
            lines.append(f"• {_clean(evidence.source, 100)} — {label}: {evidence.url.strip()}")

    if event.member_ids:
        lines.append(f"**Independent members:** {len(event.member_ids)}")

    if superseded_by:
        lines.append("**Replaced by:** " + _clean(", ".join(superseded_by[:3]), 180))

    message = "\n".join(lines)
    return message[:MAX_INTELLIGENCE_MESSAGE_LENGTH]
