from datetime import datetime, timezone

import pytest

from intelligence.configuration import load_intelligence_config
from intelligence.pipeline import event_id_for, select_items
from models import NewsItem
from storage.intelligence_state import JsonIntelligenceState, IntelligenceStateError


def item(score, candidate):
    return NewsItem(title=candidate, url=f"https://example.test/{candidate}", source="Example", relevance_score=score, candidate_id=candidate, published_at=datetime.now(timezone.utc))


def test_select_items_is_bounded_and_score_ordered():
    config = load_intelligence_config()
    config = config.__class__(enabled=False, model=config.model, max_items_per_run=2, min_relevance_score=50, timeout_seconds=45)
    selected = select_items([item(55, "a"), item(90, "b"), item(70, "c"), item(20, "d")], config)
    assert [x.candidate_id for x in selected] == ["b", "c"]


def test_event_id_is_stable():
    assert event_id_for(item(80, "abc")) == event_id_for(item(20, "abc"))


def test_intelligence_state_round_trip_and_bound(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()
    state["events"] = {str(i): {"updated_at": f"2026-10-{i:02d}"} for i in range(1, 502)}
    store.save(state)
    loaded = store.load()
    assert len(loaded["events"]) == 500


def test_corrupt_intelligence_state_fails_closed(tmp_path):
    path = tmp_path / "intelligence.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(IntelligenceStateError):
        JsonIntelligenceState(path).load()
