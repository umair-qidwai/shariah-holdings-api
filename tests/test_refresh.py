import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from shariah_holdings.refresh import (ValidationPolicy, build_outputs, parse_mnzl_holdings,
                                      parse_standard_holdings, refresh_to_directory)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 31, 18, tzinfo=timezone.utc)
POLICY = ValidationPolicy(max_age_days=7, minimum_holdings={"SPUS": 2, "HLAL": 2, "MNZL": 2})


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


def test_rejects_bad_schema_stale_date_weight_total_duplicates_and_bad_ticker():
    raw = text("SPUS.csv")
    cases = [
        (raw.replace("SecurityName,", "WrongName,"), "missing columns"),
        (raw.replace("07/31/2026", "07/01/2026"), "old"),
        (raw.replace("60.00%", "6.00%"), "total weight"),
        (raw.replace("MSFT,594918104,Microsoft Corp", "AAPL,594918104,Microsoft Corp"), "duplicate"),
        (raw.replace("MSFT,594918104", "BAD TICKER,594918104"), "ticker"),
    ]
    for candidate, message in cases:
        with pytest.raises(ValueError, match=message):
            parse_standard_holdings("SPUS", candidate, "https://official.test/SPUS.csv", POLICY, NOW.date())


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
