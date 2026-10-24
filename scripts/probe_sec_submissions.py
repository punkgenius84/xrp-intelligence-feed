from __future__ import annotations

from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.http import BoundedHttpClient, DiscoveryHttpError


SEC_ROOT = "https://data.sec.gov/submissions/"
ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data/"
# CIKs are included only for this read-only diagnostics probe. This does not
# activate the production collector or modify its source configuration.
ISSUERS = (
    ("Coinbase Global, Inc.", "1679788"),
    ("Circle Internet Group, Inc.", "1876042"),
    ("PayPal Holdings, Inc.", "1633917"),
    ("Visa Inc.", "1403161"),
)
ACCESSION_RE = re.compile(r"^(\d{10})-(\d{2})-(\d{6})$")


def summarize_payload(payload: object, expected_cik: str) -> dict[str, object]:
    """Validate an SEC submissions response and summarize a bounded sample."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    actual_cik = str(payload.get("cik", ""))
    if not actual_cik.isdigit() or actual_cik.zfill(10) != expected_cik.zfill(10):
        raise ValueError(f"CIK mismatch: expected {expected_cik.zfill(10)}, got {actual_cik or 'missing'}")

    issuer_name = payload.get("name")
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(issuer_name, str) or not issuer_name.strip() or not isinstance(recent, dict):
        raise ValueError("payload is missing issuer name or filings.recent")

    required = ("accessionNumber", "form", "filingDate")
    columns = {key: recent.get(key) for key in required}
    if any(not isinstance(value, list) for value in columns.values()):
        raise ValueError("recent filings is missing required arrays")
    lengths = {len(value) for value in columns.values()}
    if len(lengths) != 1:
        raise ValueError("recent filing arrays have inconsistent lengths")
    row_count = next(iter(lengths), 0)

    documents = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    if not isinstance(documents, list):
        documents = []
    if not isinstance(descriptions, list):
        descriptions = []

    counts: Counter[str] = Counter()
    valid_rows: list[dict[str, str]] = []
    skipped = 0
    for index in range(row_count):
        accession = str(columns["accessionNumber"][index] or "").strip()
        form = str(columns["form"][index] or "").strip()
        filed = str(columns["filingDate"][index] or "").strip()
        match = ACCESSION_RE.fullmatch(accession)
        if not match or match.group(1) != expected_cik.zfill(10):
            skipped += 1
            continue
        try:
            date.fromisoformat(filed)
        except ValueError:
            skipped += 1
            continue
        if not form:
            skipped += 1
            continue
        # primaryDocument can legitimately contain SEC-generated subpaths
        # (for example inline-XBRL directories). This probe builds only the
        # official filing-index URL from CIK + accession, so document paths
        # must not cause otherwise valid submissions rows to be discarded.
        description = str(descriptions[index] or "").strip() if index < len(descriptions) else ""
        counts[form] += 1
        accession_path = accession.replace("-", "")
        index_url = f"{ARCHIVES_ROOT}{int(expected_cik)}/{accession_path}/{accession}-index.htm"
        valid_rows.append({
            "date": filed,
            "form": form,
            "accession": accession,
            "description": description[:180],
            "filing_index_url": index_url,
        })

    valid_rows.sort(key=lambda row: (row["date"], row["accession"]), reverse=True)
    return {
        "issuer_name": issuer_name.strip(),
        "cik": expected_cik.zfill(10),
        "recent_rows": row_count,
        "valid_rows": len(valid_rows),
        "skipped_rows": skipped,
        "form_counts": dict(sorted(counts.items())),
        "sample": valid_rows[:3],
    }


def main() -> int:
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if any(ch in email for ch in "\r\n"):
        print("probe_error=SEC_CONTACT_EMAIL contains a line break")
        return 1
    user_agent = "XRPIntelligenceFeed/0.4 (read-only SEC submissions probe)"
    if email:
        user_agent += f" (contact: {email})"

    client = BoundedHttpClient(
        user_agent=user_agent,
        timeout=(4.0, 10.0),
        max_bytes=2_000_000,
        max_redirects=1,
        max_attempts=1,
    )
    failures = 0
    print("SEC submissions endpoint probe: READ-ONLY; no state writes, no filing-body downloads, no Discord publishing")
    for index, (label, cik) in enumerate(ISSUERS):
        if index:
            time.sleep(1.05)
        url = f"{SEC_ROOT}CIK{cik.zfill(10)}.json"
        try:
            response = client.get(url, expected_content_types=("application/json", "text/json"))
            final_host = urlsplit(response.url).hostname
            if final_host != "data.sec.gov":
                raise ValueError(f"unexpected response host: {final_host or 'missing'}")
            payload = json.loads(response.content.decode("utf-8"))
            summary = summarize_payload(payload, cik)
        except (DiscoveryHttpError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            failures += 1
            if isinstance(exc, DiscoveryHttpError):
                print(f"issuer={label!r} cik={cik} status=unavailable kind={exc.kind} http={exc.status_code or 'n/a'}")
            else:
                print(f"issuer={label!r} cik={cik} status=invalid error={type(exc).__name__}: {exc}")
            continue

        print(
            f"issuer={label!r} cik={cik} status=valid response_name={summary['issuer_name']!r} "
            f"recent_rows={summary['recent_rows']} valid_rows={summary['valid_rows']} "
            f"skipped_rows={summary['skipped_rows']} form_counts={json.dumps(summary['form_counts'], sort_keys=True)}"
        )
        for row in summary["sample"]:
            print(
                f"  filing date={row['date']} form={row['form']} "
                f"description={row['description']!r} accession={row['accession']}"
            )
            print(f"  official_index_url={row['filing_index_url']}")

    print(f"probe_complete issuers={len(ISSUERS)} failures={failures} mode=read_only state_write=disabled publishing=disabled")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
