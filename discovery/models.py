from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from models import NewsItem


def stable_candidate_id(source_id: str, source_native_id: str = "", *,
                        canonical_url: str = "", content_hash: str = "") -> str:
    """Prefer publisher IDs; otherwise derive a repeatable ID from normalized inputs."""
    if source_native_id:
        return f"{source_id}:{source_native_id}"
    identity = "|".join((source_id, canonical_url.strip(), content_hash.strip()))
    return f"{source_id}:{sha256(identity.encode('utf-8')).hexdigest()}"


@dataclass(slots=True)
class DiscoveryCandidate(NewsItem):
    """A NewsItem with discovery provenance, directly reusable by v0.2 intelligence."""

    def __post_init__(self) -> None:
        if not self.primary_url:
            self.primary_url = self.url
        if not self.candidate_id:
            self.candidate_id = stable_candidate_id(
                self.source_id, self.source_native_id,
                canonical_url=self.url, content_hash=self.content_hash,
            )

    @property
    def summary_excerpt(self) -> str:
        return self.summary

    @property
    def entities(self) -> list[str]:
        return self.detected_entities

    @property
    def first_seen_time(self) -> datetime | None:
        return self.first_seen_at

    @property
    def last_seen_time(self) -> datetime | None:
        return self.last_seen_at
