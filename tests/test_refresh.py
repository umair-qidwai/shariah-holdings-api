import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from shariah_holdings.refresh import (ValidationPolicy, build_outputs, load_current_outputs,
                                      parse_mnzl_holdings, parse_standard_holdings,
                                      refresh_to_directory)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 31, 18, tzinfo=timezone.utc)
POLICY = ValidationPolicy(max_age_days=7, minimum_holdings={"SPUS": 2, "HLAL": 2, "MNZL": 2},
                          minimum_source_rows={"SPUS": 1, "HLAL": 1, "MNZL": 1})


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_standard_schema_parses_details_and_excludes_cash_and_zero_value():
    fund = parse_standard_holdings("SPUS", text("SPUS.csv"), "https://official.test/SPUS.csv", POLICY, NOW.date())
    assert [h.symbol for h in fund.holdings] == ["AAPL", "MSFT"]
    apple = fund.holdings[0]
    assert (apple.cusip, str(apple.shares), str(apple.market_value), str(apple.weight)) == (
        "037833100", "100", "20000", "60.00")
    assert fund.holdings_date == date(2026, 7, 31)


def test_mnzl_schema_uses_dated_filename_and_preserves_missing_market_value(tmp_path):
    path = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    path.write_text(text(path.name), encoding="utf-8")
    fund = parse_mnzl_holdings(path, "https://manzilfunds.com/", POLICY, NOW.date())
    assert [h.symbol for h in fund.holdings] == ["AAPL", "AVGO"]
    assert fund.holdings[0].market_value is None
    assert fund.holdings[0].shares == 10


def test_mnzl_shares_use_period_as_thousands_separator_and_zero_weight_is_equity(tmp_path):
    path = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    path.write_text(
        'TICKER,NAME,CUSIP,SHARES,% of NET ASSETS\n'
        'AVGO,Broadcom Inc,11135F101,3.997,99\n'
        'WLK,Westlake Corp,960413102,6,0\n'
        'Cash&Other,Cash & Other,,1.000,1\n', encoding="utf-8")
    fund = parse_mnzl_holdings(path, "https://manzilfunds.com/", POLICY, NOW.date())
    assert [(h.symbol, h.shares) for h in fund.holdings] == [("AVGO", 3997), ("WLK", 6)]
    assert fund.total_rows == 3
    assert [(e.row_number, e.reason) for e in fund.exclusions] == [(4, "cash_or_money_market")]


def test_mnzl_full_official_source_has_487_equities_and_union_531():
    full = FIXTURES / "full"
    policy = ValidationPolicy(max_age_days=7)
    mnzl = parse_mnzl_holdings(full / "mnzl-official-holdings-2026-07-31.csv",
                               "https://manzilfunds.com/", policy, NOW.date())
    zero_symbols = {h.symbol for h in mnzl.holdings if h.weight == 0}
    assert len(mnzl.holdings) == 487
    assert zero_symbols == {"CAI", "DDS", "INGM", "IPGP", "JAN", "LBRDA", "LEN/B",
                            "LLYVA", "MWH", "REYN", "SAIL", "SMMT", "UI", "WLK"}
    assert [(e.symbol, e.reason) for e in mnzl.exclusions] == [
        ("CASH&OTHER", "cash_or_money_market"),
        ("2602335D", "malformed_identifier"),
        ("2645536D", "malformed_identifier"),
    ]
    funds = [mnzl] + [
        parse_standard_holdings(fund, text(f"full/{fund}.csv"),
                                f"https://official.test/{fund}.csv", policy, NOW.date())
        for fund in ("SPUS", "HLAL")
    ]
    assert {fund.fund: len(fund.holdings) for fund in funds} == {
        "SPUS": 216, "HLAL": 211, "MNZL": 487}
    outputs = build_outputs(funds, NOW)
    assert len(list(csv.DictReader(io.StringIO(outputs["allowlist.csv"])))) == 531
    metadata = json.loads(outputs["metadata.json"])
    assert metadata["acquired_at"] == "2026-07-31T18:00:00Z"
    assert {item["fund"]: (item["source_row_count"], item["holding_count"],
                           item["excluded_row_count"])
            for item in metadata["funds"]} == {
                "SPUS": (219, 216, 3), "HLAL": (213, 211, 2), "MNZL": (490, 487, 3)}


