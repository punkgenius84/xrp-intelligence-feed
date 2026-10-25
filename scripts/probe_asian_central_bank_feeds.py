from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.dispatch import collect_source, load_discovery_sources
from discovery.normalization import normalize_candidate
from intelligence.correlation import (
    build_correlation_card,
    correlate,
    filter_secondary_opinion_commentary,
    suppress_correlated_secondary_duplicates,
)
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


TARGETS = (
    ("MAS news index", "https://www.mas.gov.sg/news?page=1", "www.mas.gov.sg"),
    ("MAS media release", "https://www.mas.gov.sg/news/media-releases/2026/mas-bnm-and-bot-partner-with-industry-to-advance-cross-border-settlement-in-digital-money", "www.mas.gov.sg"),
    ("MAS BLOOM initiative", "https://www.mas.gov.sg/schemes-and-initiatives/BLOOM", "www.mas.gov.sg"),
    ("MAS robots", "https://www.mas.gov.sg/robots.txt", "www.mas.gov.sg"),
    ("MAS sitemap", "https://www.mas.gov.sg/sitemap.xml", "www.mas.gov.sg"),
    ("SG Press Centre root", "https://www.sgpc.gov.sg/", "www.sgpc.gov.sg"),
    ("SG Press Centre releases page", "https://www.sgpc.gov.sg/home?Page=1", "www.sgpc.gov.sg"),
    ("Google News RSS MAS releases", "https://news.google.com/rss/search?q=site%3Amas.gov.sg%2Fnews%2Fmedia-releases&hl=en-SG&gl=SG&ceid=SG%3Aen", "news.google.com"),
    ("Google News RSS BOT English news", "https://news.google.com/rss/search?q=site%3Abot.or.th%2Fen%2Fnews-and-media%2Fnews&hl=en&gl=US&ceid=US%3Aen", "news.google.com"),
    ("Google News RSS BOT Thai stablecoin hearing", "https://news.google.com/rss/search?q=site%3Abot.or.th%2Fth%2Flaws-and-rules%2Fpublic-hearing+stablecoin&hl=th&gl=TH&ceid=TH%3Ath", "news.google.com"),
    ("BOT English news", "https://www.bot.or.th/en/news-and-media/news.html", "www.bot.or.th"),
    ("BOT historical RSS directory", "https://www.bot.or.th/English/RSS/Pages/index.aspx", "www.bot.or.th"),
    ("BOT English hearings", "https://www.bot.or.th/en/laws-and-rules/public-hearing.html", "www.bot.or.th"),
    ("BOT generic sitemap", "https://www.bot.or.th/sitemap.xml", "www.bot.or.th"),
    ("BOT English sitemap from robots", "https://www.bot.or.th/en.sitemap.xml", "www.bot.or.th"),
    ("BOT Thai sitemap from robots", "https://www.bot.or.th/th.sitemap.xml", "www.bot.or.th"),
    ("BOT Thai hearings", "https://www.bot.or.th/th/laws-and-rules/public-hearing.html", "www.bot.or.th"),
    ("BOT stablecoin hearing", "https://www.bot.or.th/th/laws-and-rules/public-hearing/public-hearing-20261020-Stablecoin.html", "www.bot.or.th"),
    ("BOT robots", "https://www.bot.or.th/robots.txt", "www.bot.or.th"),
)


# Inspect a bounded number of same-host JavaScript bundles only on relevant official
# index pages. The probe reports candidate API routes but never calls guessed routes.
SCRIPT_ASSET_PROBE_LABELS = frozenset({
    "MAS news index",
    "SG Press Centre root",
    "SG Press Centre releases page",
    "BOT English news",
    "BOT English hearings",
    "BOT Thai hearings",
})


