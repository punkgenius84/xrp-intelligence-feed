from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError, HttpAttemptBudget
from discovery.models import DiscoveryCandidate

OCC_SOURCE_ID = "occ-issuances"
OCC_METHOD = "occ_yearly_issuances"
OCC_ROOT = "https://www.occ.gov/news-events/newsroom/news-issuances-by-year"
OCC_HOST = "www.occ.gov"
_NEWS_ROUTE = re.compile(r"^/news-issuances/news-releases/(20\d{2})/(nr-occ-20\d{2}-\d+)\.html$")
_BULLETIN_ROUTE = re.compile(r"^/news-issuances/bulletins/(20\d{2})/(bulletin-20\d{2}-\d+)\.html$")
_DATE_FORMAT = "%m/%d/%Y"
_MAX_ATTEMPTS = 9

def validate_occ_source(source: object) -> dict[str, Any]:
    required = {"source_id","name","authority_tier","category","discovery_method","source_url",
                "enabled","lookback_days","max_items","max_years","document_types"}
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"OCC source must have exactly {sorted(required)}")
    if source["source_id"] != OCC_SOURCE_ID:
        raise ValueError(f"source_id must be {OCC_SOURCE_ID!r}")
    if source["name"] != "Office of the Comptroller of the Currency":
        raise ValueError("OCC name is fixed")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("OCC authority_tier must be 1")
    if source["category"] != "regulatory":
        raise ValueError("OCC category must be regulatory")
    if source["discovery_method"] != OCC_METHOD:
        raise ValueError(f"discovery_method must be {OCC_METHOD!r}")
    if source["source_url"] != OCC_ROOT:
        raise ValueError("OCC source_url must be the official yearly issuance archive")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 180:
        raise ValueError("lookback_days must be between 1 and 180")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be between 1 and 100")
    if type(source["max_years"]) is not int or not 1 <= source["max_years"] <= 2:
        raise ValueError("max_years must be 1 or 2")
    if source["document_types"] != ["news_release","bulletin"]:
        raise ValueError("document_types must be exactly ['news_release', 'bulletin']")
    return source

def _archive_url(year: int, document_type: str) -> str:
    suffix = "news-releases" if document_type == "news_release" else "bulletins"
    return f"{OCC_ROOT}/{suffix}/{year}-{suffix}.html"

def _row_identity(url: str, document_type: str) -> tuple[str,str] | None:
    try:
        parts = urlsplit(urljoin(OCC_ROOT + "/", url))
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or parts.hostname != OCC_HOST or parts.port not in (None,443):
        return None
    pattern = _NEWS_ROUTE if document_type == "news_release" else _BULLETIN_ROUTE
    match = pattern.fullmatch(parts.path)
    if not match:
        return None
    return match.group(2), f"https://{OCC_HOST}{parts.path}"

class _OCCArchiveParser(HTMLParser):
    def __init__(self, document_type: str) -> None:
        super().__init__(convert_charrefs=True)
        self.document_type = document_type
        self.rows: list[dict[str,str]] = []
        self._row: dict[str,str] | None = None
        self._link: dict[str,str] | None = None
        self._cell_index = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str,str|None]]) -> None:
        if tag == "tr":
            self._row = {"date":"","native_id":"","title":"","url":""}
            self._cell_index = -1
        elif tag == "td" and self._row is not None:
            self._cell_index += 1
        elif tag == "a" and self._row is not None:
            href = next((v or "" for k,v in attrs if k.casefold() == "href"), "")
            identity = _row_identity(href, self.document_type)
            if identity:
                self._link = {"native_id":identity[0],"url":identity[1],"title":""}

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._row is None:
            return
        if self._link is not None:
            self._link["title"] += (" " if self._link["title"] else "") + text
        elif self._cell_index == 0:
            self._row["date"] += (" " if self._row["date"] else "") + text
        elif self._cell_index == 1:
            self._row["native_id"] += (" " if self._row["native_id"] else "") + text
        elif self._cell_index == 2:
            self._row["title"] += (" " if self._row["title"] else "") + text

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            if self._row is not None:
                self._row["native_id"] = self._link["native_id"]
                self._row["url"] = self._link["url"]
                self._row["title"] = " ".join(self._link["title"].split())
            self._link = None
        elif tag == "tr" and self._row is not None:
            row = self._row
            self._row = None
            if row["date"] and row["native_id"] and row["title"] and row["url"]:
                self.rows.append(row)

