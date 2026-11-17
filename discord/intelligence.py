from __future__ import annotations

from intelligence.events import IntelligenceEvent

MAX_INTELLIGENCE_MESSAGE_LENGTH = 2_000
MAX_CLAIMS = 4
MAX_EVIDENCE = 4
MAX_CONFLICTS = 3
MAX_UNCERTAINTIES = 3
MAX_SIGNIFICANCE_LENGTH = 700


def _clean(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    for character in ("\\", "*", "_", "~", "`", "|", ">"):
        text = text.replace(character, "\\" + character)
    text = text.replace("@", "@\u200b")
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
    status: str = "reported",
    superseded_by: list[str] | None = None,
) -> str:
    """Render an intelligence event for Discord without adding interpretation."""
    significance = event.significance
    if isinstance(significance, list):
        significance = " ".join(
            " ".join(value.split()) for value in significance
            if isinstance(value, str) and value.strip()
        )
    if not isinstance(significance, str):
        significance = ""

    lines = [
        f"**{_clean(event.event_type.replace('_', ' ').title(), 70)}**",
        _clean(event.summary, 420),
    ]
    if significance.strip():
        lines.append(
            f"**{'Strategic read-through' if event.event_type == 'partnership' else 'Why it matters'}:** "
            f"{_clean(significance, MAX_SIGNIFICANCE_LENGTH)}"
        )
    lines.append(f"**Status:** {_clean(status, 32)}")

    if event.entities:
        entities = sorted({value.strip() for value in event.entities if value.strip()}, key=str.casefold)
        if entities:
            lines.append("**Key entities:** " + _clean(", ".join(entities[:6]), 180))

    if event.claims:
        lines.append("**Grounded updates:**")
        for claim in event.claims[:MAX_CLAIMS]:
            certainty_value = str(claim.certainty or "").strip().casefold()
            claim_text = _clean(claim.text, 240)
            if certainty_value and certainty_value != "unknown":
                lines.append(f"• *{_clean(certainty_value, 20)}* — {claim_text}")
            else:
                lines.append(f"• {claim_text}")

    if event.conflicts:
        lines.append("**Conflicting reports:**")
        for conflict in event.conflicts[:MAX_CONFLICTS]:
            lines.append(f"• {_clean(conflict, 220)}")

    if event.uncertainties:
        lines.append("**Still unclear:**")
        for uncertainty in event.uncertainties[:MAX_UNCERTAINTIES]:
            lines.append(f"• {_clean(uncertainty, 220)}")

    if event.evidence:
        lines.append("**Sources:**")
        for evidence in event.evidence[:MAX_EVIDENCE]:
            label = _clean(evidence.title, 130) or _clean(evidence.source, 80)
            quality = str(evidence.source_quality or "").strip().casefold()
            if quality == "primary":
                source_kind = "Primary source"
            elif quality == "secondary":
                source_kind = "Independent reporting"
            else:
                source_kind = "Source"
            published = str(evidence.published_at or "").strip()[:10]
            metadata = f"{source_kind} · {label}"
            if published:
                metadata += f" · {published}"
            lines.append(f"• {metadata}")
            url = str(evidence.url or "").strip()
            if url.startswith("https://") and not any(char.isspace() for char in url):
                lines.append(f"  {url}")

    if superseded_by:
        lines.append("**Updated by:** " + _clean(", ".join(superseded_by[:3]), 140))

    message = "\n".join(lines)
    return message[:MAX_INTELLIGENCE_MESSAGE_LENGTH]
