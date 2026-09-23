from __future__ import annotations

from datetime import datetime, time as day_time, timezone
from hashlib import sha256
import json
import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate


SEC_SOURCE_ID = "sec-edgar-submissions"
SEC_SUBMISSIONS_ROOT = "https://data.sec.gov/submissions/"
SEC_ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data/"
_CIK_RE = re.compile(r"^\d{1,10}$")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_FORM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /-]{0,19}$")


def validate_sec_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "issuer_ciks", "filing_forms", "contact_email_env",
        "application_name", "max_issuers_per_run",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"SEC source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url",
                "contact_email_env", "application_name"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["contact_email_env"] != "SEC_CONTACT_EMAIL":
        raise ValueError("contact_email_env must be SEC_CONTACT_EMAIL")
    if "\r" in source["application_name"] or "\n" in source["application_name"]:
        raise ValueError("application_name must not contain line breaks")
    if source["source_id"] != SEC_SOURCE_ID:
        raise ValueError(f"SEC source_id must be {SEC_SOURCE_ID!r}")
    if source["discovery_method"] != "sec_submissions":
        raise ValueError("SEC discovery_method must be 'sec_submissions'")
    if source["source_url"] != SEC_SUBMISSIONS_ROOT:
        raise ValueError(f"source_url must be the documented SEC submissions root {SEC_SUBMISSIONS_ROOT}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("SEC authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    ciks = source["issuer_ciks"]
    if not isinstance(ciks, list) or any(not isinstance(cik, str) or not _CIK_RE.fullmatch(cik)
                                         for cik in ciks):
        raise ValueError("issuer_ciks must be a list of 1-10 digit strings")
    if len(set(ciks)) != len(ciks):
        raise ValueError("issuer_ciks must not contain duplicates")
    forms = source["filing_forms"]
    if not isinstance(forms, list) or any(not isinstance(form, str) or not _FORM_RE.fullmatch(form.strip())
                                          for form in forms):
        raise ValueError("filing_forms must be a list of non-empty SEC form strings")
    if len({form.casefold() for form in forms}) != len(forms):
        raise ValueError("filing_forms must not contain duplicates")
    maximum = source["max_issuers_per_run"]
    if type(maximum) is not int or not 1 <= maximum <= 10:
        raise ValueError("max_issuers_per_run must be from 1 to 10")
    return source


def _parse_filing_date(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(parsed, day_time.min, tzinfo=timezone.utc)


def _filing_url(cik: str, accession: str, primary_document: str) -> str:
    if not _CIK_RE.fullmatch(cik) or not _ACCESSION_RE.fullmatch(accession):
        raise ValueError("invalid SEC filing identity")
    if accession[:10] != cik.zfill(10):
        raise ValueError("SEC accession CIK does not match requested CIK")
    document = primary_document.strip()
    if document and ("/" in document or "\\" in document or document in {".", ".."}):
        raise ValueError("SEC primaryDocument must be a filename")
    accession_path = accession.replace("-", "")
    tail = quote(document, safe="-_.") if document else ""
    return f"{SEC_ARCHIVES_ROOT}{int(cik)}/{accession_path}/" + tail


def _parallel_rows(recent: dict[str, Any]) -> list[dict[str, str]]:
    required = ("accessionNumber", "form", "filingDate")
    columns = {key: recent.get(key) for key in required}
    if any(not isinstance(values, list) for values in columns.values()):
        raise ValueError("SEC submissions recent filings are missing required array fields")
    lengths = {len(values) for values in columns.values()}
    if len(lengths) != 1:
        raise ValueError("SEC submissions recent filing arrays have inconsistent lengths")
    optional = {key: recent.get(key, []) for key in ("primaryDocument", "primaryDocDescription")}
    for key, values in optional.items():
        if not isinstance(values, list):
            optional[key] = []
        elif len(values) < next(iter(lengths), 0):
            optional[key] = values + [""] * (next(iter(lengths), 0) - len(values))
    rows = []
    for index in range(next(iter(lengths), 0)):
        row = {key: str(columns[key][index] or "") for key in required}
        row["primaryDocument"] = str(optional["primaryDocument"][index] or "")
        row["primaryDocDescription"] = str(optional["primaryDocDescription"][index] or "")
        rows.append(row)
    return rows


class SECEdgarDiscovery:
    source_id = SEC_SOURCE_ID
    discovery_method = "sec_submissions"

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 environ: dict[str, str] | None = None,
                 session: Any | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 request_interval: float = 1.0,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_sec_source(source)
        environment = os.environ if environ is None else environ
        email = environment.get(self.source["contact_email_env"], "").strip()
        if "\r" in email or "\n" in email:
            raise ValueError("SEC contact email must not contain line breaks")
        app_name = self.source["application_name"].strip()
        user_agent = f"{app_name} (contact: {email})" if email else app_name
        self.http = http or BoundedHttpClient(user_agent=user_agent, session=session)
        self.sleep = sleep
        self.request_interval = max(1.0, request_interval)
        self.now = now
        self._last_request: float | None = None

    def _pace(self) -> None:
        if self._last_request is not None:
            self.sleep(max(0.0, self.request_interval - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()

    def _fetch_one(self, cik: str, state: dict[str, Any], fetched_at: datetime
                   ) -> tuple[list[DiscoveryCandidate], dict[str, str], str]:
        padded_cik = cik.zfill(10)
        request_url = f"{SEC_SUBMISSIONS_ROOT}CIK{padded_cik}.json"
        request_state = state.get("sources", {}).get(self.source_id, {}).get("requests", {}).get(cik, {})
        self._pace()
        response = self.http.get(
            request_url,
            etag=request_state.get("etag", ""),
            last_modified=request_state.get("last_modified", ""),
            expected_content_types=("application/json", "text/json"),
        )
        validators = {}
        if response.headers.get("etag"):
            validators["etag"] = response.headers["etag"]
        if response.headers.get("last-modified"):
            validators["last_modified"] = response.headers["last-modified"]
        if response.status_code == 304:
            return [], {"etag": validators.get("etag", request_state.get("etag", "")),
                        "last_modified": validators.get("last-modified", request_state.get("last_modified", ""))}, "not_modified"
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed SEC submissions JSON for CIK {padded_cik}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"SEC submissions payload for CIK {padded_cik} must be an object")
        response_cik = str(payload.get("cik", "")).zfill(10)
        if not payload.get("cik"):
            raise ValueError(f"SEC submissions payload is missing CIK {padded_cik}")
        if response_cik != padded_cik:
            raise ValueError(f"SEC submissions CIK mismatch: requested {padded_cik}, received {response_cik}")
        issuer_name = payload.get("name")
        filings = payload.get("filings")
        recent = filings.get("recent") if isinstance(filings, dict) else None
        if not isinstance(issuer_name, str) or not issuer_name.strip() or not isinstance(recent, dict):
            raise ValueError(f"SEC submissions payload for CIK {padded_cik} is missing issuer or filings.recent data")

        accepted_forms = {form.casefold() for form in self.source["filing_forms"]}
        seen_accessions: set[str] = set()
        candidates: list[DiscoveryCandidate] = []
        errors: list[str] = []
        for row in _parallel_rows(recent):
            accession = row["accessionNumber"].strip()
            form = row["form"].strip()
            filing_date = row["filingDate"].strip()
            if (not _ACCESSION_RE.fullmatch(accession)
                    or accession[:10] != padded_cik
                    or not form or not filing_date):
                errors.append("Skipped SEC filing row with missing or malformed identity fields")
                continue
            if form.casefold() not in accepted_forms or accession in seen_accessions:
                continue
            seen_accessions.add(accession)
            try:
                published = _parse_filing_date(filing_date)
                filing_url = _filing_url(cik, accession, row["primaryDocument"])
            except ValueError as exc:
                errors.append(str(exc))
                continue
            native = f"CIK{padded_cik}:{accession}"
            record_hash = sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            desc = row["primaryDocDescription"].strip()
            summary = (f"{issuer_name} (CIK {padded_cik}) filed SEC Form {form} on {filing_date}; "
                       f"accession {accession}." + (f" Document: {desc}." if desc else ""))
            candidates.append(DiscoveryCandidate(
                title=f"SEC Form {form} filing: {issuer_name}",
                url=filing_url,
                source=self.source["name"],
                published_at=published,
                summary=summary,
                source_type="discovery",
                source_id=self.source_id,
                authority_tier=1,
                category=self.source["category"],
                source_native_id=native,
                candidate_id=f"{self.source_id}:{native}",
                discovery_method=self.discovery_method,
                source_url=request_url,
                document_type=form,
                content_hash=record_hash,
                etag=validators.get("etag", ""),
                last_modified=validators.get("last-modified", ""),
                change_kind="new",
                primary_url=filing_url,
                provenance=[request_url, filing_url],
                source_status="success",
                source_native_metadata={
                    "issuer": issuer_name,
                    "cik": padded_cik,
                    "accession_number": accession,
                    "form": form,
                    "filing_date": filing_date,
                    "primary_document": row["primaryDocument"],
                },
                collected_at=fetched_at,
                first_seen_at=fetched_at,
                last_seen_at=fetched_at,
            ))
        candidates.sort(key=lambda item: (item.published_at or fetched_at, item.source_native_id), reverse=True)
        status = "partial" if errors else "success"
        return candidates, validators, status if not errors else "; ".join(errors)

    def collect(self, state: dict[str, Any] | None = None) -> DiscoveryResult:
        state = state or {"sources": {}, "candidates": {}}
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "disabled", fetched_at=fetched_at)
        all_ciks = self.source["issuer_ciks"]
        if not all_ciks or not self.source["filing_forms"]:
            return DiscoveryResult(
                self.source_id, self.discovery_method, "not_configured", fetched_at=fetched_at,
            )
        prior_pagination = state.get("sources", {}).get(self.source_id, {}).get("pagination", {})
        start_index = prior_pagination.get("next_issuer_index", 0) if isinstance(prior_pagination, dict) else 0
        if type(start_index) is not int:
            start_index = 0
        start_index %= len(all_ciks)
        ordered_ciks = all_ciks[start_index:] + all_ciks[:start_index]
        ciks = ordered_ciks[:self.source["max_issuers_per_run"]]
        next_index = (start_index + len(ciks)) % len(all_ciks)

        candidates: list[DiscoveryCandidate] = []
        updates: dict[str, dict[str, str]] = {}
        errors: list[str] = []
        successes = 0
        unchanged = 0
        for cik in ciks:
            try:
                found, validators, result = self._fetch_one(cik, state, fetched_at)
                candidates.extend(found)
                if result == "not_modified":
                    updates[cik] = validators
                    unchanged += 1
                elif result == "success":
                    updates[cik] = validators
                    successes += 1
                else:
                    successes += 1
                    errors.append(f"CIK {cik}: {result}")
                    for candidate in found:
                        candidate.source_status = "partial"
                        candidate.source_error = result
            except DiscoveryHttpError as exc:
                errors.append(f"CIK {cik}: {exc.kind}" +
                              (f" (HTTP {exc.status_code})" if exc.status_code else ""))
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f"CIK {cik}: {exc}")
        previous_candidates = state.get("candidates", {})
        for candidate in candidates:
            previous = previous_candidates.get(candidate.candidate_id, {})
            if previous.get("first_seen_time"):
                try:
                    candidate.first_seen_at = datetime.fromisoformat(
                        previous["first_seen_time"].replace("Z", "+00:00")
                    )
                except ValueError:
                    # JsonDiscoveryState validates timestamps before this state is used.
                    pass
            candidate.last_seen_at = fetched_at
        if errors and (successes or unchanged):
            status = "partial"
        elif errors:
            status = "failed"
        elif unchanged == len(ciks):
            status = "not_modified"
        else:
            status = "success"
        return DiscoveryResult(self.source_id, self.discovery_method, status,
                               candidates=candidates, fetched_at=fetched_at,
                               errors=errors, state_updates=updates,
                               pagination={"next_issuer_index": next_index})