def _probe_configured_google_news_collectors() -> None:
    """Exercise the real collector in memory without enabling or saving any source."""
    print("\nREAD-ONLY collector validation; enabled flags are overridden in memory only")
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Google News collector validation)",
        timeout=(3.0, 6.0),
        max_bytes=1_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        sources = [
            source for source in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
            if source.get("discovery_method") == "google_news_official_rss"
        ]
    except Exception as exc:
        print(f"collector_registry_error={type(exc).__name__}: {exc}")
        return

    for configured in sources:
        source = {**configured, "enabled": True}
        try:
            result = collect_source(source, http=client)
        except Exception as exc:
            print(f"collector source_id={source['source_id']} status=error error={type(exc).__name__}: {exc}")
            continue
        print(
            f"collector source_id={source['source_id']} configured_enabled={configured['enabled']} "
            f"probe_status={result.status} candidates={len(result.candidates)} "
            f"errors={len(result.errors)}"
        )
        for index, candidate in enumerate(result.candidates):
            detect_entities(candidate)
            classify_source_quality(candidate)
            score, reasons = score_relevance(candidate)
            if score < 30 and index >= 3:
                continue
            metadata = candidate.source_native_metadata
            print(
                f"  candidate date={candidate.published_at.isoformat() if candidate.published_at else 'unknown'} "
                f"score={score} entities={','.join(candidate.detected_entities) or 'none'} "
                f"categories={','.join(candidate.relevance_categories) or 'none'} "
                f"url_status={metadata.get('article_url_status', 'unknown')} "
                f"title={candidate.title[:180]!r}"
            )
            print(f"  candidate_link={candidate.url}")
            if reasons:
                print(f"  score_reasons={' | '.join(reasons)[:400]}")
        for error in result.errors[:3]:
            print(f"  collector_error={error[:300]}")


FEED_RE = re.compile(r"""<link\b[^>]*>""", re.IGNORECASE)
ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*(['"])(.*?)\2""", re.IGNORECASE | re.DOTALL)
FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}


def _host_ok(url: str, expected_host: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and p.hostname and p.hostname.casefold() == expected_host


def _links(html: str, page_url: str, expected_host: str) -> tuple[list[str], list[str]]:
    feeds: list[str] = []
    article_links: list[str] = []
    for tag in FEED_RE.findall(html):
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        typ = attrs.get("type", "").split(";", 1)[0].strip().lower()
        href = attrs.get("href", "")
        if not href:
            continue
        absolute = urljoin(page_url, href)
        if not _host_ok(absolute, expected_host):
            continue
        path = urlsplit(absolute).path.lower()
        if "alternate" in rel and (typ in FEED_TYPES or path.endswith((".rss", ".atom", ".xml")) or "feed" in path):
            feeds.append(absolute)
    for match in re.finditer(r"""href\s*=\s*(['"])(.*?)\1""", html, re.IGNORECASE | re.DOTALL):
        href = unescape(match.group(2)).strip()
        if not href:
            continue
        absolute = urljoin(page_url, href)
        if not _host_ok(absolute, expected_host):
            continue
        path = urlsplit(absolute).path
        if re.search(r"/(?:news|media-releases|press-release|public-hearing)/[^?#]*(?:20\d{2}|\d{8})", path, re.IGNORECASE):
            article_links.append(absolute)
    return sorted(set(feeds)), sorted(set(article_links))


def _same_host_script_urls(html: str, page_url: str, expected_host: str, limit: int = 6) -> list[str]:
    """Return a small, same-host list of script assets for read-only diagnostics."""
    if limit <= 0:
        return []
    preferred: list[str] = []
    fallback: list[str] = []
    vendor_markers = ("/vendors/", "jquery", "bootstrap", "swiper", "moment", "fontawesome", "iframe-lightbox")
    for tag in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE | re.DOTALL):
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(tag)}
        src = attrs.get("src", "").strip()
        if not src:
            continue
        absolute = urljoin(page_url, src)
        if not _host_ok(absolute, expected_host):
            continue
        path = urlsplit(absolute).path.lower()
        if not path.endswith((".js", ".mjs")):
            continue
        target = fallback if any(marker in path for marker in vendor_markers) else preferred
        if absolute not in preferred and absolute not in fallback:
            target.append(absolute)
    return (preferred + fallback)[:limit]


def _candidate_api_routes(script_text: str, limit: int = 20) -> list[str]:
    """Extract likely same-site content/API route strings; never request them here."""
    normalized = script_text.replace(r"\/", "/").replace(r"\u002F", "/")
    pattern = re.compile(
        r"""["'\x60]((?:/api(?:/v[0-9]+)?|/graphql|/search|/news(?:/|\?)|"""
        r"""/media-releases/|/content/|/umbraco/)[^"'\x60\s]{0,160})["'\x60]""",
        re.IGNORECASE,
    )
    routes: list[str] = []
    for match in pattern.finditer(normalized):
        route = match.group(1).rstrip(".,;)")
        if route.startswith("//") or route in routes:
            continue
        routes.append(route)
        if len(routes) >= max(0, limit):
            break
    return routes


