from datetime import datetime, timezone

from models import NewsItem
from storage.delivery_history import JsonDeliveryHistory
from storage.outbox import JsonOutboxState


STAMP = datetime.now(timezone.utc)


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


def test_history_records_queued_legacy_item_using_its_canonical_key(tmp_path):
    source = NewsItem(
        title="Legacy source item",
        url="https://example.com/legacy",
        source="Official Source",
        published_at=STAMP,
    )
    outbox = JsonOutboxState(tmp_path / "outbox.json")
    queued = outbox.enqueue([source])
    key = queued[0].key

    history = JsonDeliveryHistory(tmp_path / "history.json")
    history.record(queued, {key}, posted_at=STAMP)

    records = history.load()
    assert len(records) == 1
    assert records[0].key == key
