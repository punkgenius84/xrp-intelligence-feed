# XRP Intelligence Feed

A free, source-first intelligence feed for XRP, Ripple, the XRP Ledger, and the wider crypto and financial ecosystem.

It looks beyond headlines that literally mention XRP. Regulatory decisions, government policy, banking and payments infrastructure, stablecoins, tokenization, institutional partnerships, and other developments can matter to crypto even when XRP isn't in the headline.

## What it does

- **Broad signal detection:** Tracks developments across crypto, finance, regulators, governments, and payment networks—not just XRP keywords.
- **Evidence over hype:** Uses source attribution, publication dates, relevance reasons, and supporting links to show why an item may matter.
- **Primary-source-first:** Prefers official releases and documents, while clearly distinguishing secondary discovery sources when official sites are unavailable.
- **Careful relevance filtering:** Applies explicit scoring, freshness, duplicate, and evidence checks before delivery.
- **Grounded intelligence enrichment:** Uses the language model to organize claims from collected evidence, not to create facts. Generated claims are checked against source evidence and entity, metadata, and event-type constraints.
- **Free to run:** Uses Python and GitHub Actions, with Discord delivery; no paid data API is required.

## Intelligence enrichment and validation

Eligible candidates can be grouped into related intelligence events and enriched with source-grounded claims.

- **Single-source and multi-source handling:** A one-article event follows the dedicated single-source analysis path. Events with multiple supporting sources use the multi-source path.
- **Factual-anchor validation:** Important names, identifiers, figures, and other factual anchors in generated claims must be supported by the evidence. Alphanumeric identifiers such as `B2B` are treated as complete tokens rather than being split into misleading fragments.
- **Targeted correction attempts:** When claim validation identifies an empty or unsupported claim, the model receives focused feedback and can make up to two targeted retries (three generation attempts total). Every attempt is checked by the same validators; retries do not bypass evidence checks. Provider-unavailable errors are not retried.
- **Conservative institutional relevance:** A bank-charter development can qualify through a narrowly defined category only when the evidence establishes bank-charter context, names an applicable authority (OCC, Federal Reserve, or FDIC), and describes an explicit charter action. Generic bank coverage or an unnamed regulator is not enough by itself.
- **No automatic promotion of speculation:** A candidate that cannot support its claims remains ineligible or is handled by the existing validation/quarantine safeguards rather than being presented as established fact.

The relevance category for bank-charter actions is intentionally narrow. It does not make ordinary banking news, generic regulatory commentary, or every OCC mention relevant to XRP.

## Publishing

The **XRP Intelligence Feed** GitHub Actions workflow runs automatically on its configured schedule and publishes eligible items to Discord.

For a manual run:

1. Open the repository's **Actions** tab.
2. Select **XRP Intelligence Feed** and choose **Run workflow**.
3. The current workflow presents **Dry run** as checked by default. Leave it checked to collect and analyze without posting to Discord or saving production state.
4. Uncheck **Dry run** only when you deliberately intend to publish and persist production state.

Scheduled runs publish automatically. Push-triggered diagnostic runs do not publish or update production state. Manual-run behavior is controlled by the workflow's existing `workflow_dispatch` input; this README documents that behavior but does not configure it.

### Publishing limits and safeguards

- The regular Discord feed can publish up to **8 individually relevant news items per run**. This is a delivery cap, not a relevance threshold; freshness and source/evidence checks still apply.
- The intelligence stage considers up to **12 fresh candidates per run**, with a minimum relevance score of **30** in GitHub Actions, before grouping related evidence into intelligence events.
- The workflow excludes items older than **14 days** from the regular Discord feed.
- Intelligence-event publishing is enabled for scheduled production runs. Manual dry runs and push diagnostics do not publish intelligence events.
- Related developments are grouped only when there is supporting evidence; shared words alone are not proof of a connection.
- A failed or unsupported model-generated claim does not become valid merely because a retry was attempted.

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