def _route_context(script_text: str, route: str, radius: int = 650) -> str:
    """Return a short, whitespace-normalized context around a route string."""
    normalized = script_text.replace(r"\/", "/").replace(r"\u002F", "/")
    index = normalized.find(route)
    if index < 0:
        return ""
    start = max(0, index - max(0, radius))
    end = min(len(normalized), index + len(route) + max(0, radius))
    return " ".join(normalized[start:end].split())[:1500]


def _probe_sgpc_listing_api(client: BoundedHttpClient) -> None:
    """Probe the first-party SG Press Centre listing API with a bounded date window."""
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=30)
    query = urlencode({
        "StartDate": start_date.strftime("%d-%b-%Y"),
        "EndDate": end_date.strftime("%d-%b-%Y"),
        "page": "1",
    })
    url = "https://www.sgpc.gov.sg/api/ApiMainListing/SubmitData?" + query
    print(f"  sgpc_listing_api_request={url}")
    try:
        response = client.get(url, expected_content_types=("application/json", "text/json"))
    except DiscoveryHttpError as exc:
        status = f" http_status={exc.status_code}" if exc.status_code else ""
        print(f"  sgpc_listing_api=unavailable reason={exc.kind}{status}")
        return
    final_host = urlsplit(response.url).hostname
    if final_host != "www.sgpc.gov.sg":
        print(f"  sgpc_listing_api=rejected unexpected_final_host={final_host or 'missing'}")
        return
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        print(f"  sgpc_listing_api=invalid_json error={type(exc).__name__}")
        return
    if not isinstance(payload, dict):
        print("  sgpc_listing_api=invalid_payload top_level_type=" + type(payload).__name__)
        return
    result = payload.get("result")
    if not isinstance(result, dict):
        print(f"  sgpc_listing_api=unexpected_shape top_level_keys={sorted(payload)[:20]}")
        return
    items = result.get("items")
    if not isinstance(items, list):
        print(f"  sgpc_listing_api=unexpected_shape result_keys={sorted(result)[:30]}")
        return
    print(
        f"  sgpc_listing_api=valid http={response.status_code} items={len(items)} "
        f"result_keys={sorted(result)[:30]}"
    )
    for item in items[:5]:
        if not isinstance(item, dict):
            print(f"    sgpc_item_type={type(item).__name__}")
            continue
        selected = {
            key: value for key, value in item.items()
            if any(token in key.casefold() for token in ("title", "date", "url", "link", "agency", "subject", "type", "id"))
        }
        safe = {key: str(value)[:240] for key, value in list(selected.items())[:12]}
        print(f"    sgpc_item_fields={json.dumps(safe, ensure_ascii=False, sort_keys=True)}")


def _probe_sgpc_search_terms(client: BoundedHttpClient) -> None:
    """Check whether the first-party listing API supports targeted keyword queries."""
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=30)
    for index, term in enumerate(("BLOOM", "stablecoin", "digital money", "Ripple", "XRP")):
        if index:
            time.sleep(1.0)
        params = {
            "StartDate": start_date.strftime("%d-%b-%Y"),
            "EndDate": end_date.strftime("%d-%b-%Y"),
            "KeySearch": term,
            "page": "1",
        }
        url = "https://www.sgpc.gov.sg/api/ApiMainListing/SubmitData?" + urlencode(params)
        try:
            response = client.get(url, expected_content_types=("application/json", "text/json"))
        except DiscoveryHttpError as exc:
            status = f" http_status={exc.status_code}" if exc.status_code else ""
            print(f"  sgpc_search term={term!r} status=unavailable reason={exc.kind}{status}")
            continue
        if urlsplit(response.url).hostname != "www.sgpc.gov.sg":
            print(f"  sgpc_search term={term!r} status=rejected final_host={urlsplit(response.url).hostname}")
            continue
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            print(f"  sgpc_search term={term!r} status=invalid_json")
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            print(f"  sgpc_search term={term!r} status=unexpected_shape")
            continue
        print(f"  sgpc_search term={term!r} status=valid items={len(items)}")
        for item in items[:3]:
            if isinstance(item, dict):
                print(
                    f"    sgpc_search_item title={str(item.get('Title', ''))[:180]!r} "
                    f"date={str(item.get('DateTime', ''))[:40]!r} "
                    f"url={str(item.get('Url', ''))[:200]!r}"
                )


