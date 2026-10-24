# XRP Intelligence Feed

A free, source-first intelligence feed for XRP, Ripple, the XRP Ledger, and the wider crypto and financial ecosystem.

It looks beyond headlines that literally mention XRP. Regulatory decisions, government policy, banking and payments infrastructure, stablecoins, tokenization, institutional partnerships, and other developments can matter to crypto even when XRP isn't in the headline.

## What makes it different?

- **Broader signal detection:** Tracks developments across crypto, finance, regulators, governments, and payment networks—not just XRP keywords.
- **Evidence over hype:** Uses source attribution, publication dates, relevance reasons, and supporting links to make it easier to understand why an item matters.
- **Primary-source-first:** Prefers official releases and documents, with clearly distinguished secondary discovery sources when official sites are unavailable.
- **Careful relevance filtering:** Applies explicit scoring and eligibility rules instead of treating every mention as important.
- **Built to avoid noise:** Normalizes headlines, tracks provenance, and deduplicates overlapping reports while preserving distinct developments.
- **Free to run:** Uses Python and GitHub Actions, with Discord delivery; no paid data API is required.

## How it works

1. Collect news and announcements from configured sources.
2. Normalize items and retain source and date information.
3. Detect relevant organizations, assets, and topics.
4. Score items using transparent, configurable rules.
5. Apply freshness, duplicate, and evidence checks before delivery.

The goal is useful situational awareness—not price predictions, promotional hype, or a claim that every signal will affect XRP.

## Coverage

Coverage includes XRP/Ripple/XRPL, crypto markets and infrastructure, stablecoins, tokenization and real-world assets, CBDCs, regulation, government policy, banks, cross-border payments, institutional finance, and relevant partnerships.

Sources are added selectively and validated where practical. A source can remain disabled or marked partial when reliable data cannot be collected. Secondary discovery links are not presented as verified official article URLs.

## Run locally

Requires Python 3.12 (the version used in GitHub Actions).

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python main.py
```

The local run prints scored new items and stores deduplication state under `state/`. GitHub Actions handles automated collection. Discord delivery and intelligence-event publishing have separate safety controls; routine intelligence-event publishing is not enabled by default.

## Project principles

- Prefer verifiable evidence and honest source-health reporting.
- Never invent publication dates when a source omits them.
- Keep relevance thresholds and delivery safeguards explicit.
- Treat inference as a way to connect evidence—not as permission to invent facts.
- Keep launch resets and production-state changes deliberate and reviewable.

## Status and development

The project is actively developed. See [Issues](https://github.com/punkgenius84/xrp-intelligence-feed/issues) for specific source gaps and engineering work, and [Actions](https://github.com/punkgenius84/xrp-intelligence-feed/actions) for automated test and workflow runs.
