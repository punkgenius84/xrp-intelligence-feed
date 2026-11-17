from intelligence.theses import update_theses


def _event(summary, entity, source_id, *, status="active", source_quality="primary"):
    return {
        "status": status,
        "summary": summary,
        "significance": "",
        "entities": [entity],
        "evidence": [{
            "source_id": source_id,
            "source": source_id,
            "url": f"https://example.test/{source_id}",
            "title": summary,
            "published_at": "2026-11-01T00:00:00+00:00",
            "source_quality": source_quality,
        }],
    }


def test_thesis_requires_distinct_events_and_independent_sources():
    state = {"events": {
        "event-1": _event("Bank seeks master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for banking charter", "Example Bank", "publisher-a"),
    }}
    assert update_theses(state, "2026-11-17T12:00:00+00:00") == []
    assert state["theses"] == {}


def test_thesis_opens_with_two_events_and_two_independent_sources():
    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    opened = update_theses(state, "2026-11-17T12:00:00+00:00")
    assert len(opened) == 1
    thesis = opened[0]
    assert thesis["theme"] == "regulated_digital_asset_banking"
    assert thesis["independent_event_count"] == 2
    assert thesis["independent_source_count"] == 2
    assert thesis["status"] == "supported"
    assert thesis["mechanism"]
    assert thesis["watch_for"]
    assert len(thesis["event_ids"]) == 2


def test_duplicate_event_is_not_double_counted_and_updates_are_idempotent():
    state = {"events": {
        "event-1": _event("Stablecoin settlement integration", "Example Network", "publisher-a"),
        "event-2": _event("Stablecoin payment integration", "Example Network", "publisher-b"),
    }}
    first = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    revision = first["revision"]
    second = update_theses(state, "2026-11-17T13:00:00+00:00")[0]
    assert second["revision"] == revision
    assert second["independent_event_count"] == 2


def test_thesis_retains_negative_evidence_and_marks_mixed_or_weakened():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event("Bank launches digital asset custody", "Example Bank", "publisher-b"),
        "event-3": _event("Bank suspends custody service", "Example Bank", "publisher-c"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "mixed"
    assert thesis["negative_event_ids"] == ["event-3"]


def test_inactive_events_do_not_create_or_support_theses():
    state = {"events": {
        "event-1": _event("Tokenization platform launches", "Example Bank", "publisher-a", status="stale"),
        "event-2": _event("Tokenized fund goes live", "Example Bank", "publisher-b", status="superseded"),
    }}
    assert update_theses(state, "2026-11-17T12:00:00+00:00") == []


def test_thesis_persists_validated_source_links_and_schema_version():
    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["schema_version"] == 1
    assert {item["url"] for item in thesis["source_evidence"]} == {
        "https://example.test/publisher-a",
        "https://example.test/publisher-b",
    }


def test_thesis_context_is_entity_scoped_and_labeled_as_prior_inference():
    from intelligence.theses import format_thesis_context

    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    context = format_thesis_context(
        "Example Bank expands payments service",
        "A new operational announcement.",
        state["theses"],
    )
    assert "PERSISTENT STRATEGIC THESIS HISTORY" in context
    assert "prior inference" in context
    assert "https://example.test/publisher-a" in context
    assert format_thesis_context("Different Bank", "Unrelated story", state["theses"]) == ""