def _probe_same_host_script_assets(
    client: BoundedHttpClient, label: str, html: str, page_url: str, expected_host: str
) -> None:
    """Inspect a few same-host JS bundles for API routes without calling those routes."""
    scripts = _same_host_script_urls(html, page_url, expected_host)
    print(f"  same_host_script_assets={len(scripts)} host={expected_host}")
    for script_url in scripts:
        try:
            response = client.get(script_url)
        except (DiscoveryHttpError, TypeError) as exc:
            print(f"  script_asset_unavailable url={script_url} reason={type(exc).__name__}")
            continue
        final_host = urlsplit(response.url).hostname
        if final_host != expected_host:
            print(f"  script_asset_skipped_cross_host requested={script_url} final_host={final_host or 'missing'}")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if "javascript" not in content_type and not urlsplit(response.url).path.lower().endswith((".js", ".mjs")):
            print(f"  script_asset_skipped_unexpected_type url={script_url} type={content_type or 'missing'}")
            continue
        body = response.content.decode("utf-8", errors="replace")
        routes = _candidate_api_routes(body)
        print(f"  script_asset url={script_url} http={response.status_code} bytes={len(response.content)} candidate_routes={len(routes)}")
        for route in routes:
            print(f"    candidate_same_site_route={route}")
            context = _route_context(body, route)
            if context:
                print(f"    route_context={context}")


def _page_metadata(html: str, page_url: str, expected_host: str) -> dict[str, list[str]]:
    """Report page-owned metadata from HTML without treating it as verified article data."""
    found: dict[str, list[str]] = {}

    def add(key: str, value: str) -> None:
        value = " ".join(unescape(value).split()).strip()
        if not value:
            return
        values = found.setdefault(key, [])
        if value not in values and len(values) < 4:
            values.append(value[:300])

    for tag in re.findall(r"<meta\b[^>]*>", html, re.IGNORECASE | re.DOTALL):
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").strip().lower()
        value = attrs.get("content", "")
        if key in {
            "og:title", "og:url", "article:published_time", "article:modified_time",
            "date", "datepublished", "dc.date", "dc.date.issued", "headline",
            "twitter:title",
        }:
            add(key, value)

    for tag in re.findall(r"<link\b[^>]*>", html, re.IGNORECASE | re.DOTALL):
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(tag)}
        if "canonical" in attrs.get("rel", "").lower().split():
            canonical = urljoin(page_url, attrs.get("href", ""))
            if _host_ok(canonical, expected_host):
                add("canonical", canonical)

    for match in re.finditer(r"<title\b[^>]*>(.*?)</title\s*>", html, re.IGNORECASE | re.DOTALL):
        add("title", re.sub(r"<[^>]+>", " ", match.group(1)))

    for match in re.finditer(r"<time\b([^>]*)>", html, re.IGNORECASE | re.DOTALL):
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(match.group(1))}
        if attrs.get("datetime"):
            add("time.datetime", attrs["datetime"])

    for match in re.finditer(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL):
        opening_tag = match.group(0).split(">", 1)[0] + ">"
        attrs = {m.group(1).lower(): unescape(m.group(3)) for m in ATTR_RE.finditer(opening_tag)}
        if attrs.get("type", "").split(";", 1)[0].strip().lower() != "application/ld+json":
            continue
        payload = match.group(1)
        for key in ("datePublished", "dateCreated", "headline", "name", "url", "mainEntityOfPage"):
            pattern = r'"' + re.escape(key) + r'"\s*:\s*"([^"]{1,500})"'
            for value in re.findall(pattern, payload):
                add("jsonld." + key, value)

    return found


