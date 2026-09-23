def classify_source_quality(item) -> str:
    """Classify provenance from registry authority tier; not factual accuracy."""
    quality = {1: "primary", 2: "high_signal", 3: "discovery"}.get(
        item.authority_tier, "discovery"
    )
    item.source_quality = quality
    return quality
