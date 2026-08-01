from datetime import date
from pathlib import Path

import httpx
import pytest

from shariah_holdings.sources import (HLAL_CSV_URL, SPUS_CSV_URL, DateConflictError,
                                      HttpDownloader, MnzlAcquirer, acquire_all_sources,
                                      playwright_mnzl_download)


def test_http_downloader_retries_transient_failures_without_accepting_empty_body():
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        if calls == 2:
            return httpx.Response(200, text="   ")
        return httpx.Response(200, content=b"\xef\xbb\xbfcol\nvalue\n")
    sleeps = []
    downloader = HttpDownloader(client=httpx.Client(transport=httpx.MockTransport(handler)),
                                attempts=3, sleep=sleeps.append)
    assert downloader.get_text("https://example.test/data.csv") == "col\nvalue\n"
    assert calls == 3 and sleeps == [1, 2]


def test_http_downloader_does_not_return_partial_result_after_final_failure():
    downloader = HttpDownloader(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(500))), attempts=2, sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="failed to download"):
        downloader.get_text("https://example.test/data.csv")


def test_mnzl_prefers_explicit_dated_official_export(tmp_path):
    export = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    export.write_text("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", encoding="utf-8")
    source = MnzlAcquirer(downloader=None, browser_fetch=None).acquire(export)
    assert source[0].startswith("TICKER") and source[2] == export.name


def test_mnzl_discovers_content_download_and_requires_verified_as_of_date():
    pages = {
        "https://manzilfunds.com/": ('<time>Holdings as of July 31, 2026</time>'
                                     '<a data-download-holdings href="/wp-content/uploads/export?id=7">Download</a>'),
        "https://manzilfunds.com/wp-content/uploads/export?id=7":
            "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n",
    }
    class FakeDownloader:
        def get_text(self, url): return pages[url]
    direct = MnzlAcquirer(FakeDownloader(), None).acquire()
    assert direct[1].endswith("/wp-content/uploads/export?id=7")
    assert direct[2] == "mnzl-official-holdings-2026-07-31.csv"
    browser = lambda url: ("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", "holdings.csv", None)
    class Blocked:
        def get_text(self, url): raise RuntimeError("cloudflare")
    with pytest.raises(RuntimeError, match="as-of date"):
        MnzlAcquirer(Blocked(), browser).acquire()
    assert MnzlAcquirer(Blocked(), browser).acquire(as_of_date=date(2026, 7, 31))[2].endswith("2026-07-31.csv")


def test_mnzl_caller_date_must_match_direct_and_browser_issuer_dates():
    class Direct:
        def get_text(self, url):
            if url.endswith("/"):
                return ('Holdings as of 2026-07-31 '
                        '<a href="/wp-content/download">Download holdings</a>')
            return "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n"

    browser_called = False

    def browser(url):
        nonlocal browser_called
        browser_called = True
        return ("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", "holdings.csv", date(2026, 7, 31))

    with pytest.raises(ValueError, match="conflicts with issuer"):
        MnzlAcquirer(Direct(), browser).acquire(as_of_date=date(2026, 7, 30))
    assert not browser_called

    class Blocked:
        def get_text(self, url):
            raise RuntimeError("blocked")

    with pytest.raises(ValueError, match="conflicts with issuer"):
        MnzlAcquirer(Blocked(), browser).acquire(as_of_date=date(2026, 7, 30))


def test_mnzl_local_filename_date_must_match_supplied_date(tmp_path):
    export = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    export.write_text("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts with issuer"):
        MnzlAcquirer(None, None).acquire(export, as_of_date=date(2026, 7, 30))


