from intelligence.theses import update_theses


def _event(summary, entity, source_id, *, status="active", source_quality="primary",
           claim_type="reported_fact", claim_text=None, significance=""):
    return {
        "status": status,
        "summary": summary,
        "significance": significance,
        "entities": [entity],
        "claims": [{
            "text": claim_text or summary,
            "claim_type": claim_type,
            "certainty": "high",
            "evidence": ["source summary"],
        }],
        "evidence": [{
            "source_id": source_id,
            "source": source_id,
            "url": f"https://example.test/{source_id}",
            "title": summary,
            "published_at": "2026-11-01T00:00:00+00:00",
            "source_quality": source_quality,
        }],
    }


def test_thesis_requires_distinct_events_and_independent_sources():
    state = {"events": {
        "event-1": _event("Bank seeks master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for banking charter", "Example Bank", "publisher-a"),
    }}
    assert update_theses(state, "2026-11-17T12:00:00+00:00") == []
    assert state["theses"] == {}


def test_thesis_opens_with_two_events_and_two_independent_sources():
    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    opened = update_theses(state, "2026-11-17T12:00:00+00:00")
    assert len(opened) == 1
    thesis = opened[0]
    assert thesis["theme"] == "regulated_digital_asset_banking"
    assert thesis["independent_event_count"] == 2
    assert thesis["independent_source_count"] == 2
    assert thesis["status"] == "supported"
    assert thesis["mechanism"]
    assert thesis["watch_for"]
    assert len(thesis["event_ids"]) == 2


def test_duplicate_event_is_not_double_counted_and_updates_are_idempotent():
    state = {"events": {
        "event-1": _event("Stablecoin settlement integration", "Example Network", "publisher-a"),
        "event-2": _event("Stablecoin payment integration", "Example Network", "publisher-b"),
    }}
    first = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    revision = first["revision"]
    second = update_theses(state, "2026-11-17T13:00:00+00:00")[0]
    assert second["revision"] == revision
    assert second["independent_event_count"] == 2


