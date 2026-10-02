from datetime import datetime, timezone

from storage.delivery_history import JsonDeliveryHistory


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class Item:
    candidate_id = "source:item"
    content_hash = "hash"
    title = "Important XRP update"
    url = "https://example.com/update"
    source = "Official Source"
    published_at = STAMP
    collected_at = STAMP
    relevance_score = 72
    score_reasons = ["primary source authority (+20)"]


def test_record_keeps_only_successfully_posted_items(tmp_path):
    store = JsonDeliveryHistory(tmp_path / "history.json")
    store.record([Item()], {"discord|source:item|hash"}, posted_at=STAMP)
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].title == "Important XRP update"
    assert loaded[0].source == "Official Source"


def test_record_is_idempotent_for_same_publication(tmp_path):
    store = JsonDeliveryHistory(tmp_path / "history.json")
    store.record([Item()], {"discord|source:item|hash"}, posted_at=STAMP)
    store.record([Item()], {"discord|source:item|hash"}, posted_at=STAMP)
    assert len(store.load()) == 1