def test_mnzl_fallback_is_used_only_after_live_acquisition_failure(tmp_path):
    fallback = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    fallback.write_text("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", encoding="utf-8")
    calls = []

    class Blocked:
        def get_text(self, url):
            calls.append(("direct", url))
            raise RuntimeError("blocked")

    def broken_browser(url):
        calls.append(("browser", url))
        raise RuntimeError("browser blocked")

    result = MnzlAcquirer(Blocked(), broken_browser).acquire(fallback_export=fallback)
    assert result[2] == fallback.name
    assert [kind for kind, _ in calls] == ["direct", "browser"]


def test_mnzl_fallback_is_not_used_for_explicit_date_conflict(tmp_path):
    fallback = tmp_path / "mnzl-official-holdings-2026-07-30.csv"
    fallback.write_text("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", encoding="utf-8")

    class Direct:
        def get_text(self, url):
            if url.endswith("/"):
                return ('Holdings as of 2026-07-31 '
                        '<a href="/wp-content/download">Download holdings</a>')
            return "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n"

    with pytest.raises(DateConflictError, match="conflicts with issuer"):
        MnzlAcquirer(Direct(), None).acquire(
            as_of_date=date(2026, 7, 30), fallback_export=fallback)


def test_mnzl_verified_live_result_wins_over_fallback(tmp_path):
    fallback = tmp_path / "mnzl-official-holdings-2026-07-30.csv"
    fallback.write_text("not,a,valid,mnzl,csv\n", encoding="utf-8")

    class Direct:
        def get_text(self, url):
            if url.endswith("/"):
                return ('Holdings as of 2026-07-31 '
                        '<a href="/wp-content/download">Download holdings</a>')
            return "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n"

    assert MnzlAcquirer(Direct(), None).acquire(fallback_export=fallback)[2].endswith(
        "2026-07-31.csv")


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "ftp://manzilfunds.com/holdings.csv",
    "http://127.0.0.1/holdings.csv", "https://evil.example/holdings.csv",
])
def test_mnzl_rejects_unsafe_or_non_issuer_discovered_urls(href):
    class FakeDownloader:
        def __init__(self):
            self.seen = []

        def get_text(self, url):
            self.seen.append(url)
            return f'Holdings as of 2026-07-31 <a href="{href}">Download holdings</a>'

    downloader = FakeDownloader()
    with pytest.raises(RuntimeError, match="no discoverable holdings download"):
        MnzlAcquirer(downloader, None).acquire()
    assert downloader.seen == ["https://manzilfunds.com/"]


def test_mnzl_rejects_non_csv_content_even_when_download_link_exists():
    class FakeDownloader:
        def get_text(self, url):
            return ('Holdings as of 2026-07-31 <a href="/wp-content/download">Download holdings</a>'
                    if url.endswith("/") else "<!doctype html>blocked")
    with pytest.raises(RuntimeError, match="CSV header"):
        MnzlAcquirer(FakeDownloader(), None).acquire()


def test_playwright_blob_download_from_representative_local_page(tmp_path, monkeypatch):
    pytest.importorskip("playwright.sync_api")
    # Keep the browser/Blob integration local; production host enforcement is
    # covered separately with an unpatched page mock.
    import shariah_holdings.sources as source_module
    monkeypatch.setattr(source_module, "_require_safe_mnzl_page", lambda url: None)
    page = tmp_path / "mnzl.html"
    page.write_text('''<!doctype html><button data-download-holdings aria-label="Download holdings CSV">Download</button>
<script>document.querySelector('[data-download-holdings]').onclick=()=>{
const csv='TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\\nAVGO,Broadcom Inc,11135F101,3.997,100\\n';
const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
a.download='MNZL Fund Holdings.csv'; a.click();};</script>''', encoding="utf-8")
    try:
        csv_text, filename, as_of = playwright_mnzl_download(page.as_uri())
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium is not installed")
        raise
    assert filename == "MNZL Fund Holdings.csv"
    assert csv_text.startswith("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n")
    assert as_of is None


