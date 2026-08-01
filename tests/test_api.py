import csv
import io
import json
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.index import app as vercel_app
from shariah_holdings.api import create_app
from shariah_holdings.refresh import ValidationPolicy, refresh_to_directory
from shariah_holdings.repository import DataUnavailable, HoldingsRepository

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = ValidationPolicy(
    max_age_days=7,
    minimum_holdings={"SPUS": 2, "HLAL": 2, "MNZL": 2},
    minimum_source_rows={"SPUS": 1, "HLAL": 1, "MNZL": 1},
    maximum_malformed_rate=Decimal("1"),
)
NOW = datetime(2026, 7, 31, 18, tzinfo=timezone.utc)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def sources(*, apple_name: str = "Apple Inc") -> dict[str, tuple[str, ...]]:
    return {
        "SPUS": (
            fixture_text("SPUS.csv").replace("Apple Inc", apple_name),
            "https://official.test/SPUS.csv",
        ),
        "HLAL": (fixture_text("HLAL.csv"), "https://official.test/HLAL.csv"),
        "MNZL": (
            fixture_text("mnzl-official-holdings-2026-07-31.csv"),
            "https://manzilfunds.com/",
            "mnzl-official-holdings-2026-07-31.csv",
        ),
    }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    refresh_to_directory(sources(), tmp_path, POLICY, NOW)
    return tmp_path


@pytest.fixture
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app(data_dir=data_dir))


def generation_dir(data_dir: Path) -> Path:
    return next((data_dir / "generations").iterdir())


def assert_generation_unavailable(data_dir: Path, path: str = "/stocks") -> None:
    response = TestClient(create_app(data_dir=data_dir), raise_server_exceptions=False).get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Authoritative data is unavailable"}


def test_vercel_entry_exposes_asgi_application():
    assert isinstance(vercel_app, FastAPI)


def test_health_metadata_docs_and_openapi(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "generation": health.json()["generation"],
        "checked_at": "2026-07-31T18:00:00Z",
    }
    assert len(health.json()["generation"]) == 24

    metadata = client.get("/metadata")
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["checked_at"] == "2026-07-31T18:00:00Z"
    assert body["unique_symbols"] == 4
    assert body["generation"] == health.json()["generation"]
    assert {row["fund"]: row["holding_count"] for row in body["funds"]} == {
        "HLAL": 2,
        "MNZL": 2,
        "SPUS": 2,
    }
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/stocks" in schema.json()["paths"]


def test_stocks_pagination_search_and_fund_filter(client: TestClient):
    response = client.get("/stocks", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["page_size"], body["pages"]) == (4, 1, 2, 2)
    assert [item["symbol"] for item in body["items"]] == ["AAPL", "AVGO"]
    assert body["checked_at"] == "2026-07-31T18:00:00Z"
    assert body["items"][0]["membership"] == ["HLAL", "MNZL", "SPUS"]

    assert [item["symbol"] for item in client.get("/stocks?page=2&page_size=2").json()["items"]] == [
        "MSFT",
        "TSLA",
    ]
    assert [item["symbol"] for item in client.get("/stocks?search=broad").json()["items"]] == ["AVGO"]
    fund = client.get("/stocks?fund=spus").json()
    assert [item["symbol"] for item in fund["items"]] == ["AAPL", "MSFT"]


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101"])
def test_stocks_rejects_pagination_outside_bounds(client: TestClient, query: str):
    assert client.get(f"/stocks?{query}").status_code == 422


def test_stock_lookup_is_case_insensitive_exact_and_has_per_fund_details(client: TestClient):
    response = client.get("/stocks/aapl")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["membership"] == ["HLAL", "MNZL", "SPUS"]
    assert [item["fund"] for item in body["funds"]] == ["HLAL", "MNZL", "SPUS"]
    assert all(item["holdings_date"] == "2026-07-31" for item in body["funds"])
    assert all(item["source_url"].startswith("https://") for item in body["funds"])
    assert client.get("/stocks/app").status_code == 404
    assert client.get("/stocks/DOES-NOT-EXIST").status_code == 404


@pytest.mark.parametrize("path", [
    "/stocks/%20AAPL", "/stocks/AAPL%20", "/stocks/AAPL!",
    "/funds/%20SPUS/holdings", "/funds/SPUS%20/holdings", "/funds/SPUS!/holdings",
])
def test_path_tokens_reject_whitespace_and_malformed_values(client: TestClient, path: str):
    assert client.get(path).status_code == 404


