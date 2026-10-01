# XRP Intelligence Feed — Intelligence Discovery Feed

A free, modular XRP/XRPL source intelligence feed. No paid APIs, API keys, or AI services are required.

## Pipeline

Enabled registry sources are collected as RSS, normalized into NewsItem, deduplicated against JSON state, entity-matched using configured aliases, classified by source authority tier, and deterministically scored for relevance. The relevance score describes topical relevance only; it does not predict XRP price or market direction.


## Source health

Discovery sources persist lightweight health telemetry in `state/discovery.json`: last attempt/status, candidate count, last error, consecutive hard failures, and consecutive empty/partial runs. A `partial` result is not counted as a hard failure streak; this avoids treating bounded multi-request sources as completely unavailable when only one request had trouble. Health is returned as part of the pipeline result and printed in normal runs.

## Pipeline result model

The pipeline returns a `PipelineResult` containing collected items, fresh items, source failures, collector reports, discovery results, discovery health, publishable items, and separately flagged buried signals. This keeps run-level state explicit instead of attaching reports to the pipeline function itself.

## Buried-signal detection

Fresh candidates that remain below the normal publish threshold can be flagged as buried signals when they have strong cross-source correlation, primary-source quality, and a configured high-value entity. The detector is intentionally separate from relevance scoring: it does not raise relevance scores, bypass the normal publish threshold, deduplicate items, or treat corroboration as proof. Buried signals are currently reported in the run output for later intelligence/publishing policy work; they are deliberately not automatically posted to Discord until live scheduled-run behavior has been observed.

## Cross-source correlation

Fresh candidates are conservatively compared across different sources for likely same-event relationships. Correlation requires a shared high-value entity, publication dates within one day, and meaningful headline overlap. It enriches each item with related source/candidate IDs and a correlation score, but it does not deduplicate items, increase relevance scores, or treat independent reporting as proof of the underlying claim.

Recent correlation memory is stored separately in state/correlation.json; it is not the processed-item state/seen.json. Each bounded card stores only candidate/source identity, publication time, high-value entities, stopworded title tokens, content hash, and last-seen time. Cards are retained for 72 hours and capped at 500 entries, with deterministic oldest-first eviction. Cross-run pairing still uses the same ±1 UTC-day window, different-source requirement, and existing Jaccard/sequence floors. A changed content hash for the same candidate updates its existing card rather than creating a second event. Correlation-state writes occur after enrichment and before state/seen.json is saved; a correlation-state write failure therefore leaves candidates eligible for the next run.

## Institutional coverage

The discovery registry includes official institutional sources for Citi, Circle, Mastercard, Coinbase, Swift, Visa, DBS, J.P. Morgan/Kinexys, and BNY. Institutional candidates use the same normalization, entity detection, source-quality, relevance, deduplication, and Discord publishing path as government and XRPL sources. The feed favors primary-source evidence and does not treat an institution's involvement as proof that XRP is being used.

## CI

Every push and pull request to `main` runs the full pytest suite on Python 3.12 before changes are merged. Production scheduled runs also execute the test suite before collection and publishing.

## Discord posting

Relevant new items (score at or above `publish_score`) are posted to a Discord channel through a webhook. Configuration is by environment variable only; the webhook URL is never stored in the repository.

| Variable | Meaning |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | The channel webhook. If unset, nothing is posted and the run is otherwise unchanged. Must be a `discord.com` or `discordapp.com` webhook URL. |
| `DISCORD_MAX_POSTS` | Most posts per run, 1 to 25 (default 5). If more items qualify, the highest-scoring are selected, oldest first. The rest remain in the persistent delivery outbox and can be posted on a later run. |
| `DISCORD_DRY_RUN` | `true` prints what would be posted, posts nothing, and saves no state, so a live run afterwards still sees the same items as new. |

Each post shows the headline, source, score, matched entities, up to two scoring reasons, and the link. Posts cannot ping `@everyone` or roles. Live Discord delivery uses a bounded `state/outbox.json` queue. Relevant items are queued before `seen.json` is saved; successful posts are removed, while failed or over-cap items remain queued for later scheduled runs. This is deliberately at-least-once delivery: if an outbox cleanup write fails after Discord accepted a post, a duplicate is possible on a later run. The outbox is bounded at 250 items and cached with the other persistent state. A Discord rate limit is retried once if Discord asks for a short wait. Error messages never include the webhook URL.

On GitHub Actions, add the webhook as the repository secret `DISCORD_INTELLIGENCE_DISCORD_WEBHOOK`; the workflow passes it to the program as `DISCORD_WEBHOOK_URL`. Manual runs have a `dry_run` checkbox that defaults to on; untick it to post for real. Dry runs do not save the state cache.

## Discovery foundation

