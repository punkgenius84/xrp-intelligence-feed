from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256

@dataclass(slots=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    summary: str = ""
    source_type: str = "discovery"
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fingerprint(self) -> str:
        raw = f"{self.url.strip().lower()}|{self.title.strip().lower()}"
        return sha256(raw.encode("utf-8")).hexdigest()
