from models import NewsItem
from intelligence.deduplication import deduplicate

def test_fingerprint_is_stable():
    a = NewsItem("Test", "https://example.com/a", "Example")
    b = NewsItem("Test", "https://example.com/a", "Example")
    assert a.fingerprint == b.fingerprint

def test_deduplication():
    item = NewsItem("Test", "https://example.com/a", "Example")
    fresh, seen = deduplicate([item, item])
    assert len(fresh) == 1
    assert item.fingerprint in seen
