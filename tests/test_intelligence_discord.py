from intelligence.discord import format_intelligence_event
from dataclasses import replace

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from intelligence.llm.schemas import Claim


def make_event():
    return IntelligenceEvent(
        event_id="evt-1",
        event_type="regulatory_action",
        summary="A regulator announced an action involving a monitored company.",
        significance="The supplied source identifies a formal regulatory action.",
        entities=["Ripple", "SEC"],
        claims=[
            Claim(
                text="The source reports the action.",
                certainty="high",
                evidence=["source-1 title"],
            )
        ],
        conflicts=["Source A says planned; Source B says launched."],
        uncertainties=["Implementation timing is not established by the supplied material."],
        member_ids=["a", "b"],
        evidence=[
            Evidence(
                source_id="source-1",
                source="SEC",
                url="https://example.test/source-1",
                published_at="2026-10-01T00:00:00+00:00",
                title="Formal action",
                source_quality="primary",
            ),
            Evidence(
                source_id="source-2",
                source="Company",
                url="https://example.test/source-2",
                published_at="2026-10-01T01:00:00+00:00",
                title="Company response",
                source_quality="primary",
            ),
        ],
        model="test-model",
    )


def test_format_includes_evidence_and_conflicts():
    message = format_intelligence_event(make_event())
    assert "**Conflicts:**" in message
    assert "Source A says planned; Source B says launched." in message
    assert "**Evidence:**" in message
    assert "https://example.test/source-1" in message
    assert "**Independent members:** 2" in message


def test_format_is_bounded():
    event = make_event()
    event.summary = "x" * 5000
    event.significance = "y" * 5000
    message = format_intelligence_event(event)
    assert len(message) <= 2000


def test_format_does_not_add_unverified_content():
    message = format_intelligence_event(make_event())
    assert "therefore" not in message.lower()
    assert "likely" not in message.lower()
    assert "confirmed" not in message.lower()