The discovery package provides a bounded HTTPS client, a small strategy interface, candidate normalization, dispatch, and a separate fail-closed JSON state file. `DiscoveryCandidate` extends the existing `NewsItem`, so discovery results use the same entity detection, source-quality classification, relevance scoring, and deduplication path rather than creating a second intelligence pipeline.

The discovery layer now combines bounded primary-source adapters for SEC filings and press releases, Federal Register, OFAC, CFTC, FinCEN, Treasury, FDIC, OCC, Federal Reserve, BIS, White House Presidential Actions, DOJ targeted press releases, Ripple's Press Center, and the XRP Ledger Community Blog. It uses the SEC documented submissions JSON endpoint for explicitly allowlisted issuer CIKs and filing forms. It does not crawl EDGAR or follow arbitrary filing links. Filing URLs are constructed from validated SEC CIK, accession number, and primary-document fields. SEC requests use the identifiable `XRPIntelligenceFeed/0.3` User-Agent; configure `SEC_CONTACT_EMAIL` in the environment to include an actual contact address. No identity is fabricated.

`config/discovery_sources.json` contains the complete enabled discovery registry. Every adapter is bounded, official-source validated, and routed through the same normalization, entity detection, source-quality, relevance, deduplication, and Discord delivery path. The shipped issuer CIK allowlist is empty, so the SEC adapter reports `not_configured` and makes no SEC requests until real issuer CIKs are deliberately added. Filing forms are also explicitly configured and can be narrowed. At most five issuer requests are made per run, spaced at least one second apart. The HTTP client enforces HTTPS, request timeouts, response-size and redirect bounds, conditional ETag/Last-Modified requests, and bounded retries for timeouts, 429, and selected 5xx responses.

The same registry includes a bounded Federal Register API source. It uses only the official HTTPS `www.federalregister.gov/api/v1/documents.json` endpoint and requires no key. The default query terms are `digital assets` and `stablecoin`, with a seven-day publication window and at most ten results per page. Each term refreshes pages 1–2 and can request at most two additional deep pages per run, for a maximum of four requests per term. The document number is the authoritative deep-pagination boundary; the stored page is only a search hint. A shifted boundary is searched in bounded increments, and the boundary advances only after it is found and the relevant pages are complete. Partial/failed requests do not advance pagination. If the API confirms the boundary has aged out of the result window, deep collection for that term pauses without rebasing while shallow refresh continues. Pagination is stored in the existing `state/discovery.json`; no additional state file is used. Terms, agency slugs, document types, lookback, and page size are validated configuration. Empty terms or a disabled source cause `not_configured` and zero requests. Results use Federal Register document numbers as stable candidate IDs. Returned document links are retained only on approved Federal Register or govinfo hosts; links are metadata and are never fetched. Results go through the existing normalization and intelligence stages and are not sent to Discord.

The registry also includes a bounded OFAC Recent Actions HTML adapter at the fixed official `https://ofac.treasury.gov/recent-actions` endpoint. It uses the configured rolling date window and zero-based `page` parameter. Page 0 is refreshed every run; at most two additional sequential recovery/frontier page-fetch operations are made, for a hard maximum of three logical page fetches per adapter run. The existing HTTP client may perform its bounded retries and same-host redirect handling underneath a page fetch. The parser accepts only rows in OFAC's Recent Actions listing and validates each title, date, category, and official action URL. Discovered action pages are provenance only and are never fetched. The validated native action route ID is the candidate identity. The stored boundary ID/date are authoritative; `deep_page_hint` is only a search hint. A boundary is advanced only after its identity is rediscovered and all intervening pages are fully processed. Malformed/failed responses can return valid candidates already parsed but do not advance pagination. Displayed totals and page counts are not used to expire the frontier; a boundary older than the configured rolling window pauses deep progression without rebasing. OFAC progress uses the existing `state/discovery.json` and persists even on runs with no new candidates. OFAC is recognized as a regulator, but authority alone does not make a story relevant: regulatory scoring still requires an action signal and digital-asset/payment signal. OFAC candidates use the existing intelligence path and are not sent to Discord.

