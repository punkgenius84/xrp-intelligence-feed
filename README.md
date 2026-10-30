# XRP Intelligence Feed

A free, source-first intelligence feed for XRP, Ripple, the XRP Ledger, and the wider crypto and financial ecosystem.

It looks beyond headlines that literally mention XRP. Regulatory decisions, government policy, banking and payments infrastructure, stablecoins, tokenization, institutional partnerships, and other developments can matter to crypto even when XRP isn't in the headline.

## What it does

- **Broad signal detection:** Tracks developments across crypto, finance, regulators, governments, and payment networks—not just XRP keywords.
- **Evidence over hype:** Uses source attribution, publication dates, relevance reasons, and supporting links to show why an item may matter.
- **Primary-source-first:** Prefers official releases and documents, while clearly distinguishing secondary discovery sources when official sites are unavailable.
- **Careful relevance filtering:** Applies explicit scoring, freshness, duplicate, and evidence checks before delivery.
- **Free to run:** Uses Python and GitHub Actions, with Discord delivery; no paid data API is required.

## Publishing

The **XRP Intelligence Feed** GitHub Actions workflow runs automatically on its configured schedule and publishes eligible items to Discord.

For a manual run:

1. Open the repository's **Actions** tab.
2. Select **XRP Intelligence Feed** and choose **Run workflow**.
3. Leave **Dry run** checked to collect and analyze without posting to Discord or saving production state.
4. Uncheck **Dry run** only when you intend to publish and persist state.

Scheduled runs publish automatically. Manual runs default to dry-run mode as a safety measure. Push-triggered diagnostic runs do not publish or update production state.

### Publishing limits

- The regular Discord feed can publish up to **8 individually relevant news items per run**. This is a delivery cap, not a relevance threshold; freshness and source/evidence checks still apply.
- The intelligence stage considers up to **12 fresh candidates per run**, with a minimum relevance score of **30** in GitHub Actions, before grouping related evidence into intelligence events.
- Intelligence-event publishing is enabled for scheduled production runs. It is not enabled for push diagnostics or manual dry runs.
- Related developments are grouped only when there is supporting evidence; shared words alone are not proof of a connection.

## Coverage

Coverage includes XRP/Ripple/XRPL, crypto markets and infrastructure, stablecoins, tokenization and real-world assets, CBDCs, regulation, government policy, banks, cross-border payments, institutional finance, and relevant partnerships.

Sources are added selectively and validated where practical. A source can remain disabled or marked partial when reliable data cannot be collected. Secondary discovery links are not presented as verified official article URLs.

## Fresh-start launch

The **Fresh Start Launch Reset** workflow is a separate, deliberate operation. It previews the current state by default. Applying a reset requires the main branch, a restored production state cache, and the exact confirmation phrase shown in the workflow.

Before clearing operational state, it creates and verifies a pre-reset archive. It clears active deduplication and delivery history, preserves quarantine audit records, verifies the operational state is empty, and does not send Discord messages. Do not apply the reset until you are ready for the fresh production start; the normal scheduled feed should remain untouched until then.

## Run locally

Requires Python 3.12 (the version used in GitHub Actions).

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python main.py
```

The local run prints scored new items and stores deduplication state under `state/`. GitHub Actions handles automated collection and production Discord delivery.

## Project principles

- Prefer verifiable evidence and honest source-health reporting.
- Never invent publication dates when a source omits them.
- Keep relevance thresholds and delivery safeguards explicit.
- Treat inference as a way to connect evidence—not as permission to invent facts.
- Keep launch resets and production-state changes deliberate and reviewable.

## Status and development

See [Actions](https://github.com/punkgenius84/xrp-intelligence-feed/actions) for test and production runs, and [Issues](https://github.com/punkgenius84/xrp-intelligence-feed/issues) for tracked source gaps and engineering work.
