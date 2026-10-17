from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main
from models import NewsItem
from storage.correlation_state import JsonCorrelationState
from storage.database import JsonState


def test_future_dated_candidate_is_not_marked_seen_and_can_reappear_when_corrected(
    monkeypatch, tmp_path
):
    now = datetime.now(timezone.utc)
    item = NewsItem(
        title="Ripple announces XRP cross-border payment integration",
        url="https://example.test/ripple-xrp-payment",
        source="Example",
        published_at=now + timedelta(days=3),
        collected_at=now,
        source_id="example",
        authority_tier=1,
        category="official",
        entity_coverage=["XRP"],
        candidate_id="example:ripple-xrp-payment",
    )
    current = [item]

    class Collector:
        last_report = None

        def __init__(self, source):
            pass

        def collect(self):
            return list(current)

    class Outbox:
        def __init__(self):
            self.items = []

        def enqueue(self, items):
            self.items.extend(items)

    monkeypatch.setattr(main, "RSSCollector", Collector)
    monkeypatch.setattr(
        main,
        "load_intelligence_config",
        lambda: SimpleNamespace(enabled=False, publish_enabled=False),
    )

    source = {
        "source_id": "example",
        "name": "Example",
        "url": "https://example.test/feed",
        "authority_tier": 1,
        "category": "official",
        "entity_coverage": ["XRP"],
        "collection_type": "rss",
    }
    seen = JsonState(str(tmp_path / "seen.json"))
    correlation = JsonCorrelationState(tmp_path / "correlation.json")
    outbox = Outbox()

    first = main.run_pipeline(
        sources=[source],
        state=seen,
        correlation_state=correlation,
        outbox_state=outbox,
    )

    assert first.fresh == []
    assert seen.load() == set()
    assert outbox.items == []

    # The same URL and title now have a credible publication date. Because the
    # future-dated version never entered dedup state, it can be processed normally.
    current[0].published_at = now - timedelta(days=1)
    second = main.run_pipeline(
        sources=[source],
        state=seen,
        correlation_state=correlation,
        outbox_state=outbox,
    )

    assert [candidate.url for candidate in second.fresh] == [item.url]
    assert item.fingerprint in seen.load()
