from __future__ import annotations

import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError


TARGETS = (
    ("MAS news index", "https://www.mas.gov.sg/news?page=1", "www.mas.gov.sg"),
    ("MAS media release", "https://www.mas.gov.sg/news/media-releases/2026/mas-bnm-and-bot-partner-with-industry-to-advance-cross-border-settlement-in-digital-money", "www.mas.gov.sg"),
    ("MAS robots", "https://www.mas.gov.sg/robots.txt", "www.mas.gov.sg"),
    ("MAS sitemap", "https://www.mas.gov.sg/sitemap.xml", "www.mas.gov.sg"),
    ("Google News RSS MAS releases", "https://news.google.com/rss/search?q=site%3Amas.gov.sg%2Fnews%2Fmedia-releases&hl=en-SG&gl=SG&ceid=SG%3Aen", "news.google.com"),
    ("Google News RSS BOT English news", "https://news.google.com/rss/search?q=site%3Abot.or.th%2Fen%2Fnews-and-media%2Fnews&hl=en&gl=US&ceid=US%3Aen", "news.google.com"),
    ("Google News RSS BOT Thai stablecoin hearing", "https://news.google.com/rss/search?q=site%3Abot.or.th%2Fth%2Flaws-and-rules%2Fpublic-hearing+stablecoin&hl=th&gl=TH&ceid=TH%3Ath", "news.google.com"),
    ("BOT English news", "https://www.bot.or.th/en/news-and-media/news.html", "www.bot.or.th"),
    ("BOT English hearings", "https://www.bot.or.th/en/laws-and-rules/public-hearing.html", "www.bot.or.th"),
    ("BOT generic sitemap", "https://www.bot.or.th/sitemap.xml", "www.bot.or.th"),
    ("BOT English sitemap from robots", "https://www.bot.or.th/en.sitemap.xml", "www.bot.or.th"),
    ("BOT Thai sitemap from robots", "https://www.bot.or.th/th.sitemap.xml", "www.bot.or.th"),
    ("BOT Thai hearings", "https://www.bot.or.th/th/laws-and-rules/public-hearing.html", "www.bot.or.th"),
    ("BOT stablecoin hearing", "https://www.bot.or.th/th/laws-and-rules/public-hearing/public-hearing-20261020-Stablecoin.html", "www.bot.or.th"),
    ("BOT robots", "https://www.bot.or.th/robots.txt", "www.bot.or.th"),
)
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
        if urlsplit(url).path.lower().endswith("robots.txt"):
            sitemaps = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]
            for sitemap in sitemaps[:5]:
                print(f"  robots_sitemap={sitemap}")
        if "html" in content_type or b"<html" in response.content[:500].lower():
            feeds, articles = _links(text, response.url, host)
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
                        title = entry.get("title", "")
                        if any(term in title.casefold() for term in ("bloom", "stablecoin")):
                            try:
                                resolved = client.get(entry.get("link", ""))
                                print(f"  rss_relevant_link_resolution final_url={resolved.url} type={resolved.headers.get('content-type', 'unknown')} bytes={len(resolved.content)}")
                            except DiscoveryHttpError as exc:
                                print(f"  rss_relevant_link_resolution_error={exc.kind} status={exc.status_code or 'unknown'}")
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

if __name__ == "__main__":
    main()