def _probe_sgpc_collector() -> None:
    """Exercise the configured SGPC collector in memory; never save state or publish."""
    try:
        sources = load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        source = next(item for item in sources if item.get("source_id") == "sgpc-media-releases-api")
    except Exception as exc:
        print(f"sgpc_collector_registry_error={type(exc).__name__}: {exc}")
        return
    try:
        result = collect_source(source, {})
    except Exception as exc:
        print(f"sgpc_collector status=error error={type(exc).__name__}: {exc}")
        return
    bloom = [item for item in result.candidates if "BLOOM" in item.title.upper()]
    print(
        f"sgpc_collector configured_enabled={source['enabled']} status={result.status} "
        f"candidates={len(result.candidates)} errors={len(result.errors)} "
        f"requests_made={result.pagination.get('requests_made', 0)} "
        f"bloom_candidates={len(bloom)}"
    )
    for error in result.errors[:5]:
        print(f"  sgpc_collector_error={error}")
    for item in sorted(
        result.candidates,
        key=lambda candidate: (candidate.published_at or datetime.min.replace(tzinfo=timezone.utc), candidate.title),
        reverse=True,
    )[:8]:
        detect_entities(item)
        classify_source_quality(item)
        score, reasons = score_relevance(item)
        print(
            f"  sgpc_candidate date={item.published_at.isoformat() if item.published_at else 'unknown'} "
            f"score={score} title={item.title[:200]!r} url={item.url}"
        )
    if bloom:
        print(f"  sgpc_bloom_official_url={bloom[0].url}")


def _probe_secondary_source_duplicate_gate() -> None:
    """Compare a few real publisher collectors in memory; never save state or publish."""
    selected_ids = {
        "mas-google-news-discovery",
        "coinbase-google-news-discovery",
        "mastercard-press-releases",
        "xrpl-community-blog",
        "coindesk-google-news-discovery",
        "theblock-google-news-discovery",
    }
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only cross-source duplicate probe)",
        timeout=(3.0, 6.0),
        max_bytes=1_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        configured_sources = load_discovery_sources(ROOT / "config" / "discovery_sources.json")
    except Exception as exc:
        print(f"duplicate_gate_probe=error registry={type(exc).__name__}: {exc}")
        return

    items: list[NewsItem] = []
    now = datetime.now(timezone.utc)
    for configured in configured_sources:
        if configured.get("source_id") not in selected_ids:
            continue
        source = {**configured, "enabled": True}
        try:
            result = collect_source(source, http=client)
        except Exception as exc:
            print(f"duplicate_gate_source={source['source_id']} status=error error={type(exc).__name__}: {exc}")
            continue
        print(
            f"duplicate_gate_source={source['source_id']} configured_enabled={configured['enabled']} "
            f"status={result.status} candidates={len(result.candidates)} errors={len(result.errors)}"
        )
        for candidate in result.candidates:
            candidate = normalize_candidate(candidate)
            item = NewsItem(
                title=candidate.title,
                url=candidate.url,
                source=candidate.source,
                published_at=candidate.published_at,
                summary=candidate.summary,
                source_id=candidate.source_id,
                authority_tier=candidate.authority_tier,
                category=candidate.category,
                candidate_id=candidate.candidate_id,
            )
            detect_entities(item)
            classify_source_quality(item)
            score_relevance(item)
            published = item.published_at
            if published is None:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age = now - published.astimezone(timezone.utc)
            if age < -timedelta(days=1) or age > timedelta(days=14):
                continue
            if item.relevance_score >= 30:
                items.append(item)
        for error in result.errors[:2]:
            print(f"  duplicate_gate_error={error[:260]}")

    for item in sorted(items, key=lambda value: (value.source_id, value.title.casefold())):
        print(
            f"  duplicate_gate_candidate source={item.source_id} quality={item.source_quality} "
            f"score={item.relevance_score} date={item.published_at.isoformat() if item.published_at else 'unknown'} "
            f"entities={','.join(item.detected_entities) or 'none'} title={item.title[:160]!r}"
        )
    correlate(items)
    kept, suppressed = suppress_correlated_secondary_duplicates(items, publish_score=30)
    print(
        f"duplicate_gate_summary candidates_score30_recent14d={len(items)} "
        f"kept_for_delivery={len(kept)} suppressed_secondary_duplicates={len(suppressed)}"
    )
    for item in suppressed:
        print(
            f"  duplicate_gate_suppressed score={item.relevance_score} source={item.source_id} "
            f"title={item.title[:180]!r} url={item.url}"
        )
    if not suppressed:
        print("  duplicate_gate_suppressed=none")

    # Model the realistic later-run case: the primary has already been seen, so
    # only the secondary candidates are new. The cards are built in memory from
    # this probe's current official candidates; nothing is read from or written to
    # production correlation state.
    primary_history = [
        build_correlation_card(item)
        for item in items
        if item.source_quality == "primary" and item.relevance_score >= 30
    ]
    secondary_only = [
        item for item in items
        if item.source_quality == "high_signal" and item.relevance_score >= 30
    ]
    cross_run_kept, cross_run_suppressed = suppress_correlated_secondary_duplicates(
        secondary_only, publish_score=30, history=primary_history
    )
    print(
        f"cross_run_duplicate_gate_summary secondary_candidates={len(secondary_only)} "
        f"qualifying_primary_history_cards={len(primary_history)} "
        f"kept_for_delivery={len(cross_run_kept)} suppressed={len(cross_run_suppressed)}"
    )
    for item in cross_run_suppressed:
        print(
            f"  cross_run_duplicate_gate_suppressed score={item.relevance_score} "
            f"source={item.source_id} title={item.title[:180]!r} url={item.url}"
        )

    commentary_kept, commentary_held = filter_secondary_opinion_commentary(items)
    print(
        f"secondary_commentary_gate_summary qualifying_candidates={len(items)} "
        f"eligible={len(commentary_kept)} held={len(commentary_held)}"
    )
    for item in commentary_held[:10]:
        print(
            f"  secondary_commentary_gate_held score={item.relevance_score} "
            f"source={item.source_id} title={item.title[:180]!r} url={item.url}"
        )


