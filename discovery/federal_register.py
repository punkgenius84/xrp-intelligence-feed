from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta, timezone
import json
import re
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate


FEDERAL_REGISTER_SOURCE_ID = "federal-register-api"
FEDERAL_REGISTER_API_URL = "https://www.federalregister.gov/api/v1/documents.json"
FEDERAL_REGISTER_API_ROOT = "https://www.federalregister.gov/api/v1/"
_DOCUMENT_NUMBER_RE = re.compile(r"^\d{4}-\d{4,6}$")
_AGENCY_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOCUMENT_TYPES = {"RULE", "PRORULE", "NOTICE", "PRESDOCU"}
_DOCUMENT_TYPE_NAMES = {
    "RULE": "Rule",
    "PRORULE": "Proposed Rule",
    "NOTICE": "Notice",
    "PRESDOCU": "Presidential Document",
}
_DOCUMENT_TYPE_CODES = {value.casefold(): key for key, value in _DOCUMENT_TYPE_NAMES.items()}
_FIELDS = (
    "document_number", "title", "publication_date", "type", "abstract", "excerpts",
    "agencies", "html_url", "pdf_url",
)
_FEDERAL_REGISTER_HOSTS = {"www.federalregister.gov", "federalregister.gov"}
_PDF_HOSTS = _FEDERAL_REGISTER_HOSTS | {"www.govinfo.gov"}
_SHALLOW_PAGES = 2
_MAX_DEEP_PAGES = 2


