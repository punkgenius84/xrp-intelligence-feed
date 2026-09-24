# XRP Intelligence Feed — v0.3 Discovery Foundation

A free, modular XRP/XRPL source intelligence feed. No paid APIs, API keys, or AI services are required.

## v0.2 pipeline

Enabled registry sources are collected as RSS, normalized into NewsItem, deduplicated against JSON state, entity-matched using configured aliases, classified by source authority tier, and deterministically scored for relevance. The relevance score describes topical relevance only; it does not predict XRP price or market direction.

The pipeline does not publish to Discord yet. discord/webhook.py remains isolated for a later delivery phase.

## v0.3 discovery foundation

The discovery package provides a bounded HTTPS client, a small strategy interface, candidate normalization, dispatch, and a separate fail-closed JSON state file. `DiscoveryCandidate` extends the existing `NewsItem`, so discovery results use the same entity detection, source-quality classification, relevance scoring, and deduplication path rather than creating a second intelligence pipeline.

The first adapter is SEC EDGAR company-submissions discovery. It uses the SEC documented submissions JSON endpoint for explicitly allowlisted issuer CIKs and filing forms. It does not crawl EDGAR or follow arbitrary filing links. Filing URLs are constructed from validated SEC CIK, accession number, and primary-document fields. SEC requests use the identifiable `XRPIntelligenceFeed/0.3` User-Agent; configure `SEC_CONTACT_EMAIL` in the environment to include an actual contact address. No identity is fabricated.

`config/discovery_sources.json` contains the SEC and Federal Register source configurations. The shipped issuer CIK allowlist is empty, so the SEC adapter reports `not_configured` and makes no SEC requests until real issuer CIKs are deliberately added. Filing forms are also explicitly configured and can be narrowed. At most five issuer requests are made per run, spaced at least one second apart. The HTTP client enforces HTTPS, request timeouts, response-size and redirect bounds, conditional ETag/Last-Modified requests, and bounded retries for timeouts, 429, and selected 5xx responses.

The same registry includes a bounded Federal Register API source. It uses only the official HTTPS `www.federalregister.gov/api/v1/documents.json` endpoint and requires no key. The default query terms are `digital assets` and `stablecoin`, with a seven-day publication window, at most ten results per page, and two pages per term. Terms, agency slugs, document types, lookback, page size, and page count are validated configuration. Empty terms or a disabled source cause `not_configured` and zero requests. Results use Federal Register document numbers as stable candidate IDs. Returned document links are retained only on approved Federal Register or govinfo hosts; links are metadata and are never fetched. Results go through the existing normalization and intelligence stages and are not sent to Discord.

Discovery state is stored locally in `state/discovery.json`, separately from the existing `state/seen.json`. It records per-request validators, successful fetch time, watermarks, and candidate first/last-seen metadata, with deterministic age/count retention and atomic writes. Corrupt state fails closed and is preserved. Both discovery state files are git-ignored. GitHub Actions cache integration for discovery state is deferred to Phase 4; the existing workflow and v0.2 cache behavior are unchanged in this phase.

SEC submissions metadata identifies that a filing exists; it does not include the filing text in this adapter. Relevance therefore reflects the issuer/form metadata available from the submissions record until a separately approved phase adds bounded document-content inspection. This phase adds only Federal Register API discovery; it does not add Congress, GDELT, Ripple/XRPL monitors, page or sitemap monitoring, or Discord delivery.

## Source registry

config/sources.json uses schema version 1 and a sources list. Every entry has source_id, name, authority_tier, category, url, entity_coverage, enabled, and collection_type. Authority tiers are 1 (primary), 2 (high-signal), and 3 (discovery). Collection currently supports rss.

The initial enabled registry is deliberately small: SEC, CFTC, OCC, and Federal Reserve official press-release feeds. These endpoints are linked from each agency’s official RSS listing. Other requested organizations are not enabled until a supported feed endpoint is verified. Registry validation rejects malformed entries and duplicate IDs.

## Entity detection and relevance

config/entities.json supplies canonical entities and aliases. Matching is case-insensitive and uses word boundaries to avoid matching short aliases inside unrelated words. Matches are stored on each item.

Source quality maps tiers to primary, high_signal, or discovery. config/keywords.json and config/thresholds.json drive fixed additive relevance rules. Direct asset/company signals and domain-relevant regulatory actions are distinguished from broad tokenization coverage; primary authority and entity counts alone do not qualify generic stories. Each scored item retains a numeric score, human-readable reasons, machine-readable signals, and relevance categories. Scores are capped at 100. The existing threshold and weights are unchanged. No cross-source corroboration is treated as independent event confirmation.

Distinctive exact or near-identical headlines are deduplicated within the same UTC publication day (collection day when publication time is unavailable). Additional source IDs and URLs are retained on the first item as provenance, without increasing the score. Similar headlines on different days are separate events. Existing URL-plus-title fingerprints remain supported, and the same JSON state file continues to be used.

RSS requests use bounded connect/read timeouts. Each source reports success, empty, partial malformed, malformed, HTTP failure, timeout, or request failure; useful entries from partially malformed feeds are retained. Unsupported collection types fail clearly.

The JSON state is saved only after entity detection, source-quality classification, and relevance scoring complete. State is written atomically and retains the most recent 25,000 fingerprints in deterministic order. On GitHub Actions, the workflow restores and saves this file using GitHub's free Actions cache with a unique per-run key and a branch-scoped restore prefix. This avoids committing state or triggering workflow loops. Local runs continue to use state/seen.json, which is ignored by git.

## Running locally

    python -m pip install -r requirements.txt
    python -m pytest -q
    python main.py

The application reports source collection failures and prints scored new items. State is saved in state/seen.json. GitHub Actions continues to run on Python 3.12; the new intelligence processing uses Python 3.12-compatible syntax and the standard library.