def main() -> None:
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only official feed discovery probe)",
        timeout=(3.0, 6.0),
        max_bytes=1_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    print("READ-ONLY official RSS/Atom and index probe; no state writes or Discord publishing")
    for label, url, host in TARGETS:
        try:
            response = client.get(url)
        except DiscoveryHttpError as exc:
            status = f" http_status={exc.status_code}" if exc.status_code else ""
            print(f"{label}: unavailable reason={exc.kind}{status}")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        print(f"{label}: http={response.status_code} type={content_type or 'missing'} bytes={len(response.content)} final_host={urlsplit(response.url).hostname}")
        text = response.content.decode("utf-8", errors="replace")
        if label == "BOT stablecoin hearing":
            canonical = re.search(r'<link\b(?=[^>]*\brel\s*=\s*["\']canonical["\'])(?=[^>]*\bhref\s*=\s*["\']([^"\']+)["\'])[^>]*>', text, re.IGNORECASE | re.DOTALL)
            title_match = re.search(r'<h1\b[^>]*>(.*?)</h1>', text, re.IGNORECASE | re.DOTALL)
            title_text = re.sub(r'<[^>]+>', ' ', title_match.group(1)).strip() if title_match else "missing"
            print(f"  official_canonical={unescape(canonical.group(1)) if canonical else 'missing'}")
            print(f"  official_h1={title_text[:220]}")
            for meta in re.finditer(r'<meta\b[^>]*>', text, re.IGNORECASE | re.DOTALL):
                tag = meta.group(0)
                if re.search(r'published|publish|date|created|modified|canonical|og:url', tag, re.IGNORECASE):
                    print(f"  hearing_metadata={tag[:420]}")
            for raw in re.findall(r'<script\b(?=[^>]*\btype\s*=\s*["\']application/ld\+json["\'])[^>]*>(.*?)</script\s*>', text, re.IGNORECASE | re.DOTALL):
                if re.search(r'datePublished|dateCreated|NewsArticle|PressRelease', raw, re.IGNORECASE):
                    jsonld_preview = re.sub(r'\s+', ' ', raw)
                    print(f"  hearing_jsonld={jsonld_preview[:900]}")
            for pattern in (r'2026[-/]10[-/]\d{1,2}', r'\d{1,2}\s+October\s+2026', r'October\s+\d{1,2},?\s+2026'):
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    print(f"  hearing_visible_date_match={match.group(0)!r}")
                    break
        if urlsplit(url).path.lower().endswith("robots.txt"):
            sitemaps = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]
            for sitemap in sitemaps[:5]:
                print(f"  robots_sitemap={sitemap}")
        if "html" in content_type or b"<html" in response.content[:500].lower():
            feeds, articles = _links(text, response.url, host)
            if label in SCRIPT_ASSET_PROBE_LABELS:
                final_host = urlsplit(response.url).hostname or ""
                _probe_same_host_script_assets(client, label, text, response.url, final_host)
            if label == "SG Press Centre releases page":
                _probe_sgpc_listing_api(client)
                _probe_sgpc_search_terms(client)
            metadata = _page_metadata(text, response.url, host)
            for key, values in sorted(metadata.items()):
                for value in values[:3]:
                    print(f"  page_metadata {key}={value!r}")
            if host == "www.mas.gov.sg":
                for keyword in ("BLOOM", "stablecoin", "Ripple", "XRP"):
                    matches = list(re.finditer(keyword, text, re.IGNORECASE))
                    print(f"  keyword={keyword} occurrences={len(matches)}")
                    for match in matches[:3]:
                        snippet = " ".join(text[max(0, match.start()-180):match.end()+220].split())
                        print(f"  keyword_context={snippet[:420]}")
            for feed in feeds[:10]:
                print(f"  feed_link={feed}")
            for article in articles[:8]:
                print(f"  dated_official_link={article}")
            if not feeds and not articles:
                print("  no feed links or date-bearing official article links in returned HTML")
        elif content_type in FEED_TYPES or "xml" in content_type:
            if "news.google.com" in urlsplit(url).hostname:
                try:
                    import feedparser
                    parsed = feedparser.parse(response.content)
                    print(f"  rss_entry_count={len(parsed.entries)} bozo={bool(parsed.bozo)}")
                    for entry in parsed.entries[:12]:
                        source = entry.get("source", {})
                        print(f"  rss_item date={entry.get('published', entry.get('updated', 'unknown'))!r} title={entry.get('title', '')[:180]!r} source={source.get('title', '')!r} source_url={source.get('href', '')!r} feed_link={entry.get('link', '')!r}")
                        # Keep the Google News article URL as the usable discovery link.
                        # The feed carries publisher attribution and publication time; following
                        # the Google landing page is unnecessary for this read-only probe.
                        if entry.get("link"):
                            print(f"  google_news_link={entry.get('link', '')}")
                except Exception as exc:
                    print(f"  rss_parse_error={type(exc).__name__}: {exc}")
                continue
            blocks = re.findall(r"<url>(.*?)</url>", text, re.IGNORECASE | re.DOTALL)
            locations = []
            for block in blocks:
                loc_match = re.search(r"<loc>(.*?)</loc>", block, re.IGNORECASE | re.DOTALL)
                mod_match = re.search(r"<lastmod>(.*?)</lastmod>", block, re.IGNORECASE | re.DOTALL)
                if loc_match:
                    locations.append((unescape(loc_match.group(1).strip()), mod_match.group(1).strip() if mod_match else ""))
            print(f"  sitemap_url_count={len(locations)}")
            targeted = [(loc, modified) for loc, modified in locations if any(term in loc.lower() for term in ("stablecoin", "public-hearing", "hearing", "digital-money"))]
            print(f"  targeted_hearing_or_stablecoin_urls={len(targeted)}")
            for loc, modified in targeted[:25]:
                print(f"  targeted_sitemap_url lastmod={modified or 'unknown'} url={loc}")
            dated_2026 = [(loc, modified) for loc, modified in locations if "2026" in loc]
            print(f"  url_encoded_2026_count={len(dated_2026)}")
            for loc, modified in dated_2026[-20:]:
                print(f"  latest_2026_sitemap_url lastmod={modified or 'unknown'} url={loc}")
            if not locations:
                print(f"  xml_preview={text[:220].replace(chr(10), chr(32))}")

    _probe_sgpc_collector()
    _probe_configured_google_news_collectors()
    _probe_secondary_source_duplicate_gate()


if __name__ == "__main__":
    main()