def test_path_tokens_remain_case_insensitive(client: TestClient):
    assert client.get("/stocks/aApL").status_code == 200
    assert client.get("/funds/sPuS/holdings").status_code == 200


def test_funds_and_fund_holdings_filter_and_pagination(client: TestClient):
    response = client.get("/funds")
    assert response.status_code == 200
    assert [item["fund"] for item in response.json()["items"]] == ["HLAL", "MNZL", "SPUS"]
    spus = next(item for item in response.json()["items"] if item["fund"] == "SPUS")
    assert spus == {
        "fund": "SPUS",
        "holding_count": 2,
        "holdings_date": "2026-07-31",
        "source_url": "https://official.test/SPUS.csv",
    }

    holdings = client.get("/funds/spus/holdings?page=1&page_size=1")
    assert holdings.status_code == 200
    assert (holdings.json()["total"], holdings.json()["pages"]) == (2, 2)
    assert holdings.json()["items"][0]["symbol"] == "AAPL"
    search = client.get("/funds/HLAL/holdings?search=tesla").json()
    assert [item["symbol"] for item in search["items"]] == ["TSLA"]

    assert client.get("/funds/NOPE/holdings").status_code == 404
    assert client.get("/funds/SPUS/holdings?page=0").status_code == 422
    assert client.get("/funds/SPUS/holdings?page_size=101").status_code == 422


def test_overlap_has_exact_categories_and_pairwise_counts(client: TestClient):
    response = client.get("/overlap")
    assert response.status_code == 200
    body = response.json()
    assert body["fund_totals"] == {"SPUS": 2, "HLAL": 2, "MNZL": 2}
    assert body["union"] == 4
    assert body["exactly_one"] == 3
    assert body["at_least_two"] == 1
    assert body["pairwise_including_triple"] == {
        "SPUS_HLAL": 1,
        "SPUS_MNZL": 1,
        "HLAL_MNZL": 1,
        "SPUS_HLAL_MNZL": 1,
    }
    assert body["exact_only"] == {
        "SPUS": 1,
        "HLAL": 1,
        "MNZL": 1,
        "SPUS_HLAL": 0,
        "SPUS_MNZL": 0,
        "HLAL_MNZL": 0,
        "SPUS_HLAL_MNZL": 1,
    }
    assert body["checked_at"] == "2026-07-31T18:00:00Z"


def test_csv_downloads_are_current_generation_with_safe_headers(client: TestClient):
    allowlist = client.get("/downloads/shariah-list.csv")
    assert allowlist.status_code == 200
    assert allowlist.headers["content-type"].startswith("text/csv")
    assert allowlist.headers["content-disposition"] == 'attachment; filename="shariah-list.csv"'
    rows = list(csv.DictReader(io.StringIO(allowlist.text)))
    assert [row["symbol"] for row in rows] == ["AAPL", "AVGO", "MSFT", "TSLA"]

    holdings = client.get("/downloads/holdings.csv")
    assert holdings.status_code == 200
    assert holdings.headers["content-disposition"] == 'attachment; filename="holdings.csv"'
    detail = list(csv.DictReader(io.StringIO(holdings.text)))
    assert len(detail) == 6
    assert set(detail[0]) == {
        "fund", "symbol", "name", "cusip", "weight", "shares", "market_value",
        "holdings_date", "source_url",
    }
    assert client.get("/downloads/current.json").status_code == 404


def test_homepage_is_responsive_and_contains_counts_overlap_sources_and_links(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for text in [
        "Shariah Holdings", "2026-07-31T18:00:00Z", "SPUS", "HLAL", "MNZL",
        "Union", "Exactly one", "At least two", "official", "fail-closed",
        'href="/docs"', 'href="/downloads/shariah-list.csv"',
        'href="/downloads/holdings.csv"', "viewport",
    ]:
        assert text in html
    assert '<link rel="stylesheet" href="/assets/site.css">' in html
    assert "<style" not in html
    csp = response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp
    assert "style-src 'self'" in csp

    stylesheet = client.get("/assets/site.css")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ".grid" in stylesheet.text
    assert stylesheet.headers["content-security-policy"] == csp


def test_missing_or_invalid_authoritative_data_returns_safe_503(tmp_path: Path):
    client = TestClient(create_app(data_dir=tmp_path), raise_server_exceptions=False)
    for path in ["/", "/health", "/metadata", "/stocks", "/downloads/holdings.csv"]:
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "Authoritative data is unavailable"}

    (tmp_path / "current.json").write_text(
        '{"generation":"../../etc/passwd","path":"../../etc","files":[]}\n', encoding="utf-8"
    )
    response = client.get("/stocks")
    assert response.status_code == 503
    assert "etc" not in response.text


