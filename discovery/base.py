from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from discovery.models import DiscoveryCandidate


@dataclass(slots=True)
class DiscoveryResult:
    source_id: str
    discovery_method: str
    status: str
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    errors: list[str] = field(default_factory=list)
    state_updates: dict[str, dict[str, str]] = field(default_factory=dict)
    pagination: dict[str, Any] = field(default_factory=dict)


class DiscoveryStrategy(Protocol):
    source_id: str
    discovery_method: str

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult: ...
