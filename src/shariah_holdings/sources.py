"""Official source acquisition with explicit retries and MNZL recovery paths."""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

SPUS_CSV_URL = "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPUS.csv"
HLAL_CSV_URL = "https://docs.google.com/spreadsheets/d/1UC1Bk67bGuYsos_i8y_HQpNoHpVHAvqf71MbgrafJOQ/export?format=csv&gid=0"
MNZL_PAGE_URL = "https://manzilfunds.com/"
USER_AGENT = "shariah-holdings-refresh/0.1 (+https://github.com/)"
MNZL_LOCAL_RE = re.compile(r"^mnzl-official-holdings-\d{4}-\d{2}-\d{2}\.csv$", re.I)
LINK_RE = re.compile(r'''<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>''', re.I | re.S)
MNZL_HEADER = "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS"


def _filename(as_of_date: date) -> str:
    return f"mnzl-official-holdings-{as_of_date.isoformat()}.csv"


class DateConflictError(ValueError):
    """A caller assertion disagrees with an issuer-provided holdings date."""


def _verified_date(caller_date: date | None, issuer_date: date | None) -> date | None:
    if caller_date is not None and issuer_date is not None and caller_date != issuer_date:
        raise DateConflictError(
            f"caller as-of date {caller_date} conflicts with issuer date {issuer_date}")
    return issuer_date if issuer_date is not None else caller_date


def _extract_as_of_date(content: str) -> date | None:
    """Extract an issuer-labelled holdings date, never an acquisition date."""
    labelled = re.search(
        r"(?:holdings\s+)?(?:as\s+of|as-of)\s*[:\-]?\s*"
        r"((?:\d{4}-\d{2}-\d{2})|(?:[A-Za-z]+\s+\d{1,2},?\s+\d{4})|(?:\d{1,2}/\d{1,2}/\d{4}))",
        content, re.I,
    )
    if not labelled:
        return None
    value = labelled.group(1).replace(",", "")
    for fmt in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def _safe_mnzl_url(candidate: str) -> str | None:
    url = urljoin(MNZL_PAGE_URL, candidate.replace("&amp;", "&"))
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"}
            or not (hostname == "manzilfunds.com" or hostname.endswith(".manzilfunds.com"))):
        return None
    return url


def _discover_download_url(page: str) -> str | None:
    """Find literal CSV and WordPress/generated holdings controls."""
    for href, body in LINK_RE.findall(page):
        context = f"{href} {re.sub(r'<[^>]+>', ' ', body)}"
        if (".csv" in href.lower() or "wp-content" in href.lower()
                or re.search(r"download|holding|portfolio", context, re.I)):
            safe_url = _safe_mnzl_url(href)
            if safe_url:
                return safe_url
    # Some WordPress builders place the URL in JSON/data attributes, not anchors.
    embedded = re.search(
        r'''["']((?:https?://|/)[^"']*(?:wp-content|download)[^"']*)["']''', page, re.I)
    return _safe_mnzl_url(embedded.group(1)) if embedded else None


def _validate_mnzl_csv(text: str) -> str:
    normalized = text.lstrip("\ufeff")
    first_line = normalized.splitlines()[0].replace('"', "") if normalized.splitlines() else ""
    if first_line != MNZL_HEADER:
        raise RuntimeError("MNZL download did not contain the expected CSV header")
    return normalized


