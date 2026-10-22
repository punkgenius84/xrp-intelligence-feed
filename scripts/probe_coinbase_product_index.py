from __future__ import annotations

from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import _parse_page


URL = "https://www.coinbase.com/en-gb/blog/landing/product"
TARGET_PATH = "/blog/coinbase-and-thunes-connect-stablecoin-infrastructure"
HOSTS = {"www.coinbase.com", "coinbase.com"}


def probe_product_index(http=None, *, target_path: str = TARGET_PATH) -> str:
    """Read-only bounded probe; never changes active source configuration or state."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Coinbase product-index diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        response = client.get(URL, expected_content_types=("text/html",))
    except DiscoveryHttpError as exc:
        suffix = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"status=unavailable reason={exc.kind}{suffix}"
    if response.status_code != 200:
        return f"status=unavailable reason=unexpected_http_status http_status={response.status_code}"

    final = urlsplit(response.url)
    if (
        final.scheme.lower() != "https"
        or final.hostname is None
        or final.hostname.casefold() not in HOSTS
        or final.port not in (None, 443)
        or final.username is not None
        or final.password is not None
    ):
        return "status=rejected reason=non_official_final_url"
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        return f"status=rejected reason=unexpected_content_type content_type={content_type or 'missing'}"

    source = {
        "source_id": "coinbase-blog",
        "name": "Coinbase",
        "source_url": URL,
        "allowed_hosts": sorted(HOSTS),
        "article_path_regex": r"(?:/[a-z]{2}-[a-z]{2})?/blog/(?!landing/?$)[^/?#]+",
    }
    diagnostics = {}
    try:
        rows, complete = _parse_page(source, response.content, diagnostics)
        page_text = response.content.decode("utf-8-sig").casefold()
    except (UnicodeDecodeError, ValueError) as exc:
        return f"status=invalid_page reason={type(exc).__name__}"

    def target_route_matches(native_id: object) -> bool:
        if not isinstance(native_id, str):
            return False
        normalized = re.sub(r"^/[a-z]{2}-[a-z]{2}(?=/blog/)", "", native_id.casefold())
        return normalized == target_path.casefold()

    target = next((row for row in rows if target_route_matches(row.get("native_id"))), None)
    if target is not None:
        published = target.get("date")
        date_label = published.isoformat() if hasattr(published, "isoformat") else "missing"
        return f"status=valid_index target_route=present target_date={date_label} parsed_items={len(rows)}"
    if target_path.casefold() in page_text:
        parse_status = "complete" if complete else "partial"
        return (
            f"status=valid_index target_route=present target_date=unresolved "
            f"parsed_items={len(rows)} page_parse={parse_status}"
        )
    return f"status=valid_index target_route=absent parsed_items={len(rows)}"


def main() -> None:
    print(f"Coinbase official product-index diagnostic ({URL}): {probe_product_index()}")


if __name__ == "__main__":
    main()
