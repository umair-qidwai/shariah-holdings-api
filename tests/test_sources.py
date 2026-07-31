from pathlib import Path

import httpx
import pytest

from shariah_holdings.sources import (HLAL_CSV_URL, SPUS_CSV_URL, HttpDownloader,
                                      MnzlAcquirer, acquire_all_sources)


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


def test_mnzl_extracts_csv_link_then_uses_injected_browser_fallback():
    pages = {
        "https://manzilfunds.com/": '<a href="https://cdn.test/mnzl.csv">Download CSV</a>',
        "https://cdn.test/mnzl.csv": "TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n",
    }
    class FakeDownloader:
        def get_text(self, url): return pages[url]
    direct = MnzlAcquirer(FakeDownloader(), None).acquire()
    assert direct[1] == "https://cdn.test/mnzl.csv"
    assert direct[2].startswith("mnzl-official-holdings-")
    browser = lambda url: ("TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n", "holdings.csv")
    class Blocked:
        def get_text(self, url): raise RuntimeError("cloudflare")
    assert MnzlAcquirer(Blocked(), browser).acquire()[2].startswith("mnzl-official-holdings-")


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
