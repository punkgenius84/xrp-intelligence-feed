import feedparser
from datetime import datetime, timezone
from models import NewsItem

class RSSCollector:
    def __init__(self, name: str, url: str, source_type: str = "discovery"):
        self.name, self.url, self.source_type = name, url, source_type

    def collect(self) -> list[NewsItem]:
        feed = feedparser.parse(self.url)
        items = []
        for entry in feed.entries:
            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            items.append(NewsItem(
                title=getattr(entry, "title", "").strip(),
                url=getattr(entry, "link", "").strip(),
                source=self.name,
                published_at=published,
                summary=getattr(entry, "summary", "").strip(),
                source_type=self.source_type,
            ))
        return [x for x in items if x.title and x.url]
