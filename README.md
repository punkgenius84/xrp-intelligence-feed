# XRP Intelligence Feed

A free, primary-source-first XRP/XRPL intelligence feed. It collects official government, regulator, XRPL, Ripple, and institutional sources, scores relevance deterministically, and can deliver qualifying items to Discord.

**Current status:** deterministic intelligence processing is live in GitHub Actions, while **intelligence-event Discord publishing remains OFF**. The system is deliberately observing real shadow output before enabling that final delivery step.



## Launch checkpoint — 22 October 2026

- **Routine intelligence-event publishing remains disabled.** The normal scheduled feed is still in shadow mode for intelligence events; do not change `INTELLIGENCE_PUBLISH=false` as part of routine cleanup.
- The guarded one-event test requires an exact event ID, `dry_run=true`, and the confirmation `POST-ONE-INTELLIGENCE-EVENT`. There is no hardcoded fallback event ID.
- The pre-launch queue reset archived the prior 19 active events. Delivery history and deduplication state were preserved; the reset did not post to Discord.
- The post-merge push diagnostic (#445, 13:42:25Z) was read-only and did not persist state or post to Discord. The next scheduled run (#446, started 14:28:31Z) completed successfully, passed **1,037 tests**, restored 1,516 seen entries and saved state caches. It reported 2 fresh intelligence candidates, 0 meeting the relevance threshold, 0 queued intelligence events, 20 quarantined events preserved, 29 regular delivery-history entries, 2 intelligence delivery-history entries, and 0 Discord posts. This is a successful scheduled observation, but it does not validate publishing.
- **Schedule-cadence warning remains open:** the workflow is configured for four runs per hour at `8,23,38,53 * * * *`. Run #446 is fresh evidence that scheduling resumed, but the gap from the prior scheduled run (#444 at 08:56:43Z) to #446 (14:28:31Z) was still several hours. A single new scheduled run does not prove the 15-minute cadence is restored. GitHub scheduled events can be delayed or dropped; continue observing real scheduled-run timestamps before calling this fixed.
- **Source health:** Coinbase remains partial (17 candidates, 3 incomplete/empty outcomes), with one official-index article route unresolved because no reliable publication date could be established. Its first-seen timestamp is not a publication date. Citi returned 0 candidates in this production run. The collector intentionally short-circuits to success with no candidates when the saved sitemap digest is unchanged; that makes an unchanged sitemap a plausible explanation, not a proven explanation for this specific run because the post-merge production run #447 confirmed `Citi sitemap digest: unchanged`. The zero-candidate result was a no-change fast path, not evidence of a parser defect. SWIFT and IMF sources remain not configured, pending reliable access.
- The launch gate remains: observe a genuinely fresh, relevant queued event, review its evidence and date, run the guarded one-event delivery manually, and verify both the Discord post and persisted delivery history. Do not clear quarantine or enable routine intelligence publishing as part of diagnostics.

## What it does

1. Collects bounded feeds and official discovery sources.
2. Normalizes and deduplicates candidates.
3. Detects XRP/XRPL/Ripple and related entities.
4. Assigns an auditable relevance score.
5. Posts qualifying news to Discord through a durable outbox.
6. Optionally enriches qualifying items with local Ollama inference.
7. Validates intelligence claims against explicit source evidence before an intelligence event can be delivered.

The relevance score measures **topical relevance only**. It is not a price prediction, trading signal, or claim that XRP is actually involved in every institutional, regulatory, tokenization, or stablecoin story.

## What a Discord news post looks like

A normal post identifies the source, publication date, headline, deterministic `Why this fired` reasons, matched entities, relevance score, and source link.

Institutional or government coverage is **not automatically XRP news**. The feed intentionally distinguishes:
- **XRP** — the asset.
- **XRPL** — the ledger/network.
- **Ripple** — the company.
- **RLUSD** — Ripple's stablecoin.
- **Stablecoin/tokenization/payment activity** — broader institutional activity that may be relevant without proving XRP or XRPL usage.

## Intelligence layer

### Permanent historical context

The intelligence model has access to a separate, curated historical context pack at config/historical_context.json. This is **institutional memory**, not a replacement for the bounded news lookback.

The distinction matters:

- The normal news/discovery lookback answers **what happened recently**.
- Historical context records source-linked relationships and developments that may be years old but are still useful for recognizing the significance of a new event.
- Historical records do **not** expire on a 180-day rolling window.
- At most four relevant historical records are selected for an article based on explicit entity aliases.
- Historical records are background only; current claims must still be grounded in the current article/source evidence.

The context pack deliberately tracks relationships such as historical XRP/ODL deployments, Ripple network relationships, bank/payment relationships, government exploration, RLUSD, custody, tokenization, the SEC case's procedural resolution, Ripple Prime/Hidden Road, GTreasury, Ripple Treasury's digital-asset launch, and recent regulatory milestones. Each record carries a date, entities, classification, status caveat, and HTTPS source URL.

The model is explicitly instructed not to turn historical relationships into claims of current activity. In particular, it must distinguish:

- **Ripple** — the company.
- **XRP** — the digital asset.
- **XRPL/XRP Ledger** — the ledger/network.
- **RLUSD** — Ripple's stablecoin.
- **RippleNet/payment-network relationships** — not automatically evidence of XRP use.
- **Historical pilots, announced partnerships, or MoUs** — not automatically proof of current production activity.

See docs/historical-context.md for maintenance rules and the current coverage philosophy. Expansion should be research-driven and source-linked rather than becoming an unverified list of every historical Ripple mention.


The intelligence layer runs downstream of deterministic relevance. The LLM cannot promote an item into relevance, invent facts or sources, or bypass evidence validation.

GitHub Actions currently runs `qwen2.5:3b` through Ollama in shadow mode. Intelligence events require grounded claims with controlled evidence references. **`INTELLIGENCE_PUBLISH=false` remains intentional until real scheduled output has been reviewed for semantic accuracy as well as evidence quality.**

## Quick start

    python -m pip install -r requirements.txt
    python -m pytest -q
    python main.py

For local intelligence previews, set `INTELLIGENCE_ENABLED=true`. Intelligence publication requires the separate `INTELLIGENCE_PUBLISH=true` flag and a configured Discord webhook.

## Fixed headline evaluation

The repository includes ten source-linked headline cases covering ended relationships, historical XRP/ODL use, RLUSD, custody, tokenization, acquisitions, the SEC case, treasury capabilities, and institutional prime brokerage. This set checks event classification and common overclaiming/conflation errors against validated model output.

With Ollama running locally and the configured model installed, run from the repository root:

```powershell
python scripts/evaluate_intelligence.py
```

Use `--case <id>` to run a specific case or `--model <ollama-model>` to select a model. A passing result is a quality screen, not proof of correctness or permission to enable intelligence-event publishing. Review output against the linked source and keep `INTELLIGENCE_PUBLISH=false` until scheduled shadow output has been accepted.

## Current production posture

The project has two separate delivery paths. Normal deterministic news posts can be delivered when configured; intelligence-event delivery is deliberately kept separate and remains disabled while shadow output is being validated.

- Normal relevant-news Discord delivery: **enabled when the webhook is configured**.
- Intelligence processing: **enabled in GitHub Actions shadow mode**.
- Intelligence-event Discord delivery: **disabled pending shadow-output validation**.
- Historical institutional context: **enabled as background context for intelligence analysis**.
- Evidence validation: **fail closed**.
- Persistent state: restored/saved through GitHub Actions cache; intelligence, correlation, discovery, and delivery state are kept separate.
- No paid APIs are required.


## Pipeline

Enabled registry sources are collected as RSS, normalized into NewsItem, deduplicated against JSON state, entity-matched using configured aliases, classified by source authority tier, and deterministically scored for relevance. The relevance score describes topical relevance only; it does not predict XRP price or market direction.


## Source health

Discovery sources persist lightweight health telemetry in `state/discovery.json`: last attempt/status, candidate count, last error, consecutive hard failures, and consecutive empty/partial runs. A `partial` result is not counted as a hard failure streak; this avoids treating bounded multi-request sources as completely unavailable when only one request had trouble. Health is returned as part of the pipeline result and printed in normal runs.

### Undated official route observations

A source may opt into `track_undated_observations: true` when its official index exposes an article title and URL but no defensible publication date. For an opted-in source, `state/discovery.json` retains the route, title, first-seen time, last-seen time, and a title/URL fingerprint (capped at 500 routes per source). The feed logs a route when it is first observed or its title/URL changes; unchanged observations stay quiet.

**Observation time is not publication time.** An undated observation is not a `DiscoveryCandidate`, is not eligible for relevance scoring or Discord publishing, and does not let the collector borrow a neighboring card's date. The option is currently enabled only for Coinbase's official blog index, where the date remains unresolved. Other sources' observation records are cleared unless they explicitly opt in.

## Pipeline result model

The pipeline returns a `PipelineResult` containing collected items, fresh items, source failures, collector reports, discovery results, discovery health, publishable items, and separately flagged buried signals. This keeps run-level state explicit instead of attaching reports to the pipeline function itself.

## Intelligence state retention

Persistent intelligence state is bounded at 500 current event records, with at most 10 revisions retained per event. Oldest records are evicted deterministically so a 15-minute schedule cannot grow `state/intelligence.json` without limit.

## Buried-signal detection

Fresh candidates that remain below the normal publish threshold can be flagged as buried signals when they have strong cross-source correlation, primary-source quality, and a configured high-value entity. The detector is intentionally separate from relevance scoring: it does not raise relevance scores, bypass the normal publish threshold, deduplicate items, or treat corroboration as proof. Buried signals are currently reported in the run output for later intelligence/publishing policy work; they are deliberately not automatically posted to Discord until live scheduled-run behavior has been observed.

## Cross-source correlation

Fresh candidates are conservatively compared across different sources for likely same-event relationships. Correlation requires a shared high-value entity, publication dates within one day, and meaningful headline overlap. It enriches each item with related source/candidate IDs and a correlation score, but it does not deduplicate items, increase relevance scores, or treat independent reporting as proof of the underlying claim.

Recent correlation memory is stored separately in state/correlation.json; it is not the processed-item state/seen.json. Each bounded card stores only candidate/source identity, publication time, high-value entities, stopworded title tokens, content hash, and last-seen time. Cards are retained for 72 hours and capped at 500 entries, with deterministic oldest-first eviction. Cross-run pairing still uses the same ±1 UTC-day window, different-source requirement, and existing Jaccard/sequence floors. A changed content hash for the same candidate updates its existing card rather than creating a second event. Correlation-state writes occur after enrichment and before state/seen.json is saved; a correlation-state write failure therefore leaves candidates eligible for the next run.

## Institutional coverage

The discovery registry includes official institutional sources for Citi, Circle, Mastercard, Coinbase, Swift, Visa, DBS, J.P. Morgan/Kinexys, and BNY. Institutional candidates use the same normalization, entity detection, source-quality, relevance, deduplication, and Discord publishing path as government and XRPL sources. The feed favors primary-source evidence and does not treat an institution's involvement as proof that XRP is being used.

## Workflow supply-chain security

External GitHub Actions are pinned to full commit SHAs, with weekly Dependabot checks configured to propose deliberate updates. The production workflow installs a fixed Ollama release archive and verifies its published SHA-256 before extraction. The expected archive version and digest must be updated together from the official release when intentionally upgrading Ollama. CI tests enforce SHA pinning and the Ollama checksum gate.

## GitHub Actions workflow guide

| Workflow | Purpose | Operational notes |
| --- | --- | --- |
| **XRP Intelligence Feed** | Scheduled collection, deterministic scoring, intelligence shadow processing, and manual preview/one-event test. | Scheduled every 15 minutes at minutes 8, 23, 38, and 53. Normal intelligence-event publishing stays disabled. The isolated one-event path is manual and confirmation-gated. |
| **Test Suite** | Full pytest suite on pushes and pull requests to `main`. | Required regression gate; keep it. |
| **Intelligence Headline Evaluation** | Manual Ollama/model quality evaluation against the fixed source-linked cases. | A quality screen only; it does not authorize publishing. |
| **Clean Intelligence Launch Queue** | Manual, cache-aware queue reset with audit archive. | Preview-only by default; apply requires explicit confirmation. Do not rerun apply casually. |
| **Quarantine Stale Intelligence Queue Items** | Manual quarantine of events whose latest trustworthy evidence is older than 30 days. | Preview-only by default; apply is confirmation-gated and never publishes. |
| **Quarantine One Malformed Intelligence Event** | Manual quarantine of one exact event with generic/invalid evidence. | Requires an exact event ID; saves quarantine audit before updated active state. Never publishes. |
| **XRP Intelligence Weekly Digest** | Posts a weekly digest of delivery history to Discord. | Separate from intelligence-event publishing; keep only if the weekly summary is wanted. It is a real posting workflow, not a test. |
| **ECB / Bank of Canada / Bank of England RSS probes** | Read-only runner checks for enabled official feeds. | Retain as source-health diagnostics; no state writes or Discord posts. |
| **Coinbase / SWIFT / IMF probes** | Read-only diagnostics for sources with unresolved access/date limitations. | Retain while those source issues are being investigated. Results do not bypass access controls or enable parked sources. |

The obsolete marker-commit workflow for generic-event quarantine has been removed. The supported manual quarantine workflow remains. Avoid adding workflows that mutate the same production cache without the feed's concurrency lock and an explicit preview/confirmation gate.

## CI

Every push and pull request to `main` runs the full pytest suite on Python 3.12 before changes are merged. Production scheduled runs also execute the test suite before collection and publishing.

## License

This repository is released under the MIT License. See `LICENSE`.

## Discord posting

Relevant new items (score at or above `publish_score`) are posted to a Discord channel through a webhook. Configuration is by environment variable only; the webhook URL is never stored in the repository.

| Variable | Meaning |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | The channel webhook. If unset, nothing is posted and the run is otherwise unchanged. Must be a `discord.com` or `discordapp.com` webhook URL. |
| `DISCORD_MAX_POSTS` | Most posts per run, 1 to 25 (default 5). If more items qualify, the highest-scoring are selected, oldest first. The rest remain in the persistent delivery outbox and can be posted on a later run. |
| `DISCORD_DRY_RUN` | `true` prints what would be posted, posts nothing, and saves no state, so a live run afterwards still sees the same items as new. |

Each post leads with the source and publication date, followed by the headline and a plain-English `Why this fired` line from the deterministic scoring reasons. Matched entities follow, with the numeric relevance score at the bottom. The source link is always included. Posts cannot ping `@everyone` or roles. Live Discord delivery uses a bounded `state/outbox.json` queue. Relevant items are queued before `seen.json` is saved; successful posts are removed, while failed or over-cap items remain queued for later scheduled runs. This is deliberately at-least-once delivery: if an outbox cleanup write fails after Discord accepted a post, a duplicate is possible on a later run. The outbox is bounded at 250 items and cached with the other persistent state. Successfully posted items are also retained in bounded `state/delivery_history.json` for 35 days, capped at 2,000 records. A Discord rate limit is retried once if Discord asks for a short wait. Error messages never include the webhook URL.

The optional local Ollama intelligence layer is disabled by default. GitHub Actions currently enables intelligence processing in shadow mode with `INTELLIGENCE_ENABLED=true` and keeps `INTELLIGENCE_PUBLISH=false`. A second independent `INTELLIGENCE_PUBLISH=true` flag is required before intelligence events can post to the existing Discord channel. Publication fails closed if an event has no evidence URL.

On GitHub Actions, add the webhook as the repository secret `DISCORD_INTELLIGENCE_DISCORD_WEBHOOK`; the workflow passes it to the program as `DISCORD_WEBHOOK_URL`. Manual runs have a `dry_run` checkbox that defaults to on; untick it to post for real. Dry runs do not save the state cache.

## Weekly digest

A deterministic weekly digest runs on Sunday and posts to the same Discord channel. It summarizes items that were actually delivered during the previous seven days, including source, publication date, title, and the existing `Why this fired` reason. It does not include merely discovered, queued, or buried items.

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

Source quality maps tiers to primary, high_signal, or discovery. config/keywords.json and config/thresholds.json drive fixed additive relevance rules. Direct asset/company signals and domain-relevant regulatory actions are distinguished from broad tokenization coverage; primary authority and entity counts alone do not qualify generic stories. Each scored item retains a numeric score, human-readable reasons, machine-readable signals, and relevance categories. Scores are capped at 100. The minimum relevance publish score and the intelligence enrichment minimum are both 30 (lowered from 35 to admit more relevant institutional/payment/regulatory stories without bypassing evidence and freshness rules); scoring weights are unchanged. No cross-source corroboration is treated as independent event confirmation.

Distinctive exact or near-identical headlines are deduplicated within the same UTC publication day (collection day when publication time is unavailable). Additional source IDs and URLs are retained on the first item as provenance, without increasing the score. Similar headlines on different days are separate events. Existing URL-plus-title fingerprints remain supported, and the same JSON state file continues to be used.

RSS requests use bounded connect/read timeouts. Each source reports success, empty, partial malformed, malformed, HTTP failure, timeout, or request failure; useful entries from partially malformed feeds are retained. Unsupported collection types fail clearly.

The JSON state is saved only after entity detection, source-quality classification, and relevance scoring complete. State is written atomically and retains the most recent 25,000 fingerprints in deterministic order. On GitHub Actions, the workflow restores and saves this file using GitHub's free Actions cache with a unique per-run key and a branch-scoped restore prefix. This avoids committing state or triggering workflow loops. Local runs continue to use state/seen.json, which is ignored by git.

## Running locally

    python -m pip install -r requirements.txt
    python -m pytest -q
    python main.py

The application reports source collection failures and prints scored new items. State is saved in state/seen.json. GitHub Actions runs on Python 3.12. The scheduled workflow runs four times per hour at minutes 8, 23, 38, and 53. It runs the full test suite, restores persistent state, runs the intelligence layer in shadow mode, and then executes the feed. Manual dispatch retains a dry-run checkbox that defaults to safe/no-post behavior.

## Intelligence queue review

The intelligence publisher holds stories with missing/invalid dates and quarantines stories whose latest evidence is older than the 30-day publication window. Quarantined records retain their summary, original evidence URLs, dates, queue time, and reason; quarantine does not publish them.

Preview the quarantined records locally without changing state or posting to Discord:

    python scripts/preview_intelligence_quarantine.py --limit 20

Use `--path` to inspect a copied quarantine JSON file. The preview command is read-only and limits output to five evidence records per event.

### Stale-only queue quarantine (manual, production-cache aware)

Use the **Quarantine Stale Intelligence Queue Items** workflow to archive only queued events whose latest trustworthy evidence is older than 30 days. The workflow is read-only by default.

- Run the workflow with `apply_quarantine=false` first and inspect the preview. It reports the stale count without changing either state store.
- Only when the stale items are intentionally being held out of the active launch queue, set `apply_quarantine=true` and enter the exact confirmation `QUARANTINE-STALE-INTELLIGENCE`.
- It requires a restored production state cache and shares the production feed concurrency lock. It archives the stale records to the existing quarantine store before removing only those stale keys from the active outbox.
- Fresh, undated, and future-dated events remain active. If the quarantine write, collision check, or active queue update fails, the workflow fails closed; delivery history, source pagination, `seen.json`, and deduplication state are not reset.
- This workflow never publishes to Discord. It is separate from the full pre-launch reset below, which archives and clears every queued item.

Do not apply stale-only quarantine automatically or treat the preview recommendation as editorial approval.

### Pre-launch queue reset (manual, production-cache aware)

The active intelligence outbox is persisted in GitHub Actions caches, so running the Python cleanup script only in a local clone does **not** reset the production queue. Use the manual **Clean Intelligence Launch Queue** workflow from the Actions tab when the launch reset is intentionally approved.

- First run it with `apply_reset=false` (the default) and review the read-only queue preview.
- Only at the agreed launch-preparation point, set `apply_reset=true` and enter the exact confirmation `CLEAN-INTELLIGENCE-QUEUE`.
- The workflow refuses to apply if it cannot restore a production state cache. It archives every active queued event to the separate quarantine cache before saving the empty outbox, and shares the production feed's concurrency lock to avoid racing a scheduled run.
- The workflow does not post to Discord and does not reset delivery history, `seen.json`, source pagination, or deduplication state. If archiving or saving the audit copy fails, it does not advance the active queue cache.

Do not run the apply mode until the first-live-post plan is approved; routine intelligence publishing remains disabled.

## Undated official-source observations

Some official index pages expose a reliable headline and URL but no publication date. The Coinbase adapter records these routes only for change detection; they are never published as news candidates without a trustworthy publication date. The state records when the feed first and most recently observed a route, which is acquisition timing—not the article's publication date. When a headline changes, the previous and current titles are logged and the latest previous title is retained in state for review.

Inspect saved observations locally without changing state or posting to Discord:

    python scripts/preview_discovery_observations.py --source-id coinbase-blog --limit 50

Omit `--source-id` to inspect observations from all opted-in sources. Use `--path` to inspect a copied `state/discovery.json` file. The preview is read-only.

## Source availability notes

- **Bank of England news RSS is enabled after a successful runner probe.** Its official [RSS feeds page](https://www.bankofengland.co.uk/rss) links to `https://www.bankofengland.co.uk/rss/news`. The 22 October 2026 GitHub Actions probe returned HTTP 200, parsed 50 entries with no parser errors, and all 12 sampled entries had publication dates and official HTTPS links. The adapter is bounded to 50 items and a 180-day lookback, accepts only official HTTPS article links, uses conditional request validators, and sends results through existing relevance scoring. The probe itself writes no state and does not publish.
- **European Central Bank RSS is enabled after a successful runner probe.** The source uses the ECB's documented official press feed at [`https://www.ecb.europa.eu/rss/press.html`](https://www.ecb.europa.eu/rss/press.html). The 21 October 2026 GitHub Actions probe parsed 15 dated official communications with zero errors. The feed includes press releases, speeches, interviews and press-conference communications; only dated, allowlisted HTTPS `/press/` links are accepted. The adapter is bounded to 50 items and a 90-day lookback, uses conditional request validators, and sends results through existing relevance scoring. The probe itself writes no state and does not publish.
- **Bank of Canada press releases are enabled after a successful runner probe.** The official [press-release RSS feed](https://www.bankofcanada.ca/content_type/press-releases/feed/) returned 10 dated official items with zero errors from GitHub Actions on October 21, 2026. The source is bounded to 50 items and a 180-day lookback, validates official `/YYYY/MM/slug/` routes, requires publication dates, and uses existing relevance scoring. The probe itself writes no state and does not publish.
- **Swift press releases remain parked.** GitHub Actions returned HTTP 403 from the official press-release indexes, paginated Insights listing (`/insights?page=1..3&type=evo_news`), news listing and all three sitemap variants. These are read-only diagnostics; public visibility in ordinary browsing does not establish runner access. Do not enable the collector until an official endpoint returns valid, dated items from the runner.
- **Coinbase blog discovery is healthy, but its auxiliary endpoints remain limited.** The latest runner-side dated-index probe found the official Coinbase and Thunes article dated October 19, 2026, among parsed index items, and the production blog collector returned 17 candidates. The RSS and direct article-metadata endpoints returned HTTP 403; the product index did not contain that article; Coinbase Investor Relations still returned an index shell with no dated allowlisted article links, so that separate source remains disabled. Diagnostic canaries point to the current official article rather than an older route. These probe results do not bypass access controls or enable the disabled source.
- **Citi press releases are enabled and the production collector is healthy.** The latest read-only sitemap collector returned three allowlisted candidates with no errors; the scheduled collector can correctly report zero new candidates when the saved sitemap digest is unchanged. This is a no-change result, not a parked source. The separate probe is read-only and does not update production state.
- **IMF web endpoints remain parked.** GitHub Actions returned HTTP 403 from public News, Blogs, GFSR, RSS, sitemap and the official IMF Data news listing; the official Media Center listing returned HTTP 202 with an empty body. No access-control workaround is used. Re-enable only after an official RSS/API endpoint returns usable dated items from the runner; parser fixtures remain covered.
- **Push-triggered feed runs are source-diagnostic dry runs.** They skip Ollama setup, live inference smoke testing, and full per-headline enrichment to shorten feedback cycles. Scheduled and manual runs still perform the real model inference smoke test and full intelligence layer in shadow mode.

## Isolated one-event intelligence delivery test

The manual **XRP Intelligence Feed** workflow supports a narrowly scoped delivery test without enabling normal-feed publishing. Use it only after reviewing a genuinely fresh queued event:

1. Open **Actions → XRP Intelligence Feed → Run workflow** on `main`.
2. Leave **Dry run** enabled and **Preview intelligence** off.
3. Enable **Publish exactly one explicitly selected queued intelligence event**.
4. Copy the exact `event_id` from the current queued-intelligence preview/logs into **Exact queued event ID**. There is deliberately no default ID.
5. Enter `POST-ONE-INTELLIGENCE-EVENT` in the confirmation field.
6. Inspect the run output. The test must stop unless the exact ID matches one queued event, is not already in delivery history, and has at least one valid HTTP(S) evidence URL with a publication timestamp within the 30-day freshness window.

The test path temporarily enables delivery for that one event only, with a one-post limit. It leaves the normal feed in dry-run mode, refuses missing/ambiguous/stale/already-delivered targets, and retains unrelated queued items. A successful workflow run alone is not proof that a Discord post occurred; confirm the log reports exactly one delivered event and verify the Discord message.

Do not enable routine intelligence publishing until the one-event test and subsequent shadow-output review are accepted. The former hardcoded DriveWealth test workflow has been removed; there is now one supported, confirmation-gated path for an actual one-event delivery test.

