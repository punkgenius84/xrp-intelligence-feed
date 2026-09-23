from models import NewsItem

def deduplicate(items: list[NewsItem], seen: set[str] | None = None) -> tuple[list[NewsItem], set[str]]:
    seen = set(seen or ())
    fresh = []
    for item in items:
        if item.fingerprint not in seen:
            seen.add(item.fingerprint)
            fresh.append(item)
    return fresh, seen