The CFTC discovery adapter reads the official General Press Releases and Enforcement Press Releases RSS feeds linked from the [CFTC RSS index](https://www.cftc.gov/RSS/index.htm). Each feed is requested over HTTPS through the bounded HTTP client, with validators tracked independently in the existing `state/discovery.json`. The adapter processes at most 50 entries per feed by default and keeps a configurable 1–180 day lookback. CFTC release numbers from validated official article routes are the stable candidate identities. Entries appearing in both feeds are combined into one candidate while both feed URLs and categories remain in provenance metadata. Article pages are never fetched during discovery. CFTC source authority alone does not make a release relevant; relevance still depends on the existing configured digital-asset, entity, and action signals. The existing CFTC general RSS source remains enabled and is handled by the normal pipeline deduplication.

The FinCEN adapter reads the official [Press Releases listing](https://www.fincen.gov/news/press-releases), paging through the fixed HTTPS host with a maximum of three logical page fetches per run. It processes at most 50 listing rows total per run with a 60-day publication lookback. Validated native release slugs provide stable candidate IDs; article-page URLs are retained as provenance and are never fetched. The saved document boundary is authoritative while `deep_page_hint` is only a search hint. Pagination and per-page ETag/Last-Modified validators use the existing `state/discovery.json`; malformed pages do not advance the frontier or persist that page's validators. FinCEN is recognized as a regulator, but authority alone does not create relevance. Regulatory relevance still requires an action and digital-asset/payment signal.

The Treasury adapter reads the official [Treasury Press Releases listing](https://home.treasury.gov/news/press-releases) with a 60-day lookback, a 50-item run limit, and at most three logical page fetches per run. The live listing uses client-side pagination backed by official annual search JSON shards; the adapter fetches the listing plus at most the two annual shards that can intersect its bounded lookback, then slices those results into the requested page windows. It excludes listing rows explicitly labeled “Statements & Remarks”, uses validated native Treasury release routes for stable candidate IDs, and does not fetch article pages. Its authoritative boundary and page hint are stored in the existing `state/discovery.json`; Treasury publisher attribution remains distinct from OFAC and FinCEN identities. Treasury authority alone does not create relevance.
The FDIC adapter reads the official [FDIC Press Releases listing](https://www.fdic.gov/news/press-releases) using the listing's numbered `?page=N` pagination. Because the listing does not expose a reliable per-item publication date, it deliberately does not invent one or apply a calendar lookback. It accepts only canonical `www.fdic.gov/news/press-releases/YYYY/slug` routes, uses `YYYY/slug` as the stable native identity, and never fetches article pages or the separate GovDelivery feed. Page 0 is refreshed every run; at most two recovery pages are probed, with a shared outbound HTTP-attempt cap. The saved boundary is authoritative and `deep_page_hint` is only a search hint. A boundary is advanced only after it is positively located; an empty deep page does not expire the boundary, and an unlocated boundary does not cause page-0 rows to be falsely reported as new. FDIC publisher authority alone does not create relevance.

The BIS adapter reads the official [BIS media releases RSS feed](https://www.bis.org/doclist/all_pressrels.rss), which is an RSS 1.0 (RDF) document with only the most recent releases. It accepts only `www.bis.org/media-releases/YYYYMMDD-slug` routes, uses that slug as the native identity, and takes the publication time from the feed's `dc:date` exactly as given (date-only, midnight UTC; no time of day is invented). It uses a 120-day lookback because BIS publishes infrequently, relies on the feed's ETag for conditional requests, and never fetches article pages. Entity detection recognizes BIS only by its full name, because the bare acronym also names the U.S. Bureau of Industry and Security.

Discovery state is stored locally in `state/discovery.json`, separately from the existing `state/seen.json`; correlation memory is separately stored in `state/correlation.json`. It records per-request validators, successful fetch time, watermarks, candidate first/last-seen metadata, Federal Register per-term pagination progress, and the OFAC, FinCEN, Treasury, and FDIC boundaries/page hints, with deterministic age/count retention and atomic writes. Corrupt state fails closed and is preserved. Both discovery state files are git-ignored. GitHub Actions restores and saves both files using a branch-scoped immutable cache lineage; successful workflow runs persist the state for later scheduled or manual runs.

SEC submissions metadata identifies that a filing exists; it does not include the filing text in this adapter. Relevance therefore reflects the issuer/form metadata available from the submissions record until a separately approved phase adds bounded document-content inspection. The current discovery registry covers SEC filing metadata, SEC press releases, Federal Register API results, OFAC Recent Actions, CFTC press-release RSS feeds, FinCEN, Treasury, FDIC, Federal Reserve Board feeds, OCC issuances, BIS media releases, White House Presidential Actions, targeted DOJ press releases, Ripple Press Center, and the XRPL Community Blog. It deliberately does not use generic web crawling, GDELT, or paid APIs. Congressional API access is deferred because the official Congress API requires an API key.

## Source registry

`config/discovery_sources.json` uses schema version 1 and a sources list. Every entry has source_id, name, authority_tier, category, url, entity_coverage, enabled, and collection_type. Authority tiers are 1 (primary), 2 (high-signal), and 3 (discovery). Collection currently supports rss.

The enabled registry is intentionally made up of primary or official organizational sources. Each source has a dedicated validator and bounded collection strategy; registry validation rejects malformed entries, duplicate IDs, unsupported methods, and untrusted endpoints.

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

The application reports source collection failures and prints scored new items. State is saved in state/seen.json. GitHub Actions runs on Python 3.12. The scheduled workflow runs every 15 minutes and posts live results using the configured Discord webhook. Manual dispatch retains a dry-run checkbox that defaults to safe/no-post behavior.
