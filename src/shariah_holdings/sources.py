"""Official source acquisition with explicit retries and MNZL recovery paths."""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

SPUS_CSV_URL = "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPUS.csv"
HLAL_CSV_URL = "https://docs.google.com/spreadsheets/d/1UC1Bk67bGuYsos_i8y_HQpNoHpVHAvqf71MbgrafJOQ/export?format=csv&gid=0"
MNZL_PAGE_URL = "https://manzilfunds.com/"
USER_AGENT = "shariah-holdings-refresh/0.1 (+https://github.com/)"
MNZL_LOCAL_RE = re.compile(r"^mnzl-official-holdings-\d{4}-\d{2}-\d{2}\.csv$", re.I)
CSV_LINK_RE = re.compile(r'''(?:href|src)=["']([^"']+\.csv(?:\?[^"']*)?)["']''', re.I)


def _acquisition_filename() -> str:
    """Date a live official export by the day it was generated/acquired."""
    return f"mnzl-official-holdings-{date.today().isoformat()}.csv"


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


class MnzlAcquirer:
    """Acquire MNZL without claiming its Cloudflare-protected page is stable.

    A dated local issuer export is the reproducible path. Otherwise, a CSV link
    exposed by the public page is used, then an injected browser acquisition is
    attempted. No cache or previous export is silently reused.
    """

    def __init__(self, downloader: HttpDownloader | None,
                 browser_fetch: Callable[[str], tuple[str, str]] | None):
        self.downloader = downloader
        self.browser_fetch = browser_fetch

    def acquire(self, local_export: Path | None = None) -> tuple[str, str, str]:
        if local_export is not None:
            if not MNZL_LOCAL_RE.fullmatch(local_export.name):
                raise ValueError("MNZL local export must be named mnzl-official-holdings-YYYY-MM-DD.csv")
            text = local_export.read_text(encoding="utf-8-sig")
            if not text.strip():
                raise ValueError("MNZL local export is empty")
            return text, MNZL_PAGE_URL, local_export.name

        direct_error: Exception | None = None
        if self.downloader is not None:
            try:
                page = self.downloader.get_text(MNZL_PAGE_URL)
                match = CSV_LINK_RE.search(page)
                if match:
                    url = urljoin(MNZL_PAGE_URL, match.group(1))
                    return self.downloader.get_text(url), url, _acquisition_filename()
                direct_error = RuntimeError("MNZL page exposed no CSV link")
            except Exception as exc:  # boundary: browser is the isolated fallback
                direct_error = exc
        if self.browser_fetch is not None:
            text, filename = self.browser_fetch(MNZL_PAGE_URL)
            if not text.strip():
                raise RuntimeError("MNZL browser download was empty")
            # The site's generated filename is not a reliable as-of marker. The
            # current live export is explicitly dated at acquisition instead.
            return text, MNZL_PAGE_URL, _acquisition_filename()
        raise RuntimeError(f"MNZL acquisition failed; provide a dated local export or browser fallback: {direct_error}")


def playwright_mnzl_download(url: str) -> tuple[str, str]:
    """Capture Manzil's browser-generated download using optional Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed; install the browser extra and Chromium") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            with page.expect_download(timeout=60_000) as pending:
                page.get_by_text(re.compile(r"download.*(?:holding|csv)", re.I)).first.click()
            download = pending.value
            path = download.path()
            if path is None:
                raise RuntimeError("browser download produced no file")
            return Path(path).read_text(encoding="utf-8-sig"), download.suggested_filename
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
