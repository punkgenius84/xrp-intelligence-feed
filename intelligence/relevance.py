import re
from pathlib import Path

from intelligence.configuration import (read_object, validate_keyword_groups,
                                        validate_thresholds)


def _contains(text: str, phrase: str) -> bool:
    return bool(phrase.strip()) and re.search(
        r"(?<!\w)" + re.escape(phrase.strip()) + r"(?!\w)", text, re.IGNORECASE
    ) is not None


def _classify(item, combined: str) -> list[str]:
    entities = set(item.detected_entities)
    categories = []
    direct = bool(entities & {"XRP", "Ripple"})
    if "XRP" in entities:
        categories.append("direct_xrp_xrpl")
    if "Ripple" in entities:
        categories.append("direct_ripple")
    if "RLUSD" in entities or _contains(combined, "Ripple USD"):
        categories.append("rlusd_stablecoin")
    elif _contains(combined, "stablecoin"):
        categories.append("stablecoin_development")
    if (any(_contains(combined, phrase) for phrase in ("Ripple Payments", "RippleNet"))
            or (_contains(combined, "ODL") and bool(entities & {"Ripple", "XRP"}))):
        categories.append("ripple_payments_network")

    regulatory = bool(entities & {"SEC", "CFTC", "Federal Reserve", "OCC", "FDIC", "Treasury", "OFAC"})
    action = any(_contains(combined, term) for term in (
        "approves", "approved", "adopts", "adopted", "announces", "announced",
        "enforcement", "rulemaking", "proposed rule", "final rule", "guidance",
        "settlement", "licenses", "licensing", "sanctions", "charges",
        "designation", "designations", "designates", "removal", "removals", "delisting",
    ))
    digital_or_payment = any(_contains(combined, term) for term in (
        "digital asset", "cryptocurrency", "crypto", "stablecoin", "payment", "payments",
        "tokenized deposit", "blockchain", "XRP", "XRPL", "Ripple", "RLUSD",
    ))
    if regulatory and action and digital_or_payment:
        categories.append("relevant_regulatory_digital_asset_payments")

    broad_tokenization = any(_contains(combined, term) for term in ("tokenization", "tokenized"))
    if broad_tokenization and not direct and "RLUSD" not in entities and not _contains(combined, "stablecoin"):
        categories.append("broad_tokenization")
    return categories


def score_relevance(item, keywords_path: str | Path = "config/keywords.json",
                    thresholds_path: str | Path = "config/thresholds.json") -> tuple[int, list[str]]:
    """Fixed, additive topical relevance; never a price or market-direction prediction."""
    keywords = validate_keyword_groups(read_object(keywords_path, "keywords"))
    thresholds = validate_thresholds(read_object(thresholds_path, "thresholds"))
    weights = thresholds["weights"]
    combined = f"{item.title} {item.summary}"
    categories = _classify(item, combined)
    direct = bool(set(item.detected_entities) & {"XRP", "Ripple", "RLUSD"})
    regulatory_relevance = "relevant_regulatory_digital_asset_payments" in categories
    topical = direct or regulatory_relevance or "ripple_payments_network" in categories

    score = 0
    reasons: list[str] = []
    signals: list[str] = [f"category:{category}" for category in categories]
    groups = (("high", "high_keyword"), ("medium", "medium_keyword"),
              ("context", "context_keyword"))
    for group, weight_key in groups:
        hits = [term for term in keywords[group] if _contains(combined, term)]
        # High terms are meaningful only alongside a configured direct entity.
        # Bare regulatory acronyms never qualify an otherwise generic story.
        if group == "high" and not direct:
            hits = []
        if group == "medium":
            hits = [term for term in hits if term.casefold() not in {"sec", "cftc"}]
            if not topical:
                hits = []
        if group == "context" and not topical:
            hits = []
        if hits:
            points = weights[weight_key]
            score += points
            reasons.append(f"{group} keyword(s) {', '.join(hits)} (+{points})")
            signals.extend(f"keyword:{group}:{hit}" for hit in hits)

    if topical and item.source_quality == "primary":
        points = weights["primary_source_bonus"]
        score += points
        reasons.append(f"primary source authority for relevant topic (+{points})")
        signals.append("source_quality:primary")

    if direct and len(item.detected_entities) > 1:
        points = weights["multiple_entity_bonus"]
        score += points
        reasons.append(f"{len(item.detected_entities)} detected entities alongside direct XRP/Ripple signal (+{points})")
        signals.extend(f"entity:{entity}" for entity in item.detected_entities)

    title_hits = [term for group, _ in groups for term in keywords[group]
                  if _contains(item.title, term)]
    title_hits = [term for term in title_hits if topical and (direct or term.casefold() not in {"sec", "cftc"})]
    if title_hits:
        title_hits = list(dict.fromkeys(title_hits))
        points = weights["title_match_bonus"]
        score += points
        reasons.append(f"title keyword(s) {', '.join(title_hits)} (+{points})")
        signals.extend(f"title:{term}" for term in title_hits)

    item.relevance_score = min(100, score)
    item.score_reasons = reasons
    item.score_signals = signals
    item.relevance_categories = categories
    return item.relevance_score, reasons
