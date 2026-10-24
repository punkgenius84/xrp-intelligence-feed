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
    xrp_ledger_surface = "XRP" in entities and any(
        _contains(combined, phrase) for phrase in ("XRP Ledger", "XRPL")
    )
    protocol_change = any(_contains(combined, phrase) for phrase in (
        "validator", "amendment", "bug fix", "protocol upgrade", "protocol update",
        "network upgrade", "consensus", "ledger upgrade", "ledger bug",
    ))
    protocol_action = any(_contains(combined, phrase) for phrase in (
        "moved to", "delayed", "postponed", "deferred", "scheduled",
        "support dipped", "support dropped", "activated", "released", "fixed",
        "rolled out", "implemented", "passed", "rejected", "vote", "voting",
    ))
    if xrp_ledger_surface and protocol_change and protocol_action:
        categories.append("xrp_protocol_development")
    xrp_ledger_tokenization = any(_contains(combined, phrase) for phrase in (
        "fund shares", "tokenized fund", "tokenised fund", "tokenized shares",
        "tokenised shares", "tokenization", "tokenisation", "on-chain fund",
        "onchain fund", "real-world asset", "real world asset", "RWA",
    ))
    xrp_ledger_tokenization_action = any(_contains(combined, phrase) for phrase in (
        "adds", "added", "issues", "issued", "launches", "launched", "mints",
        "minted", "records", "recorded", "lists", "listed", "moves", "moved",
        "integrates", "integrated", "tokenizes", "tokenises",
    ))
    if xrp_ledger_surface and xrp_ledger_tokenization and xrp_ledger_tokenization_action:
        categories.append("xrp_ledger_tokenization_development")
    if "Ripple" in entities:
        categories.append("direct_ripple")
    if "RLUSD" in entities or _contains(combined, "Ripple USD"):
        categories.append("rlusd_stablecoin")
    elif _contains(combined, "stablecoin"):
        categories.append("stablecoin_development")
    if (any(_contains(combined, phrase) for phrase in ("Ripple Payments", "RippleNet"))
            or (_contains(combined, "ODL") and bool(entities & {"Ripple", "XRP"}))):
        categories.append("ripple_payments_network")

    regulatory = bool(entities & {"SEC", "CFTC", "Federal Reserve", "OCC", "FDIC", "Treasury", "OFAC", "FinCEN", "BIS", "ECB", "Bank of Canada", "Bank of England", "Monetary Authority of Singapore", "Bank Negara Malaysia", "Bank of Thailand", "Department of Finance Canada"})
    government = bool(entities & {"White House", "DOJ", "Treasury", "Government of Canada"})
    institutional = bool(entities & {"Citi", "Circle", "Coinbase", "Mastercard", "Swift", "Visa", "DBS", "J.P. Morgan", "BNY", "International Monetary Fund"})
    action = any(_contains(combined, term) for term in (
        "approves", "approved", "adopts", "adopted", "announces", "announced",
        "report", "reports", "reported", "recommends", "recommended",
        "propose", "proposes", "proposed", "identified", "identifies", "issues", "issued", "publishes", "published", "regulation", "regulations", "pre-published", "prepublished", "consultation",
        "enforcement", "rulemaking", "proposed rule", "final rule", "guidance",
        "settlement", "licenses", "licensing", "sanctions", "charges",
        "launch", "launches", "launched", "launching", "expansion", "expands", "expanded", "expanding", "partner", "partners", "partnered", "joins", "joined", "joining", "introduces", "introduced", "introducing", "opens", "opened", "opening",
        "collaborate", "collaboration", "connect", "connects", "connected", "connecting", "integrate", "integration",
        "roll out", "rolls out", "rolled out", "unveil", "unveils", "unveiled", "deploy", "deploys", "deployed",
        "go live", "goes live", "went live",
        "designation", "designations", "designates", "removal", "removals", "delisting",
        "announcement", "announcements", "partnership", "partnerships",
        "agreement", "agreements", "integration", "integrations", "issuance", "issuances",
        "progresses", "progressed", "progressing", "MVP implementation", "minimum viable product",
        "pilot", "pilots", "pilot program", "proof of concept", "proof-of-concept",
        "proposal", "proposals", "public hearing", "public comment", "request for comment", "requests comment",
    ))
    digital_asset_topic = any(_contains(combined, term) for term in (
        "digital asset", "digital assets", "cryptocurrency", "cryptocurrencies",
        "CBDC", "central bank digital currency", "central bank digital currencies",
        "crypto", "stablecoin", "stablecoins", "payment stablecoin",
        "virtual currency", "digital money", "digital asset service provider",
        "genius act", "tokenized deposit", "tokenized deposits", "tokenised deposit",
        "tokenised deposits", "blockchain", "XRP", "XRPL", "Ripple", "RLUSD",
        "tokenization", "tokenisation", "tokenized", "tokenised",
        "real world asset", "real-world asset", "RWA",
    ))
    payment_topic = any(_contains(combined, term) for term in (
        "payment", "payments", "cross-border settlement", "settlement",
    ))
    if regulatory and action and digital_asset_topic:
        categories.append("relevant_regulatory_digital_asset_payments")
    elif regulatory and action and payment_topic:
        categories.append("relevant_regulatory_payment_action")
    if government:
        categories.append("government")
    if government and action and digital_asset_topic:
        categories.append("government_digital_asset_action")
    elif government and action and payment_topic:
        categories.append("government_payment_action")
    if institutional and action and digital_asset_topic:
        categories.append("institutional_digital_asset_payment")
    elif institutional and action and payment_topic:
        # Preserve payment-network relevance without implying a digital-asset
        # link when the source only describes conventional payments.
        categories.append("institutional_payment_action")

    broad_tokenization = any(_contains(combined, term) for term in (
        "tokenization", "tokenisation", "tokenized", "tokenised",
        "real world asset", "real-world asset", "RWA"))
    if broad_tokenization and not direct and "RLUSD" not in entities and not _contains(combined, "stablecoin"):
        categories.append("broad_tokenization")
    # Tokenization-only stories should qualify only when an identified regulator,
    # government body, or tracked institution takes a concrete action. Broad market
    # commentary remains context-only and cannot pass the relevance gate.
    if broad_tokenization and regulatory and action:
        categories.append("regulatory_tokenization_action")
    if broad_tokenization and government and action:
        categories.append("government_tokenization_action")
    if broad_tokenization and institutional and action:
        categories.append("institutional_tokenization_action")
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
    regulatory_relevance = bool(set(categories) & {
        "relevant_regulatory_digital_asset_payments", "relevant_regulatory_payment_action",
    })
    government_relevance = bool(set(categories) & {
        "government_digital_asset_action", "government_payment_action",
    })
    institutional_relevance = (
        "institutional_digital_asset_payment" in categories
        or "institutional_payment_action" in categories
        or "institutional_tokenization_action" in categories
    )
    topical = (
        direct or regulatory_relevance or government_relevance
        or institutional_relevance or "ripple_payments_network" in categories
        or "regulatory_tokenization_action" in categories
        or "government_tokenization_action" in categories
        or "xrp_protocol_development" in categories
        or "xrp_ledger_tokenization_development" in categories
    )

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

    if "xrp_protocol_development" in categories:
        points = weights["technical_protocol_bonus"]
        score += points
        reasons.append(f"XRP Ledger protocol/validator change (+{points})")
        signals.append("topic:xrp_protocol_development")

    if "xrp_ledger_tokenization_development" in categories:
        points = weights["xrp_ledger_tokenization_bonus"]
        score += points
        reasons.append(f"XRP Ledger tokenized fund/share development (+{points})")
        signals.append("topic:xrp_ledger_tokenization_development")

    if topical and item.source_quality == "primary":
        points = weights["primary_source_bonus"]
        score += points
        reasons.append(f"primary source authority for relevant topic (+{points})")
        signals.append("source_quality:primary")

    if institutional_relevance and item.source_quality == "primary":
        points = weights["institutional_source_bonus"]
        score += points
        reasons.append(f"institutional source relevance bonus (+{points})")
        signals.append("source_quality:institutional")

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
