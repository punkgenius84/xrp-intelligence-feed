from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests


@dataclass(slots=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str


class DiscoveryHttpError(RuntimeError):
    def __init__(self, message: str, *, kind: str, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


@dataclass(slots=True)
class HttpAttemptBudget:
    """Shared cap on outbound HTTP sends across one or more client GETs."""

    maximum: int
    used: int = 0

    def __post_init__(self) -> None:
        if type(self.maximum) is not int or self.maximum < 1:
            raise ValueError("HTTP attempt budget must be a positive integer")

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise DiscoveryHttpError("HTTP outbound-attempt budget exhausted",
                                     kind="attempt_budget_exhausted")
        self.used += 1


def _retry_after(value: str | None, maximum: float) -> float:
    if not value:
        return 0.0
    try:
        return min(maximum, max(0.0, float(value)))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return min(maximum, max(0.0, (stamp - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0.0


class BoundedHttpClient:
    """Small HTTPS-only GET client with explicit limits and conditional request support."""

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}
    REDIRECT_STATUS = {301, 302, 303, 307, 308}

    def __init__(self, *, user_agent: str, timeout: tuple[float, float] = (5.0, 20.0),
                 max_bytes: int = 2_000_000, max_redirects: int = 3,
                 max_attempts: int = 3, max_retry_after: float = 5.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not user_agent.strip():
            raise ValueError("User-Agent must be non-empty")
        if max_bytes < 1 or max_redirects < 0 or max_attempts < 1:
            raise ValueError("HTTP bounds must be positive")
        self.user_agent = user_agent.strip()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts
        self.max_retry_after = max_retry_after
        self.session = session or requests.Session()
        self.sleep = sleep

    @staticmethod
    def _https_url(url: str) -> None:
        parts = urlsplit(url)
        if (parts.scheme.lower() != "https" or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise DiscoveryHttpError("Only absolute HTTPS URLs are allowed", kind="invalid_url")

    def get(self, url: str, *, etag: str = "", last_modified: str = "",
            expected_content_types: tuple[str, ...] = (),
            attempt_budget: HttpAttemptBudget | None = None) -> HttpResponse:
        self._https_url(url)
        original_host = urlsplit(url).hostname.casefold()
        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        current_url = url
        redirects = 0
        attempts = 0
        while True:
            self._https_url(current_url)
            if attempt_budget is not None:
                # Count each actual session send, including retries and redirects.
                attempt_budget.consume()
            try:
                response = self.session.get(
                    current_url, headers=headers, timeout=self.timeout, stream=True,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                attempts += 1
                if attempts < self.max_attempts:
                    self.sleep(min(2.0, 0.25 * (2 ** (attempts - 1))))
                    continue
                raise DiscoveryHttpError("HTTP request timed out", kind="timeout") from exc
            except requests.RequestException as exc:
                attempts += 1
                if attempts < self.max_attempts:
                    self.sleep(min(2.0, 0.25 * (2 ** (attempts - 1))))
                    continue
                raise DiscoveryHttpError("HTTP request failed", kind="request_error") from exc

            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            status = response.status_code
            if status in self.REDIRECT_STATUS:
                location = response.headers.get("Location")
                response.close()
                if not location or redirects >= self.max_redirects:
                    raise DiscoveryHttpError("HTTP redirect limit exceeded or Location missing",
                                             kind="redirect_error", status_code=status)
                current_url = urljoin(current_url, location)
                redirects += 1
                if urlsplit(current_url).hostname.casefold() != original_host:
                    raise DiscoveryHttpError("Cross-host redirects are not allowed",
                                             kind="redirect_error", status_code=status)
                continue
            if status == 304:
                response.close()
                return HttpResponse(status, response_headers, b"", current_url)
            if status in self.RETRYABLE_STATUS and attempts + 1 < self.max_attempts:
                delay = (_retry_after(response.headers.get("Retry-After"), self.max_retry_after)
                         if status == 429 else min(2.0, 0.25 * (2 ** attempts)))
                response.close()
                attempts += 1
                self.sleep(delay)
                continue
            if status < 200 or status >= 300:
                response.close()
                raise DiscoveryHttpError(f"HTTP status {status}", kind="http_error", status_code=status)

            content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if expected_content_types and content_type not in expected_content_types:
                response.close()
                raise DiscoveryHttpError(f"Unexpected content type: {content_type or 'missing'}",
                                         kind="invalid_content_type", status_code=status)
            length = response_headers.get("content-length", "")
            if length.isdigit() and int(length) > self.max_bytes:
                response.close()
                raise DiscoveryHttpError("HTTP response exceeds configured size limit",
                                         kind="response_too_large", status_code=status)
            chunks: list[bytes] = []
            size = 0
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DiscoveryHttpError("HTTP response exceeds configured size limit",
                                                 kind="response_too_large", status_code=status)
                    chunks.append(chunk)
            except DiscoveryHttpError:
                raise
            except requests.Timeout as exc:
                attempts += 1
                if attempts < self.max_attempts:
                    self.sleep(min(2.0, 0.25 * (2 ** (attempts - 1))))
                    continue
                raise DiscoveryHttpError("HTTP response timed out", kind="timeout") from exc
            except requests.RequestException as exc:
                attempts += 1
                if attempts < self.max_attempts:
                    self.sleep(min(2.0, 0.25 * (2 ** (attempts - 1))))
                    continue
                raise DiscoveryHttpError("HTTP response stream failed", kind="request_error") from exc
            finally:
                response.close()
            return HttpResponse(status, response_headers, b"".join(chunks), current_url)