def _parse_archive(content: bytes, document_type: str) -> list[dict[str,str]]:
    parser = _OCCArchiveParser(document_type)
    parser.feed(content.decode("utf-8-sig", errors="replace"))
    parser.close()
    seen: set[str] = set()
    rows: list[dict[str,str]] = []
    for row in parser.rows:
        if row["native_id"] in seen:
            continue
        try:
            datetime.strptime(row["date"], _DATE_FORMAT)
        except ValueError as exc:
            raise ValueError(f"invalid OCC publication date {row['date']!r}") from exc
        seen.add(row["native_id"])
        rows.append(row)
    if not rows:
        raise ValueError(f"OCC {document_type} archive contains no recognizable rows")
    return rows

class OCCYearlyIssuancesDiscovery:
    source_id = OCC_SOURCE_ID
    discovery_method = OCC_METHOD

    def __init__(self, source: dict[str,Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[],datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.source = validate_occ_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _years(self, today: date) -> list[int]:
        start = today - timedelta(days=self.source["lookback_days"])
        years = [today.year]
        if start.year != today.year:
            years.append(start.year)
        return years[:self.source["max_years"]]

    def _candidate(self, row: dict[str,str], document_type: str, archive_url: str,
                   fetched_at: datetime) -> DiscoveryCandidate:
        published = datetime.strptime(row["date"], _DATE_FORMAT).replace(tzinfo=timezone.utc)
        return DiscoveryCandidate(
            title=row["title"], url=row["url"], source=self.source["name"],
            published_at=published,
            summary=f"OCC {document_type.replace('_',' ').title()}",
            source_type="discovery", source_id=self.source_id, authority_tier=1,
            category=self.source["category"], source_native_id=row["native_id"],
            candidate_id=f"{self.source_id}:{row['native_id']}",
            discovery_method=self.discovery_method, source_url=OCC_ROOT,
            primary_url=row["url"], discovery_lead_url=archive_url,
            provenance=[archive_url,row["url"]], document_type=document_type,
            collected_at=fetched_at, first_seen_at=fetched_at, last_seen_at=fetched_at,
            source_native_metadata={"publication_date":row["date"],"document_type":document_type,
                                    "archive_url":archive_url},
        )

    def collect(self, state: dict[str,Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id,self.discovery_method,"not_configured",fetched_at=fetched_at)

        today = fetched_at.astimezone(timezone.utc).date()
        cutoff = today - timedelta(days=self.source["lookback_days"])
        years = self._years(today)
        candidates: list[DiscoveryCandidate] = []
        errors: list[str] = []
        updates: dict[str,dict[str,str]] = {}
        budget = HttpAttemptBudget(_MAX_ATTEMPTS)
        sources = state.get("sources",{}) if isinstance(state,dict) else {}
        source_state = sources.get(self.source_id,{}) if isinstance(sources,dict) else {}
        saved = source_state.get("requests",{}) if isinstance(source_state,dict) else {}

        for year in years:
            for document_type in self.source["document_types"]:
                request_id = f"{document_type}:{year}"
                url = _archive_url(year,document_type)
                validators = saved.get(request_id,{}) if isinstance(saved,dict) else {}
                try:
                    response = self.http.get(
                        url, etag=validators.get("etag",""), last_modified=validators.get("last_modified",""),
                        expected_content_types=("text/html",), attempt_budget=budget,
                    )
                    if response.status_code == 304:
                        continue
                    rows = _parse_archive(response.content,document_type)
                    updates[request_id] = {
                        key: response.headers[key]
                        for key in ("etag","last-modified") if response.headers.get(key)
                    }
                    for row in rows:
                        published = datetime.strptime(row["date"],_DATE_FORMAT).date()
                        if cutoff <= published <= today and len(candidates) < self.source["max_items"]:
                            candidates.append(self._candidate(row,document_type,url,fetched_at))
                except DiscoveryHttpError as exc:
                    errors.append(f"{request_id}: {exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else ""))
                    break
                except (UnicodeError,ValueError,TypeError) as exc:
                    errors.append(f"{request_id}: {exc}")
                    break
            if errors:
                break

        status = "failed" if errors and not candidates else ("partial" if errors else ("success" if candidates else "empty"))
        return DiscoveryResult(self.source_id,self.discovery_method,status,candidates,fetched_at,errors,
                               state_updates=updates,
                               pagination={"occ_years":years,"document_types":self.source["document_types"]})
