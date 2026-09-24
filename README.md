# XRP Intelligence Feed — v0.3 Discovery Foundation

A free, modular XRP/XRPL source intelligence feed. No paid APIs, API keys, or AI services are required.

## v0.2 pipeline

Enabled registry sources are collected as RSS, normalized into NewsItem, deduplicated against JSON state, entity-matched using configured aliases, classified by source authority tier, and deterministically scored for relevance. The relevance score describes topical relevance only; it does not predict XRP price or market direction.

The pipeline does not publish to Discord yet. discord/webhook.py remains isolated for a later delivery phase.

## v0.3 discovery foundation

The discovery package provides a bounded HTTPS client, a small strategy interface, candidate normalization, dispatch, and a separate fail-closed JSON state file. `DiscoveryCandidate` extends the existing `NewsItem`, so discovery results use the same entity detection, source-quality classification, relevance scoring, and deduplication path rather than creating a second intelligence pipeline.

The first adapter is SEC EDGAR company-submissions discovery. It uses the SEC documented submissions JSON endpoint for explicitly allowlisted issuer CIKs and filing forms. It does not crawl EDGAR or follow arbitrary filing links. Filing URLs are constructed from validated SEC CIK, accession number, and primary-document fields. SEC requests use the identifiable `XRPIntelligenceFeed/0.3` User-Agent; configure `SEC_CONTACT_EMAIL` in the environment to include an actual contact address. No identity is fabricated.

`config/discovery_sources.json` contains the SEC and Federal Register source configurations. The shipped issuer CIK allowlist is empty, so the SEC adapter reports `not_configured` and makes no SEC requests until real issuer CIKs are deliberately added. Filing forms are also explicitly configured and can be narrowed. At most five issuer requests are made per run, spaced at least one second apart. The HTTP client enforces HTTPS, request timeouts, response-size and redirect bounds, conditional ETag/Last-Modified requests, and bounded retries for timeouts, 429, and selected 5xx responses.

The same registry includes a bounded Federal Register API source. It uses only the official HTTPS `www.federalregister.gov/api/v1/documents.json` endpoint and requires no key. The default query terms are `digital assets` and `stablecoin`, with a seven-day publication window and at most ten results per page. Each term refreshes pages 1–2 and can request at most two additional deep pages per run, for a maximum of four requests per term. The document number is the authoritative deep-pagination boundary; the stored page is only a search hint. A shifted boundary is searched in bounded increments, and the boundary advances only after it is found and the relevant pages are complete. Partial/failed requests do not advance pagination. If the API confirms the boundary has aged out of the result window, deep collection for that term pauses without rebasing while shallow refresh continues. Pagination is stored in the existing `state/discovery.json`; no additional state file is used. Terms, agency slugs, document types, lookback, and page size are validated configuration. Empty terms or a disabled source cause `not_configured` and zero requests. Results use Federal Register document numbers as stable candidate IDs. Returned document links are retained only on approved Federal Register or govinfo hosts; links are metadata and are never fetched. Results go through the existing normalization and intelligence stages and are not sent to Discord.

The registry also includes a bounded OFAC Recent Actions HTML adapter at the fixed official `https://ofac.treasury.gov/recent-actions` endpoint. It uses the configured rolling date window and zero-based `page` parameter. Page 0 is refreshed every run; at most two additional sequential recovery/frontier page-fetch operations are made, for a hard maximum of three logical page fetches per adapter run. The existing HTTP client may perform its bounded retries and same-host redirect handling underneath a page fetch. The parser accepts only rows in OFAC's Recent Actions listing and validates each title, date, category, and official action URL. Discovered action pages are provenance only and are never fetched. The validated native action route ID is the candidate identity. The stored boundary ID/date are authoritative; `deep_page_hint` is only a search hint. A boundary is advanced only after its identity is rediscovered and all intervening pages are fully processed. Malformed/failed responses can return valid candidates already parsed but do not advance pagination. Displayed totals and page counts are not used to expire the frontier; a boundary older than the configured rolling window pauses deep progression without rebasing. OFAC progress uses the existing `state/discovery.json` and persists even on runs with no new candidates. OFAC is recognized as a regulator, but authority alone does not make a story relevant: regulatory scoring still requires an action signal and digital-asset/payment signal. OFAC candidates use the existing intelligence path and are not sent to Discord.

The CFTC discovery adapter reads the official General Press Releases and Enforcement Press Releases RSS feeds linked from the [CFTC RSS index](https://www.cftc.gov/RSS/index.htm). Each feed is requested over HTTPS through the bounded HTTP client, with validators tracked independently in the existing `state/discovery.json`. The adapter processes at most 50 entries per feed by default and keeps a configurable 1–180 day lookback. CFTC release numbers from validated official article routes are the stable candidate identities. Entries appearing in both feeds are combined into one candidate while both feed URLs and categories remain in provenance metadata. Article pages are never fetched during discovery. CFTC source authority alone does not make a release relevant; relevance still depends on the existing configured digital-asset, entity, and action signals. The existing CFTC general RSS source remains enabled and is handled by the normal pipeline deduplication.

Discovery state is stored locally in `state/discovery.json`, separately from the existing `state/seen.json`. It records per-request validators, successful fetch time, watermarks, candidate first/last-seen metadata, Federal Register per-term pagination progress, and the OFAC boundary/date/page hint, with deterministic age/count retention and atomic writes. Corrupt state fails closed and is preserved. Both discovery state files are git-ignored. GitHub Actions restores and saves both files using a branch-scoped immutable cache lineage; successful workflow runs persist the state for later scheduled or manual runs.

SEC submissions metadata identifies that a filing exists; it does not include the filing text in this adapter. Relevance therefore reflects the issuer/form metadata available from the submissions record until a separately approved phase adds bounded document-content inspection. Discovery currently covers SEC filing metadata, Federal Register API results, OFAC Recent Actions, and official CFTC press-release RSS feeds; it does not add Congress, GDELT, Ripple/XRPL monitors, generic page or sitemap scraping, or Discord delivery.

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