def validate_federal_register_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "search_terms", "agencies", "document_types",
        "lookback_days", "page_size", "max_pages",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"Federal Register source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != FEDERAL_REGISTER_SOURCE_ID:
        raise ValueError(f"source_id must be {FEDERAL_REGISTER_SOURCE_ID!r}")
    if source["discovery_method"] != "federal_register_api":
        raise ValueError("discovery_method must be 'federal_register_api'")
    if source["source_url"] != FEDERAL_REGISTER_API_ROOT:
        raise ValueError(f"source_url must be the fixed Federal Register API root {FEDERAL_REGISTER_API_ROOT}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("Federal Register authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    terms = source["search_terms"]
    if (not isinstance(terms, list) or len(terms) > 5
            or any(not isinstance(term, str) or not term.strip() or len(term) > 120 for term in terms)):
        raise ValueError("search_terms must be a list of up to 5 non-empty terms (max 120 characters)")
    if len({term.casefold() for term in terms}) != len(terms):
        raise ValueError("search_terms must not contain duplicates")
    agencies = source["agencies"]
    if (not isinstance(agencies, list) or len(agencies) > 10
            or any(not isinstance(slug, str) or not _AGENCY_SLUG_RE.fullmatch(slug) for slug in agencies)):
        raise ValueError("agencies must be a list of up to 10 Federal Register agency slugs")
    if len(set(agencies)) != len(agencies):
        raise ValueError("agencies must not contain duplicates")
    document_types = source["document_types"]
    if (not isinstance(document_types, list)
            or any(not isinstance(value, str) or value not in _DOCUMENT_TYPES for value in document_types)):
        raise ValueError(f"document_types must contain only {sorted(_DOCUMENT_TYPES)}")
    if len(set(document_types)) != len(document_types):
        raise ValueError("document_types must not contain duplicates")
    for key, low, high in (("lookback_days", 1, 90), ("page_size", 1, 100), ("max_pages", 2, 2)):
        value = source[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    return source


def _safe_document_url(value: object, document_number: str,
                       allowed_hosts: set[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (parts.scheme.lower() != "https" or not parts.hostname
            or parts.hostname.casefold() not in allowed_hosts
            or parts.port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or document_number not in parts.path):
        return None
    return value.strip()


def _publication_time(value: str) -> datetime:
    published = date.fromisoformat(value)
    return datetime.combine(published, day_time.min, tzinfo=timezone.utc)


class FederalRegisterDiscovery:
    source_id = FEDERAL_REGISTER_SOURCE_ID
    discovery_method = "federal_register_api"

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_federal_register_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _query_url(self, term: str, page: int, start_date: str) -> str:
        params: list[tuple[str, str]] = [
            ("per_page", str(self.source["page_size"])),
            ("page", str(page)),
            ("order", "newest"),
            ("conditions[term]", term),
            ("conditions[publication_date][gte]", start_date),
        ]
        params.extend(("conditions[agencies][]", slug) for slug in self.source["agencies"])
        params.extend(("conditions[type][]", kind) for kind in self.source["document_types"])
        params.extend(("fields[]", field) for field in _FIELDS)
        return FEDERAL_REGISTER_API_URL + "?" + urlencode(params)

    @staticmethod
    def _agencies(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for agency in value:
            if isinstance(agency, str) and agency.strip():
                result.append(agency.strip())
            elif isinstance(agency, dict):
                name = agency.get("name") or agency.get("raw_name")
                if isinstance(name, str) and name.strip():
                    result.append(name.strip())
        return list(dict.fromkeys(result))

    def _candidate(self, document: object, query_url: str, fetched_at: datetime
                   ) -> DiscoveryCandidate:
        if not isinstance(document, dict):
            raise ValueError("document must be an object")
        document_number = document.get("document_number")
        title = document.get("title")
        publication_date = document.get("publication_date")
        document_type_value = document.get("type")
        if not isinstance(document_number, str) or not _DOCUMENT_NUMBER_RE.fullmatch(document_number):
            raise ValueError("missing or invalid document_number")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"document {document_number} has no title")
        if not isinstance(publication_date, str):
            raise ValueError(f"document {document_number} has no publication_date")
        published_at = _publication_time(publication_date)
        if not isinstance(document_type_value, str):
            raise ValueError(f"document {document_number} has an invalid type")
        type_code = (document_type_value if document_type_value in _DOCUMENT_TYPES
                     else _DOCUMENT_TYPE_CODES.get(document_type_value.casefold()))
        if type_code is None:
            raise ValueError(f"document {document_number} has an invalid type")
        document_type = _DOCUMENT_TYPE_NAMES[type_code]
        html_url = _safe_document_url(document.get("html_url"), document_number,
                                      _FEDERAL_REGISTER_HOSTS)
        if not html_url:
            raise ValueError(f"document {document_number} has no approved Federal Register html_url")
        pdf_url = _safe_document_url(document.get("pdf_url"), document_number, _PDF_HOSTS)
        agencies = self._agencies(document.get("agencies"))
        abstract = document.get("abstract")
        if not isinstance(abstract, str):
            abstract = ""
        if not abstract:
            excerpts = document.get("excerpts")
            if isinstance(excerpts, str):
                abstract = excerpts
            elif isinstance(excerpts, list):
                abstract = " ".join(item for item in excerpts if isinstance(item, str))
        summary = abstract.strip()
        if agencies:
            summary = (summary + " " if summary else "") + "Agencies: " + "; ".join(agencies) + "."
        source_native_id = document_number
        candidate = DiscoveryCandidate(
            title=title.strip(), url=html_url, source=self.source["name"],
            published_at=published_at, summary=summary, source_type="discovery",
            source_id=self.source_id, authority_tier=1, category=self.source["category"],
            source_native_id=source_native_id,
            candidate_id=f"{self.source_id}:{source_native_id}",
            discovery_method=self.discovery_method, source_url=query_url,
            document_type=document_type, primary_url=html_url,
            provenance=[FEDERAL_REGISTER_API_ROOT, html_url],
            source_native_metadata={
                "document_number": document_number,
                "publication_date": publication_date,
                "agencies": agencies,
                "html_url": html_url,
                "pdf_url": pdf_url or "",
            },
            collected_at=fetched_at, first_seen_at=fetched_at, last_seen_at=fetched_at,
        )
        if pdf_url:
            candidate.source_native_metadata["pdf_url"] = pdf_url
        return candidate

    def collect(self, state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"] or not self.source["search_terms"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured",
                                   fetched_at=fetched_at)

        cutoff = fetched_at.astimezone(timezone.utc).date() - timedelta(days=self.source["lookback_days"] - 1)
        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        requests_made = 0
        state_sources = state.get("sources", {}) if isinstance(state, dict) else {}
        source_state = state_sources.get(self.source_id, {}) if isinstance(state_sources, dict) else {}
        stored_pagination = source_state.get("pagination", {}) if isinstance(source_state, dict) else {}
        term_state = (stored_pagination.get("federal_register_terms", {})
                      if isinstance(stored_pagination, dict) else {})
        if not isinstance(term_state, dict):
            term_state = {}
        next_term_state: dict[str, dict[str, Any]] = {
            key: dict(value) for key, value in term_state.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        any_term_complete = False
        for term in self.source["search_terms"]:
            has_saved_term = term in term_state
            previous = term_state.get(term, {})
            previous = dict(previous) if isinstance(previous, dict) else {}
            saved_boundary = previous.get("boundary_document_number")
            hint = previous.get("deep_page_hint", 3)
            lookback = previous.get("lookback_days", self.source["lookback_days"])
            frontier_status = previous.get("frontier_status", "active")
            valid_boundary = (saved_boundary is None or
                              (isinstance(saved_boundary, str)
                               and bool(_DOCUMENT_NUMBER_RE.fullmatch(saved_boundary))))
            if (not valid_boundary or type(hint) is not int or hint < 2
                    or type(lookback) is not int or frontier_status not in {"active", "boundary_expired"}
                    or (has_saved_term and lookback != self.source["lookback_days"])):
                errors.append(f"Invalid Federal Register pagination state for term {term!r}; refusing to reset its boundary")
                next_term_state[term] = previous
                continue
            valid_boundary = isinstance(saved_boundary, str)

            page_rows: dict[int, list[str]] = {}
            term_complete = True
            total_pages: int | None = None
            total_pages_trustworthy = True

            def request_page(page: int) -> bool:
                nonlocal requests_made, total_pages, total_pages_trustworthy, term_complete
                if total_pages is not None and page > total_pages:
                    return True
                query_url = self._query_url(term, page, cutoff.isoformat())
                requests_made += 1
                try:
                    response = self.http.get(query_url, expected_content_types=("application/json", "text/json"))
                    payload = json.loads(response.content.decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                        raise ValueError("Federal Register API response must contain a results list")
                    reported_pages = payload.get("total_pages")
                    if type(reported_pages) is int and reported_pages >= 0:
                        if total_pages is None:
                            total_pages = reported_pages
                        elif total_pages != reported_pages:
                            total_pages_trustworthy = False
                    elif "total_pages" in payload:
                        total_pages_trustworthy = False
                    ids: list[str] = []
                    for row in payload["results"]:
                        try:
                            candidate = self._candidate(row, query_url, fetched_at)
                        except (ValueError, TypeError) as exc:
                            errors.append(str(exc))
                            term_complete = False
                            continue
                        candidates.setdefault(candidate.source_native_id, candidate)
                        ids.append(candidate.source_native_id)
                    page_rows[page] = ids
                    return True
                except DiscoveryHttpError as exc:
                    errors.append(f"{exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else ""))
                except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
                    errors.append(str(exc) or "Malformed Federal Register API response")
                term_complete = False
                return False

            # Refresh the first two pages on every run; avoid asking for pages
            # the API has explicitly said do not exist.
            for page in range(1, _SHALLOW_PAGES + 1):
                if total_pages is not None and page > total_pages:
                    break
                if not request_page(page):
                    break

            if not term_complete:
                next_term_state[term] = previous
                continue

            ids_in_order = [identity for page in sorted(page_rows) for identity in page_rows[page]]
            progress = dict(previous)
            progress["lookback_days"] = self.source["lookback_days"]
            if not valid_boundary:
                if ids_in_order:
                    # Page two is the normal seed boundary; if the API has
                    # fewer than two pages, use the oldest available result.
                    seed_page = max(page for page, ids in page_rows.items() if ids)
                    progress.update({
                        "boundary_document_number": page_rows[seed_page][-1],
                        "deep_page_hint": seed_page + 1,
                        "frontier_status": "active",
                    })
                else:
                    progress.update({"deep_page_hint": 3, "frontier_status": "active"})
                next_term_state[term] = progress
                any_term_complete = True
                continue

            boundary_found_shallow = saved_boundary in ids_in_order
            if previous.get("frontier_status") == "boundary_expired" and not boundary_found_shallow:
                next_term_state[term] = progress
                any_term_complete = True
                continue

            if (not boundary_found_shallow and total_pages_trustworthy
                    and total_pages is not None and hint > total_pages):
                progress["frontier_status"] = "boundary_expired"
                next_term_state[term] = progress
                any_term_complete = True
                continue

            deep_start = hint
            deep_pages: list[int] = []
            shallow_boundary_page = None
            if boundary_found_shallow:
                shallow_boundary_page = next(
                    page for page, ids in page_rows.items() if saved_boundary in ids
                )
                # Pages 1–2 were already fully processed, so continue just
                # beyond the positively located shallow boundary rather than
                # trusting an obsolete, deeper page hint.
                deep_start = max(_SHALLOW_PAGES + 1, shallow_boundary_page + 1)
            for page in range(deep_start, deep_start + _MAX_DEEP_PAGES):
                if total_pages is not None and page > total_pages:
                    break
                if not request_page(page):
                    break
                deep_pages.append(page)

            if not term_complete:
                next_term_state[term] = previous
                continue

            progress["frontier_status"] = "active"

            all_ids = [identity for page in sorted(page_rows) for identity in page_rows[page]]
            if saved_boundary in all_ids:
                boundary_index = all_ids.index(saved_boundary)
                rows_beyond = all_ids[boundary_index + 1:]
                if boundary_found_shallow and not deep_pages:
                    # Shallow rows alone may locate the boundary, but the
                    # approved frontier only advances after deep processing.
                    rows_beyond = []
                if rows_beyond:
                    new_boundary = rows_beyond[-1]
                    new_boundary_page = next(
                        page for page in sorted(page_rows, reverse=True)
                        if new_boundary in page_rows[page]
                    )
                    progress["boundary_document_number"] = new_boundary
                    progress["deep_page_hint"] = new_boundary_page + 1
                else:
                    found_page = next(page for page, ids in page_rows.items() if saved_boundary in ids)
                    if boundary_found_shallow:
                        progress["deep_page_hint"] = deep_pages[-1] + 1 if deep_pages else deep_start
                    else:
                        progress["deep_page_hint"] = max(hint, found_page + 1)
                progress["frontier_status"] = "active"
            else:
                # The hint can move, but the document-number boundary cannot.
                # If the API says the hinted page is now outside the result,
                # freeze this frontier instead of silently rebasing it.
                exhausted_results = bool(
                    deep_pages and total_pages_trustworthy and total_pages is not None
                    and max(deep_pages) >= total_pages
                )
                if (total_pages_trustworthy and total_pages is not None
                        and (hint > total_pages or exhausted_results)):
                    progress["frontier_status"] = "boundary_expired"
                else:
                    completed_deep = len(deep_pages)
                    if completed_deep:
                        proposed_hint = hint + min(completed_deep, _MAX_DEEP_PAGES)
                        if total_pages_trustworthy and total_pages is not None:
                            proposed_hint = min(proposed_hint, total_pages)
                        progress["deep_page_hint"] = max(3, proposed_hint)
            next_term_state[term] = progress
            any_term_complete = True

        ordered = sorted(candidates.values(), key=lambda item: (
            item.published_at or fetched_at, item.source_native_id,
        ), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else "success"
        pagination: dict[str, Any] = {"requests_made": requests_made}
        if any_term_complete:
            pagination["federal_register_terms"] = next_term_state
        return DiscoveryResult(
            self.source_id, self.discovery_method, status, candidates=ordered,
            fetched_at=fetched_at, errors=errors,
            pagination=pagination,
        )
