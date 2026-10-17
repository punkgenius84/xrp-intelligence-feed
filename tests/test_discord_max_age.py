from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import main
from discord.publisher import (DEFAULT_MAX_AGE_DAYS, DiscordSettings, format_message,
                               settings_from_env, split_stale)
from models import NewsItem
from storage.outbox import JsonOutboxState, QueuedPublication

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def news(title, days_old, **changes):
    return NewsItem(title=title, url=f"https://example.test/{title.replace(' ', '-')}",
                    source="Ripple", published_at=NOW - timedelta(days=days_old),
                    collected_at=NOW, relevance_score=55, **changes)


def queued(title, days_old):
    when = NOW - timedelta(days=days_old)
    return QueuedPublication(
        key=f"discord|{title}|h", candidate_id=title, content_hash="h", title=title,
        url=f"https://example.test/{title}", source="Ripple", published_at=when,
        collected_at=NOW, relevance_score=55, detected_entities=["Ripple"], score_reasons=["x"])


def test_default_and_env_parsing():
    assert settings_from_env({}).max_age_days == DEFAULT_MAX_AGE_DAYS == 14
    assert settings_from_env({"DISCORD_MAX_AGE_DAYS": "30"}).max_age_days == 30
    assert settings_from_env({"DISCORD_MAX_AGE_DAYS": "0"}).max_age_days == 0
    for bad in ("abc", "-1", "400"):
        with pytest.raises(ValueError):
            settings_from_env({"DISCORD_MAX_AGE_DAYS": bad})


def test_split_future_dated_rejects_implausible_publication_dates():
    soon = news("future-within-tolerance", -1)
    too_far = news("future-too-far", -2)
    fresh, future = split_future_dated(
        [news("fresh", 1), soon, too_far],
        now=NOW,
    )
    assert [item.title for item in fresh] == ["fresh", "future-within-tolerance"]
    assert [item.title for item in future] == ["future-too-far"]


def test_split_future_dated_preserves_undated_items():
    bare = SimpleNamespace(title="no times")
    assert split_future_dated([bare], now=NOW) == ([bare], [])


def test_split_stale_boundaries_and_disabled():
    fresh, old = split_stale([news("new", 1), news("edge", 13), news("old", 15)], 14, now=NOW)
    assert [i.title for i in fresh] == ["new", "edge"]
    assert [i.title for i in old] == ["old"]
    everything, none = split_stale([news("old", 400)], 0, now=NOW)
    assert len(everything) == 1 and none == []


def test_split_stale_never_drops_items_without_timestamps():
    bare = SimpleNamespace(title="no times")
    assert split_stale([bare], 14, now=NOW) == ([bare], [])


def test_split_stale_falls_back_to_collected_at():
    item = NewsItem(title="undated", url="https://example.test/u", source="Ripple",
                    published_at=None, collected_at=NOW - timedelta(days=1))
    assert split_stale([item], 14, now=NOW)[1] == []


def test_message_unescapes_html_entities_but_keeps_markdown_escaping():
    item = news("Korea&#x27;s first &amp; best *deal*", 1)
    item.url = "https://example.test/korea-deal"  # the helper's URL embeds the title
    text = format_message(item)
    assert "Korea's first & best \\*deal\\*" in text
    assert "&#x27;" not in text and "&amp;" not in text
    assert item.title == "Korea&#x27;s first &amp; best *deal*"  # stored title is untouched


def test_publish_feed_drops_stale_queue_entries_and_posts_only_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "JsonDeliveryHistory",
                        lambda: __import__("storage.delivery_history", fromlist=["x"])
                        .JsonDeliveryHistory(tmp_path / "history.json"))
    monkeypatch.setattr(main, "load_intelligence_config",
                        lambda: SimpleNamespace(enabled=False, publish_enabled=False))
    outbox = JsonOutboxState(tmp_path / "outbox.json")
    outbox.save([queued("fresh", 2), queued("stale-a", 120), queued("stale-b", 90)])

    posted = []
    monkeypatch.setattr(main, "publish", lambda items, settings: (
        posted.extend(items), SimpleNamespace(posted_keys=[i.key for i in items]))[1])
    real_split = main.split_stale
    monkeypatch.setattr(main, "split_stale",
                        lambda items, days, now=None: real_split(items, days, now=NOW))
    settings = SimpleNamespace(dry_run=False, webhook_url="https://discord.com/api/webhooks/1/t",
                               max_age_days=14)

    main.publish_feed([], [], settings, outbox)

    assert [i.title for i in posted] == ["fresh"]
    assert outbox.load() == []  # stale entries dropped for good, fresh one delivered


def test_publish_feed_drops_future_dated_queue_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "JsonDeliveryHistory",
                        lambda: __import__("storage.delivery_history", fromlist=["x"])
                        .JsonDeliveryHistory(tmp_path / "history.json"))
    monkeypatch.setattr(main, "load_intelligence_config",
                        lambda: SimpleNamespace(enabled=False, publish_enabled=False))
    outbox = JsonOutboxState(tmp_path / "outbox.json")
    outbox.save([queued("fresh", 2), queued("future", -3)])

    posted = []
    monkeypatch.setattr(main, "publish", lambda items, settings: (
        posted.extend(items), SimpleNamespace(posted_keys=[i.key for i in items]))[1])
    real_split = main.split_stale
    monkeypatch.setattr(main, "split_stale",
                        lambda items, days, now=None: real_split(items, days, now=NOW))
    real_future_split = main.split_future_dated
    monkeypatch.setattr(main, "split_future_dated",
                        lambda items, now=None: real_future_split(items, now=NOW))
    settings = SimpleNamespace(dry_run=False, webhook_url="https://discord.com/api/webhooks/1/t",
                               max_age_days=14)

    main.publish_feed([], [], settings, outbox)

    assert [item.title for item in posted] == ["fresh"]
    assert outbox.load() == []


def test_dry_run_does_not_preview_future_dated_items(monkeypatch):
    future = news("future", -3)
    fresh = news("fresh", 1)
    captured = []
    monkeypatch.setattr(main, "publish", lambda items, settings: captured.extend(items))
    real_future_split = main.split_future_dated
    monkeypatch.setattr(main, "split_future_dated",
                        lambda items, now=None: real_future_split(items, now=NOW))
    settings = SimpleNamespace(dry_run=True, max_age_days=0)

    main.publish_feed([fresh, future], [], settings, None)

    assert [item.title for item in captured] == ["fresh"]


def test_stale_items_are_not_enqueued(monkeypatch):
    # run_pipeline filters before enqueue so an old backlog can never overflow the outbox cap.
    class Box:
        enqueued = None

        def enqueue(self, items):
            Box.enqueued = list(items)

    box = Box()
    items = [news("fresh", 1), news("stale", 100)]
    queueable, too_old = main.split_stale(items, 14, now=NOW)
    box.enqueue(queueable)
    assert [i.title for i in Box.enqueued] == ["fresh"] and len(too_old) == 1