def test_acquire_all_uses_official_direct_urls_and_injected_mnzl():
    seen = []
    class FakeDownloader:
        def get_text(self, url):
            seen.append(url)
            return "csv"
    sources = acquire_all_sources(FakeDownloader(), lambda: ("mnzl", "https://manzilfunds.com/",
                                                               "mnzl-official-holdings-2026-07-31.csv"))
    assert seen == [SPUS_CSV_URL, HLAL_CSV_URL]
    assert set(sources) == {"SPUS", "HLAL", "MNZL"}


def test_http_downloader_rejects_oversize_and_redirect_escape(monkeypatch):
    import shariah_holdings.sources as source_module
    monkeypatch.setattr(source_module, "MAX_DOWNLOAD_BYTES", 5)
    oversized = HttpDownloader(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"123456"))), attempts=1)
    with pytest.raises(RuntimeError, match="size limit"):
        oversized.get_text("https://example.test/data.csv")

    def redirect(request):
        if request.url.host == "www.sp-funds.com":
            return httpx.Response(302, headers={"location": "https://evil.example/file.csv"})
        return httpx.Response(200, text="stolen")
    escaped = HttpDownloader(client=httpx.Client(transport=httpx.MockTransport(redirect),
                                                 follow_redirects=True), attempts=1)
    with pytest.raises(RuntimeError, match="allowed host"):
        escaped.get_text(SPUS_CSV_URL)


def test_http_downloader_checks_redirect_before_requesting_target():
    seen = []

    def redirect(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/file.csv"})

    downloader = HttpDownloader(client=httpx.Client(transport=httpx.MockTransport(redirect),
                                                    follow_redirects=True), attempts=1)
    with pytest.raises(RuntimeError, match="allowed host"):
        downloader.get_text(SPUS_CSV_URL)
    assert seen == ["www.sp-funds.com"]


def test_browser_rejects_redirect_escape_before_finding_or_clicking_control():
    import shariah_holdings.sources as source_module

    class Page:
        url = "https://evil.example/phishing"

        def __init__(self):
            self.locator_called = False

        def goto(self, url, wait_until):
            assert url == "https://manzilfunds.com/"

        def on(self, event, handler):
            assert event == "response"

        def locator(self, selector):
            self.locator_called = True
            raise AssertionError("controls must not be inspected after a redirect escape")

    page = Page()
    with pytest.raises(RuntimeError, match="escaped allowed host or HTTPS"):
        source_module._capture_mnzl_download(page, "https://manzilfunds.com/")
    assert not page.locator_called


def test_browser_cancels_download_with_oversized_content_length(monkeypatch):
    import shariah_holdings.sources as source_module
    monkeypatch.setattr(source_module, "MAX_DOWNLOAD_BYTES", 5)

    class Download:
        url = "https://manzilfunds.com/holdings.csv"
        suggested_filename = "holdings.csv"

        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def path(self):
            raise AssertionError("oversized download must be cancelled before waiting for its path")

    class Pending:
        def __init__(self, download): self.value = download
        def __enter__(self): return self
        def __exit__(self, *args): return None

    class Control:
        def click(self): pass
        def is_visible(self): return True

    class Locator:
        first = Control()
        def count(self): return 1
        def is_visible(self): return True

    class Response:
        url = "https://manzilfunds.com/holdings.csv"
        headers = {"content-length": "6", "content-type": "text/csv"}

    class Page:
        url = "https://manzilfunds.com/funds/mnzl"
        def __init__(self, download): self.download, self.response_handler = download, None
        def on(self, event, handler):
            if event == "response": self.response_handler = handler
        def goto(self, url, wait_until): pass
        def locator(self, selector): return Locator()
        def content(self): return "Holdings as of 2026-07-31"
        def expect_download(self, timeout):
            self.response_handler(Response())
            return Pending(self.download)

    download = Download()
    with pytest.raises(RuntimeError, match="size limit"):
        source_module._capture_mnzl_download(Page(download), "https://manzilfunds.com/")
    assert download.cancelled
