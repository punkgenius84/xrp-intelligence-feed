from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re
import unicodedata


@dataclass(slots=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    summary: str = ""
    source_type: str = "discovery"
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_id: str = ""
    authority_tier: int = 3
    category: str = "discovery"
    entity_coverage: list[str] = field(default_factory=list)
    detected_entities: list[str] = field(default_factory=list)
    source_quality: str = "discovery"
    relevance_score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    score_signals: list[str] = field(default_factory=list)
    relevance_categories: list[str] = field(default_factory=list)
    duplicate_sources: list[str] = field(default_factory=list)
    duplicate_urls: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        # Preserve the v0.1.1 key so existing state entries remain valid.
        raw = f"{self.url.strip().lower()}|{self.title.strip().lower()}"
        return sha256(raw.encode("utf-8")).hexdigest()

    @property
    def event_fingerprint(self) -> str:
        """Fingerprint a sufficiently distinctive headline within its event day."""
        normalized = unicodedata.normalize("NFKC", self.title).casefold()
        tokens = re.findall(r"[a-z0-9]+", normalized)
        # Short/generic headlines are too collision-prone; fall back to URL+title.
        if len(tokens) < 4 or len("".join(tokens)) < 20:
            return self.fingerprint
        event_time = self.published_at or self.collected_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        event_day = event_time.astimezone(timezone.utc).date().isoformat()
        return sha256(("headline|" + event_day + "|" + " ".join(tokens)).encode("utf-8")).hexdigest()