def test_completeness_rejects_truncation_and_invalid_excluded_row_numeric(tmp_path):
    full_path = FIXTURES / "full" / "mnzl-official-holdings-2026-07-31.csv"
    truncated = tmp_path / full_path.name
    truncated.write_text("\n".join(full_path.read_text().splitlines()[:450]) + "\n")
    with pytest.raises(ValueError, match="source rows|equity holdings"):
        parse_mnzl_holdings(truncated, "https://manzilfunds.com/", ValidationPolicy(), NOW.date())

    malformed_numeric = text("mnzl-official-holdings-2026-07-31.csv").replace(
        "CASH&OTHER,Cash,,1,10.00", "CASH&OTHER,Cash,,not-a-number,10.00")
    bad = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    bad.write_text(malformed_numeric)
    with pytest.raises(ValueError, match="shares"):
        parse_mnzl_holdings(bad, "https://manzilfunds.com/", POLICY, NOW.date())


def test_rejects_bad_schema_stale_date_weight_total_and_duplicates():
    raw = text("SPUS.csv")
    cases = [
        (raw.replace("SecurityName,", "WrongName,"), "missing columns"),
        (raw.replace("07/31/2026", "07/01/2026"), "old"),
        (raw.replace("60.00%", "6.00%"), "total weight"),
        (raw.replace("MSFT,594918104,Microsoft Corp", "AAPL,594918104,Microsoft Corp"), "duplicate"),

    ]
    for candidate, message in cases:
        with pytest.raises(ValueError, match=message):
            parse_standard_holdings("SPUS", candidate, "https://official.test/SPUS.csv", POLICY, NOW.date())


def test_malformed_positive_identifier_is_an_explicit_exclusion():
    raw = text("SPUS.csv").replace("MSFT,594918104", "BAD TICKER,594918104")
    one_ok = ValidationPolicy(max_age_days=7, minimum_holdings={"SPUS": 1},
                              minimum_source_rows={"SPUS": 1})
    fund = parse_standard_holdings("SPUS", raw, "https://official.test/SPUS.csv", one_ok, NOW.date())
    assert [(item.symbol, item.reason) for item in fund.exclusions if item.symbol == "BAD TICKER"] == [
        ("BAD TICKER", "malformed_identifier")]


def test_build_outputs_is_deterministic_exact_union_and_has_one_detail_row_per_fund_ticker(tmp_path):
    mnzl = tmp_path / "mnzl-official-holdings-2026-07-31.csv"
    mnzl.write_text(text(mnzl.name), encoding="utf-8")
    funds = [
        parse_standard_holdings("HLAL", text("HLAL.csv"), "https://official.test/HLAL.csv", POLICY, NOW.date()),
        parse_mnzl_holdings(mnzl, "https://manzilfunds.com/", POLICY, NOW.date()),
        parse_standard_holdings("SPUS", text("SPUS.csv"), "https://official.test/SPUS.csv", POLICY, NOW.date()),
    ]
    outputs = build_outputs(funds, NOW)
    allow = list(csv.DictReader(io.StringIO(outputs["allowlist.csv"])))
    detail = list(csv.DictReader(io.StringIO(outputs["holdings.csv"])))
    assert [r["symbol"] for r in allow] == ["AAPL", "AVGO", "MSFT", "TSLA"]
    assert allow[0] == {"symbol": "AAPL", "name": "Apple Inc", "source": "HLAL+MNZL+SPUS",
                         "checked_at": "2026-07-31T18:00:00Z"}
    assert [(r["fund"], r["symbol"]) for r in detail] == [
        ("HLAL", "AAPL"), ("HLAL", "TSLA"), ("MNZL", "AAPL"),
        ("MNZL", "AVGO"), ("SPUS", "AAPL"), ("SPUS", "MSFT")]
    assert set(detail[0]) == {"fund", "symbol", "name", "cusip", "weight", "shares",
                              "market_value", "holdings_date", "source_url"}
    assert outputs == build_outputs(reversed(funds), NOW)