def test_thesis_retains_negative_evidence_and_marks_mixed_or_weakened():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event("Bank launches digital asset custody", "Example Bank", "publisher-b"),
        "event-3": _event("Bank suspends custody service", "Example Bank", "publisher-c"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "mixed"
    assert thesis["negative_event_ids"] == ["event-3"]


def test_inactive_events_do_not_create_or_support_theses():
    state = {"events": {
        "event-1": _event("Tokenization platform launches", "Example Bank", "publisher-a", status="stale"),
        "event-2": _event("Tokenized fund goes live", "Example Bank", "publisher-b", status="superseded"),
    }}
    assert update_theses(state, "2026-11-17T12:00:00+00:00") == []


def test_thesis_persists_validated_source_links_and_schema_version():
    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["schema_version"] == 1
    assert {item["url"] for item in thesis["source_evidence"]} == {
        "https://example.test/publisher-a",
        "https://example.test/publisher-b",
    }


def test_thesis_context_is_entity_scoped_and_labeled_as_prior_inference():
    from intelligence.theses import format_thesis_context

    state = {"events": {
        "event-1": _event("Bank seeks Federal Reserve master account", "Example Bank", "publisher-a"),
        "event-2": _event("Bank applies for national trust bank charter", "Example Bank", "publisher-b"),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    context = format_thesis_context(
        "Example Bank expands payments service",
        "A new operational announcement.",
        state["theses"],
    )
    assert "PERSISTENT STRATEGIC THESIS HISTORY" in context
    assert "prior inference" in context
    assert "https://example.test/publisher-a" in context
    assert format_thesis_context("Different Bank", "Unrelated story", state["theses"]) == ""



def test_hypothetical_negative_in_significance_does_not_weaken_a_thesis():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event(
            "Bank launches digital asset custody",
            "Example Bank",
            "publisher-b",
            significance="The service could be suspended if operating costs rise.",
        ),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "supported"
    assert thesis["negative_event_ids"] == []


def test_speculative_claim_is_not_counted_as_confirmed_negative_evidence():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event(
            "Bank launches a digital asset custody service update",
            "Example Bank",
            "publisher-b",
            claim_type="speculation",
            claim_text="Bank may suspend custody service.",
        ),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "supported"
    assert thesis["negative_event_ids"] == []


def test_hypothetical_language_inside_claim_is_not_counted_as_negative_evidence():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event(
            "Bank launches a digital asset custody service update",
            "Example Bank",
            "publisher-b",
            claim_text="The bank could suspend the service if costs rise.",
        ),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "supported"
    assert thesis["negative_event_ids"] == []



def test_reported_negative_that_was_explicitly_ruled_out_does_not_weaken_thesis():
    state = {"events": {
        "event-1": _event("Bank receives custody approval", "Example Bank", "publisher-a"),
        "event-2": _event(
            "Bank launches a digital asset custody service update",
            "Example Bank",
            "publisher-b",
            claim_text="The bank did not suspend the custody service.",
        ),
    }}
    thesis = update_theses(state, "2026-11-17T12:00:00+00:00")[0]
    assert thesis["status"] == "supported"
    assert thesis["negative_event_ids"] == []



def test_xrp_bridge_asset_thesis_requires_source_grounded_xrp_bridge_claim():
    state = {"events": {
        "event-1": _event(
            "Ripple announces an ODL payments corridor",
            "XRP",
            "publisher-a",
            claim_text="Ripple announced an ODL payments corridor.",
        ),
        "event-2": _event(
            "Ripple expands ODL payments",
            "XRP",
            "publisher-b",
            claim_text="Ripple expanded an ODL payments capability.",
        ),
    }}
    update_theses(state, "2026-11-18T12:00:00+00:00")
    assert not any(
        record["theme"] == "xrp_bridge_asset_liquidity"
        for record in state["theses"].values()
    )


def test_xrp_bridge_asset_thesis_opens_only_from_two_independent_grounded_xrp_claims():
    state = {"events": {
        "event-1": _event(
            "XRP is used as a bridge asset through ODL in a new corridor",
            "XRP",
            "publisher-a",
            claim_text="The source states that XRP is used as a bridge asset through ODL in the corridor.",
        ),
        "event-2": _event(
            "ODL uses XRP as the bridge currency for cross-border payment flows",
            "XRP",
            "publisher-b",
            claim_text="The source states that ODL uses XRP as a bridge currency for cross-border payments.",
        ),
    }}
    opened = update_theses(state, "2026-11-18T12:00:00+00:00")
    bridge = [
        record for record in opened
        if record["theme"] == "xrp_bridge_asset_liquidity" and record["entity"] == "XRP"
    ]
    assert len(bridge) == 1
    assert bridge[0]["independent_event_count"] == 2
    assert bridge[0]["independent_source_count"] == 2
    assert "market-maker" in bridge[0]["mechanism"]


def test_xrpl_amm_thesis_requires_a_reported_xrpl_amm_claim():
    state = {"events": {
        "event-1": _event(
            "XRP Ledger AMM activity expands its DeFi ecosystem",
            "XRP",
            "publisher-a",
            claim_text="The XRP Ledger expanded its decentralized finance ecosystem.",
        ),
        "event-2": _event(
            "XRPL AMM liquidity improves",
            "XRP",
            "publisher-b",
            claim_text="The source describes stronger XRPL liquidity.",
        ),
    }}
    update_theses(state, "2026-11-18T12:00:00+00:00")
    assert not any(record["theme"] == "xrpl_amm_liquidity" for record in state["theses"].values())


def test_xrpl_amm_thesis_opens_from_independent_reported_amm_developments():
    state = {"events": {
        "event-1": _event(
            "XLS-30 AMM launches on XRPL",
            "XRP",
            "publisher-a",
            claim_text="The XRP Ledger enabled the XLS-30 automated market maker amendment.",
        ),
        "event-2": _event(
            "XRPL adds an automated market maker liquidity pool",
            "XRP",
            "publisher-b",
            claim_text="The XRP Ledger added an automated market maker to its DEX.",
        ),
    }}
    opened = update_theses(state, "2026-11-18T12:00:00+00:00")
    amm = [
        record for record in opened
        if record["theme"] == "xrpl_amm_liquidity" and record["entity"] == "XRP"
    ]
    assert len(amm) == 1
    assert "not automatically XRP demand" in amm[0]["mechanism"]


def test_xrp_escrow_supply_thesis_requires_a_reported_escrow_claim():
    state = {"events": {
        "event-1": _event(
            "Ripple reports quarterly XRP escrow balances",
            "XRP",
            "publisher-a",
            claim_text="Ripple reported its XRP holdings.",
            significance="The report could relate to escrow releases.",
        ),
        "event-2": _event(
            "XRP escrow supply dynamics draw attention",
            "XRP",
            "publisher-b",
            claim_text="The report describes XRP supply dynamics.",
            significance="Escrow may influence circulating supply.",
        ),
    }}
    update_theses(state, "2026-11-18T12:00:00+00:00")
    assert not any(record["theme"] == "xrp_escrow_supply" for record in state["theses"].values())


def test_xrp_escrow_supply_thesis_opens_with_grounded_escrow_events():
    state = {"events": {
        "event-1": _event(
            "Ripple releases XRP escrow",
            "XRP",
            "publisher-a",
            claim_text="Ripple released XRP from escrow.",
        ),
        "event-2": _event(
            "Ripple re-escrows unused XRP",
            "XRP",
            "publisher-b",
            claim_text="Ripple placed unused XRP back into escrow.",
        ),
    }}
    opened = update_theses(state, "2026-11-18T12:00:00+00:00")
    escrow = [
        record for record in opened
        if record["theme"] == "xrp_escrow_supply" and record["entity"] == "XRP"
    ]
    assert len(escrow) == 1
    assert "Gross releases" in escrow[0]["mechanism"]



def test_xrpl_auto_bridging_thesis_requires_a_reported_xrp_routing_claim():
    state = {"events": {
        "event-1": _event(
            "XRPL DEX liquidity improves with auto-bridging",
            "XRP",
            "publisher-a",
            claim_text="The XRP Ledger DEX added a new liquidity feature.",
            significance="This could enable XRP to act as an intermediary currency if routing conditions favor it.",
        ),
        "event-2": _event(
            "XRP auto-bridging considered for XRPL DEX",
            "XRP",
            "publisher-b",
            claim_text="The article discusses the XRP Ledger decentralized exchange and routing.",
            significance="The feature could auto-bridge trades through XRP in the future.",
        ),
    }}
    update_theses(state, "2026-11-18T12:00:00+00:00")
    assert not any(
        record["theme"] == "xrpl_dex_xrp_autobridging"
        for record in state["theses"].values()
    )


def test_xrpl_auto_bridging_thesis_opens_only_from_independent_grounded_claims():
    state = {"events": {
        "event-1": _event(
            "XRPL DEX auto-bridging routes through XRP",
            "XRP",
            "publisher-a",
            claim_text="The XRP Ledger can auto-bridge some decentralized exchange trades through XRP.",
        ),
        "event-2": _event(
            "XRPL auto-bridging uses XRP as intermediary currency on the DEX",
            "XRP",
            "publisher-b",
            claim_text="The source describes XRP as the intermediary currency for auto-bridging on the XRP Ledger DEX.",
        ),
    }}
    opened = update_theses(state, "2026-11-18T12:00:00+00:00")
    dex = [
        record for record in opened
        if record["theme"] == "xrpl_dex_xrp_autobridging" and record["entity"] == "XRP"
    ]
    assert len(dex) == 1
    assert dex[0]["independent_source_count"] == 2
    assert "conditional on relative prices and liquidity" in dex[0]["mechanism"]


def test_spot_xrp_etf_thesis_requires_explicit_spot_holding_claims():
    state = {"events": {
        "event-1": _event(
            "A new XRP ETF offers price exposure",
            "XRP",
            "publisher-a",
            claim_text="The ETF offers exposure to XRP price movements.",
            significance="This could increase spot XRP demand through market access.",
        ),
        "event-2": _event(
            "A futures-based XRP ETF launches",
            "XRP",
            "publisher-b",
            claim_text="The futures-based ETF holds cash-settled futures contracts that reference XRP.",
        ),
    }}
    update_theses(state, "2026-11-18T12:00:00+00:00")
    assert not any(
        record["theme"] == "institutional_xrp_market_access"
        for record in state["theses"].values()
    )


def test_spot_xrp_etf_thesis_requires_independent_spot_holding_developments():
    state = {"events": {
        "event-1": _event(
            "Bitwise spot XRP ETF begins trading",
            "XRP",
            "publisher-a",
            claim_text="The Bitwise ETF will hold spot XRP.",
        ),
        "event-2": _event(
            "Grayscale XRP Trust ETF",
            "XRP",
            "publisher-b",
            claim_text="The Grayscale XRP Trust ETF holds XRP to reflect the value of the XRP held by the trust.",
        ),
    }}
    opened = update_theses(state, "2026-11-18T12:00:00+00:00")
    etf = [
        record for record in opened
        if record["theme"] == "institutional_xrp_market_access" and record["entity"] == "XRP"
    ]
    assert len(etf) == 1
    assert etf[0]["independent_source_count"] == 2
    assert "do not demonstrate on-ledger usage" in etf[0]["mechanism"]
