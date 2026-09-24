from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from discovery.base import DiscoveryResult, DiscoveryStrategy
from discovery.cftc_rss import CFTCRSSDiscovery, validate_cftc_source
from discovery.federal_register import (FederalRegisterDiscovery,
                                         validate_federal_register_source)
from discovery.fincen_press_releases import (FinCENPressReleasesDiscovery,
                                              validate_fincen_source)
from discovery.ofac_recent_actions import (OFACRecentActionsDiscovery,
                                           validate_ofac_source)
from discovery.sec_edgar import SECEdgarDiscovery, validate_sec_source


class DiscoveryRegistryError(ValueError):
    """Raised when discovery source configuration is invalid."""


class DiscoveryDispatchError(ValueError):
    """Raised when a configured discovery strategy has no implementation."""


def validate_discovery_sources(payload: object) -> list[dict[str, Any]]:
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "sources"}
            or payload.get("schema_version") != 1 or not isinstance(payload.get("sources"), list)):
        raise DiscoveryRegistryError("Discovery registry must contain schema_version 1 and a sources list")
    result = []
    seen: set[str] = set()
    for index, source in enumerate(payload["sources"]):
        label = f"sources[{index}]"
        if not isinstance(source, dict):
            raise DiscoveryRegistryError(f"{label} must be an object")
        method = source.get("discovery_method")
        validator = {
            "sec_submissions": validate_sec_source,
            "federal_register_api": validate_federal_register_source,
            "ofac_recent_actions_html": validate_ofac_source,
            "cftc_rss": validate_cftc_source,
            "fincen_press_releases": validate_fincen_source,
        }.get(method)
        if validator is None:
            raise DiscoveryRegistryError(f"{label}: unsupported discovery_method {method!r}")
        try:
            validator(source)
        except ValueError as exc:
            raise DiscoveryRegistryError(f"{label}: {exc}") from exc
        source_id = source["source_id"]
        if source_id in seen:
            raise DiscoveryRegistryError(f"duplicate discovery source_id: {source_id}")
        seen.add(source_id)
        result.append(source)
    return result


def load_discovery_sources(path: str | Path = "config/discovery_sources.json") -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryRegistryError(f"Cannot load discovery registry {path}: {exc}") from exc
    return validate_discovery_sources(payload)


def create_strategy(source: dict[str, Any], **kwargs: Any) -> DiscoveryStrategy:
    method = source.get("discovery_method")
    if method == "sec_submissions":
        return SECEdgarDiscovery(source, **kwargs)
    if method == "federal_register_api":
        return FederalRegisterDiscovery(source, **kwargs)
    if method == "ofac_recent_actions_html":
        return OFACRecentActionsDiscovery(source, **kwargs)
    if method == "cftc_rss":
        return CFTCRSSDiscovery(source, **kwargs)
    if method == "fincen_press_releases":
        return FinCENPressReleasesDiscovery(source, **kwargs)
    raise DiscoveryDispatchError(
        f"Unsupported discovery_method {method!r} for {source.get('source_id', 'unknown source')}"
    )


def collect_source(source: dict[str, Any], state: dict[str, Any] | None = None,
                   **kwargs: Any) -> DiscoveryResult:
    return create_strategy(source, **kwargs).collect(state)