class HttpDownloader:
    def __init__(self, client: httpx.Client | None = None, attempts: int = 3,
                 timeout: float = 60, sleep: Callable[[float], None] = time.sleep):
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                             follow_redirects=True)
        self.attempts = attempts
        self.sleep = sleep

    def get_text(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.client.get(url)
                response.raise_for_status()
                text = response.content.decode("utf-8-sig")
                if not text.strip():
                    raise ValueError("download was empty")
                return text
            except (httpx.HTTPError, UnicodeError, ValueError) as exc:
                last_error = exc
                if attempt < self.attempts:
                    self.sleep(attempt)
        raise RuntimeError(f"failed to download {url}: {last_error}") from last_error


BrowserResult = tuple[str, str] | tuple[str, str, date | None]


class MnzlAcquirer:
    """Acquire a fresh MNZL export without asserting unverified live availability."""

    def __init__(self, downloader: HttpDownloader | None,
                 browser_fetch: Callable[[str], BrowserResult] | None):
        self.downloader = downloader
        self.browser_fetch = browser_fetch

    def acquire(self, local_export: Path | None = None,
                as_of_date: date | None = None) -> tuple[str, str, str]:
        if local_export is not None:
            if not MNZL_LOCAL_RE.fullmatch(local_export.name):
                raise ValueError("MNZL local export must be named mnzl-official-holdings-YYYY-MM-DD.csv")
            filename_date = date.fromisoformat(local_export.name[-14:-4])
            _verified_date(as_of_date, filename_date)
            text = _validate_mnzl_csv(local_export.read_text(encoding="utf-8-sig"))
            return text, MNZL_PAGE_URL, local_export.name

        direct_error: Exception | None = None
        if self.downloader is not None:
            try:
                page = self.downloader.get_text(MNZL_PAGE_URL)
                issuer_date = _extract_as_of_date(page)
                verified_date = _verified_date(as_of_date, issuer_date)
                url = _discover_download_url(page)
                if not url:
                    raise RuntimeError("MNZL page exposed no discoverable holdings download")
                text = _validate_mnzl_csv(self.downloader.get_text(url))
                if verified_date is None:
                    raise RuntimeError("MNZL holdings as-of date is unverified; supply --mnzl-as-of")
                return text, url, _filename(verified_date)
            except DateConflictError:
                raise
            except Exception as exc:  # isolated browser fallback boundary
                direct_error = exc
        if self.browser_fetch is not None:
            result = self.browser_fetch(MNZL_PAGE_URL)
            text, _suggested = result[:2]
            browser_date = result[2] if len(result) == 3 else None
            text = _validate_mnzl_csv(text)
            verified_date = _verified_date(as_of_date, browser_date)
            if verified_date is None:
                raise RuntimeError("MNZL browser holdings as-of date is unverified; supply --mnzl-as-of")
            return text, MNZL_PAGE_URL, _filename(verified_date)
        raise RuntimeError(
            "MNZL acquisition failed; provide a dated local export or browser fallback: "
            f"{direct_error}") from direct_error


def playwright_mnzl_download(url: str) -> tuple[str, str, date | None]:
    """Capture a JS/Blob download using stable control attributes and verify it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed; install the browser extra and Chromium") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(accept_downloads=True)
            page.goto(url, wait_until="networkidle")
            selectors = [
                "[data-download-holdings]",
                "[data-testid='download-holdings']",
                "a[download]",
            ]
            control = None
            for selector in selectors:
                candidate = page.locator(selector)
                if candidate.count() and candidate.first.is_visible():
                    control = candidate.first
                    break
            if control is None:
                candidate = page.get_by_role("button", name=re.compile(r"download.*(?:holding|csv)", re.I))
                if not candidate.count():
                    candidate = page.get_by_role("link", name=re.compile(r"download.*(?:holding|csv)", re.I))
                if not candidate.count():
                    raise RuntimeError("MNZL holdings download control was not found")
                control = candidate.first
            page_content = page.content()
            with page.expect_download(timeout=60_000) as pending:
                control.click()
            download = pending.value
            path = download.path()
            if path is None:
                raise RuntimeError("browser download produced no file")
            text = _validate_mnzl_csv(Path(path).read_text(encoding="utf-8-sig"))
            return text, download.suggested_filename, _extract_as_of_date(page_content)
        finally:
            browser.close()


def acquire_all_sources(downloader: HttpDownloader,
                        mnzl_acquire: Callable[[], tuple[str, str, str]]) -> dict[str, tuple[str, ...]]:
    # Sequential and uncached by design: any failure aborts this acquisition set.
    spus = downloader.get_text(SPUS_CSV_URL)
    hlal = downloader.get_text(HLAL_CSV_URL)
    mnzl = mnzl_acquire()
    return {
        "SPUS": (spus, SPUS_CSV_URL),
        "HLAL": (hlal, HLAL_CSV_URL),
        "MNZL": mnzl,
    }