def test_refresh_validates_every_source_before_atomic_output_replacement(tmp_path):
    old = tmp_path / "allowlist.csv"
    old.write_text("old-data", encoding="utf-8")
    good = {
        "SPUS": (text("SPUS.csv"), "https://official.test/SPUS.csv"),
        "HLAL": (text("HLAL.csv"), "https://official.test/HLAL.csv"),
        "MNZL": (text("mnzl-official-holdings-2026-07-31.csv"), "https://manzilfunds.com/",
                 "mnzl-official-holdings-2026-07-31.csv"),
    }
    broken = dict(good)
    broken["HLAL"] = ("bad,csv\n", "https://official.test/HLAL.csv")
    with pytest.raises(ValueError):
        refresh_to_directory(broken, tmp_path, POLICY, NOW)
    assert old.read_text() == "old-data"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["allowlist.csv"]
    refresh_to_directory(good, tmp_path, POLICY, NOW)
    assert old.read_text().startswith("symbol,name,source,checked_at\n")
    assert (tmp_path / "holdings.csv").exists()
    assert (tmp_path / "metadata.json").exists()


def test_authoritative_manifest_switch_exposes_one_immutable_generation(tmp_path, monkeypatch):
    good = {
        "SPUS": (text("SPUS.csv"), "https://official.test/SPUS.csv"),
        "HLAL": (text("HLAL.csv"), "https://official.test/HLAL.csv"),
        "MNZL": (text("mnzl-official-holdings-2026-07-31.csv"), "https://manzilfunds.com/",
                 "mnzl-official-holdings-2026-07-31.csv"),
    }
    refresh_to_directory(good, tmp_path, POLICY, NOW)
    before = load_current_outputs(tmp_path)
    original_replace = __import__("os").replace

    def fail_pointer(source, target):
        if Path(target).name == "current.json":
            raise OSError("injected pointer failure")
        return original_replace(source, target)

    changed = dict(good)
    changed["SPUS"] = (good["SPUS"][0].replace("Apple Inc", "Apple Incorporated"), good["SPUS"][1])
    monkeypatch.setattr("shariah_holdings.refresh.os.replace", fail_pointer)
    with pytest.raises(OSError, match="pointer failure"):
        refresh_to_directory(changed, tmp_path, POLICY, NOW.replace(hour=19))
    assert load_current_outputs(tmp_path) == before
    manifest = __import__("json").loads((tmp_path / "current.json").read_text())
    generation = tmp_path / manifest["path"]
    assert generation.is_dir()
    assert set(p.name for p in generation.iterdir()) == {"allowlist.csv", "holdings.csv", "metadata.json"}


def test_concurrent_generations_are_each_complete_and_pointer_is_coherent(tmp_path):
    base = {
        "SPUS": (text("SPUS.csv"), "https://official.test/SPUS.csv"),
        "HLAL": (text("HLAL.csv"), "https://official.test/HLAL.csv"),
        "MNZL": (text("mnzl-official-holdings-2026-07-31.csv"), "https://manzilfunds.com/",
                 "mnzl-official-holdings-2026-07-31.csv"),
    }
    variants = []
    for hour, name in [(18, "Apple Incorporated"), (19, "Apple Computer")]:
        sources = dict(base)
        sources["SPUS"] = (base["SPUS"][0].replace("Apple Inc", name), base["SPUS"][1])
        variants.append((sources, NOW.replace(hour=hour)))

    with ThreadPoolExecutor(max_workers=4) as pool:
        # Duplicate each variant to exercise both distinct and identical generation races.
        concurrent_variants = variants + variants
        results = list(pool.map(
            lambda item: refresh_to_directory(item[0], tmp_path, POLICY, item[1]),
            concurrent_variants))

    current = load_current_outputs(tmp_path)
    assert current in results
    generations = [path for path in (tmp_path / "generations").iterdir() if path.is_dir()]
    assert len(generations) == 2
    assert all({item.name for item in generation.iterdir()} ==
               {"allowlist.csv", "holdings.csv", "metadata.json"} for generation in generations)