def test_csv_with_missing_required_cell_returns_safe_503(data_dir: Path):
    generation = generation_dir(data_dir)
    allowlist = generation / "allowlist.csv"
    allowlist.write_text(
        allowlist.read_text(encoding="utf-8").replace("AAPL,Apple Inc,", "AAPL,,"),
        encoding="utf-8",
    )

    client = TestClient(create_app(data_dir=data_dir), raise_server_exceptions=False)
    response = client.get("/stocks", params={"search": "apple"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Authoritative data is unavailable"}


@pytest.mark.parametrize("mutation", [
    "holding_date_mismatch",
    "holding_source_mismatch",
    "javascript_url",
    "nan_metadata",
    "infinity_metadata",
    "null_metadata",
    "malformed_number",
    "negative_number",
    "malformed_ticker",
    "membership_mismatch",
    "union_mismatch",
])
def test_incoherent_or_malformed_generation_returns_safe_503(data_dir: Path, mutation: str):
    generation = generation_dir(data_dir)
    holdings_path = generation / "holdings.csv"
    allowlist_path = generation / "allowlist.csv"
    metadata_path = generation / "metadata.json"
    holdings = holdings_path.read_text(encoding="utf-8")
    allowlist = allowlist_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    if mutation == "holding_date_mismatch":
        holdings = holdings.replace("2026-07-31", "2026-07-30", 1)
    elif mutation == "holding_source_mismatch":
        holdings = holdings.replace("https://official.test/HLAL.csv", "https://other.test/HLAL.csv", 1)
    elif mutation == "javascript_url":
        metadata["funds"][0]["source_url"] = "javascript:alert(1)"
    elif mutation == "nan_metadata":
        raw = metadata_path.read_text(encoding="utf-8").replace(
            '"source_row_count": 2', '"source_row_count": NaN', 1,
        )
        metadata_path.write_text(raw, encoding="utf-8")
        assert_generation_unavailable(data_dir)
        return
    elif mutation == "infinity_metadata":
        raw = metadata_path.read_text(encoding="utf-8").replace(
            '"source_row_count": 2', '"source_row_count": Infinity', 1,
        )
        metadata_path.write_text(raw, encoding="utf-8")
        assert_generation_unavailable(data_dir)
        return
    elif mutation == "null_metadata":
        metadata["funds"][0]["source_row_count"] = None
    elif mutation == "malformed_number":
        holdings = holdings.replace(",40.00,", ",not-a-number,", 1)
    elif mutation == "negative_number":
        holdings = holdings.replace(",40.00,", ",-40.00,", 1)
    elif mutation == "malformed_ticker":
        holdings = holdings.replace(",AAPL,", ",AAP L,")
        allowlist = allowlist.replace("AAPL,", "AAP L,")
    elif mutation == "membership_mismatch":
        allowlist = allowlist.replace("HLAL+MNZL+SPUS", "HLAL+SPUS", 1)
    elif mutation == "union_mismatch":
        metadata["unique_symbols"] += 1

    holdings_path.write_text(holdings, encoding="utf-8")
    allowlist_path.write_text(allowlist, encoding="utf-8")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert_generation_unavailable(data_dir)


def test_each_request_reloads_one_coherent_generation(data_dir: Path):
    client = TestClient(create_app(data_dir=data_dir))
    first = client.get("/stocks/AAPL").json()
    first_generation = first["generation"]
    assert first["name"] == "Apple Inc"

    refresh_to_directory(sources(apple_name="Apple Computer"), data_dir, POLICY, NOW.replace(hour=19))

    second = client.get("/stocks/AAPL").json()
    spus = next(item for item in second["funds"] if item["fund"] == "SPUS")
    assert spus["name"] == "Apple Computer"
    assert second["checked_at"] == "2026-07-31T19:00:00Z"
    assert second["generation"] != first_generation
    download = client.get("/downloads/holdings.csv")
    assert "Apple Computer" in download.text
    assert client.get("/downloads/shariah-list.csv").text.count("2026-07-31T19:00:00Z") == 4


def test_raw_generation_tampering_is_rejected_by_digest(data_dir: Path):
    holdings = generation_dir(data_dir) / "holdings.csv"
    # A blank record leaves parsed data unchanged; raw-byte verification catches it.
    holdings.write_bytes(holdings.read_bytes() + b"\n")
    assert_generation_unavailable(data_dir)


def test_unicode_case_aliases_are_rejected_before_normalization(client: TestClient):
    assert client.get("/funds/%C5%BFpus/holdings").status_code == 404
    assert client.get("/stocks", params={"fund": "ſpus"}).status_code == 422
    # Python uppercases the long-s alias to S; without the ASCII check this is MSFT.
    assert client.get("/stocks/m%C5%BFft").status_code == 404


def test_repository_caches_and_reloads_on_file_or_pointer_change(data_dir: Path):
    repository = HoldingsRepository(data_dir)
    first = repository.load()
    assert repository.load() is first

    holdings = generation_dir(data_dir) / "holdings.csv"
    holdings.write_bytes(holdings.read_bytes() + b"\n")
    with pytest.raises(DataUnavailable):
        repository.load()

    refresh_to_directory(sources(apple_name="Apple Computer"), data_dir, POLICY, NOW.replace(hour=19))
    reloaded = repository.load()
    assert reloaded is not first
    assert reloaded.generation != first.generation
    assert any(row["name"] == "Apple Computer" for row in reloaded.holdings_by_symbol["AAPL"])


@pytest.mark.parametrize("limit_name", ["file", "rows", "field"])
def test_explicit_generation_limits_fail_closed_and_cheaply(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, limit_name: str,
):
    import shariah_holdings.repository as repository_module

    if limit_name == "file":
        limits = dict(repository_module.MAX_FILE_BYTES)
        limits["holdings.csv"] = 1
        monkeypatch.setattr(repository_module, "MAX_FILE_BYTES", limits)
    elif limit_name == "rows":
        limits = dict(repository_module.MAX_ROWS)
        limits["holdings.csv"] = 1
        monkeypatch.setattr(repository_module, "MAX_ROWS", limits)
    else:
        monkeypatch.setattr(repository_module, "MAX_FIELD_LENGTH", 5)
    assert_generation_unavailable(data_dir)


def test_openapi_has_named_response_schema_refs_for_every_json_endpoint(client: TestClient):
    schema = client.get("/openapi.json").json()
    expected = {
        "/health": "Health", "/metadata": "Metadata", "/stocks": "StocksPage",
        "/stocks/{symbol}": "StockDetail", "/funds": "FundsResponse",
        "/funds/{fund}/holdings": "FundHoldingsPage", "/overlap": "OverlapResponse",
    }
    for path, model in expected.items():
        response_schema = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema["$ref"] == f"#/components/schemas/{model}"
        assert model in schema["components"]["schemas"]


def test_vercel_entry_imports_in_isolated_python_without_editable_install():
    root = Path(__file__).parents[1]
    script = (
        "import importlib.util; "
        f"p={str(root / 'api' / 'index.py')!r}; "
        "s=importlib.util.spec_from_file_location('vercel_index',p); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "assert m.app.title == 'Shariah Holdings API'"
    )
    result = subprocess.run(
        [str(root / ".venv" / "bin" / "python"), "-I", "-c", script],
        cwd="/", capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_generation_cache_headers_conditional_requests_and_503_no_store(
    client: TestClient, tmp_path: Path,
):
    for path in ["/health", "/stocks", "/downloads/holdings.csv"]:
        response = client.get(path)
        assert response.headers["etag"].startswith('"')
        assert response.headers["cache-control"] == "public, max-age=300, must-revalidate"
        conditional = client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert conditional.status_code == 304
        assert conditional.content == b""

    unavailable = TestClient(create_app(tmp_path / "missing"), raise_server_exceptions=False).get("/health")
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"


def test_security_headers_and_deliberate_read_only_public_cors(client: TestClient):
    for path in ["/", "/health"]:
        response = client.get(path, headers={"Origin": "https://consumer.example"})
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "access-control-allow-credentials" not in response.headers
    preflight = client.options("/stocks", headers={
        "Origin": "https://consumer.example", "Access-Control-Request-Method": "POST",
    })
    assert preflight.status_code == 400
    rejected_write = client.post("/stocks", headers={"Origin": "https://consumer.example"})
    assert rejected_write.status_code == 405
    assert "access-control-allow-origin" not in rejected_write.headers


def test_docs_csp_allows_swagger_assets_without_weakening_other_paths(client: TestClient):
    for path in ("/docs", "/redoc"):
        docs_csp = client.get(path).headers["content-security-policy"]
        assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in docs_csp
        assert "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in docs_csp
        assert docs_csp.count("style-src") == 1
    for path in ("/", "/health", "/openapi.json"):
        csp = client.get(path).headers["content-security-policy"]
        assert "cdn.jsdelivr.net" not in csp
        assert "object-src 'none'" in csp
