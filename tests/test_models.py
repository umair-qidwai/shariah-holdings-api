from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from shariah_holdings.models import FundData, Holding, SourceMetadata


def test_holding_normalizes_and_validates_fields():
    row = Holding(
        fund=" spus ", symbol=" aapl ", name=" Apple Inc ", cusip="037833100",
        weight="13.14%", shares="1,136,719", market_value="$379,016,216.17",
        holdings_date=date(2026, 7, 31), source_url="https://example.test/SPUS.csv",
    )
    assert (row.fund, row.symbol, row.name) == ("SPUS", "AAPL", "Apple Inc")
    assert row.weight == Decimal("13.14")
    assert row.shares == Decimal("1136719")
    assert row.market_value == Decimal("379016216.17")


@pytest.mark.parametrize("changes", [
    {"symbol": "CASH&OTHER"}, {"symbol": "bad ticker"}, {"name": ""},
    {"weight": "-1"}, {"shares": "-1"}, {"market_value": "-1"},
    {"cusip": "short"}, {"source_url": "not-a-url"},
])
def test_holding_rejects_invalid_values(changes):
    values = dict(fund="SPUS", symbol="AAPL", name="Apple", cusip="037833100",
                  weight="1", shares="2", market_value="3",
                  holdings_date=date(2026, 7, 31), source_url="https://example.test/a.csv")
    values.update(changes)
    with pytest.raises(ValueError):
        Holding(**values)


def test_fund_data_rejects_duplicate_symbols_and_mixed_funds():
    def holding(fund="SPUS"):
        return Holding(fund, "AAPL", "Apple", "037833100", "1", "2", "3",
                       date(2026, 7, 31), "https://example.test/a.csv")
    with pytest.raises(ValueError, match="duplicate"):
        FundData("SPUS", date(2026, 7, 31), (holding(), holding()), "raw")
    with pytest.raises(ValueError, match="fund"):
        FundData("SPUS", date(2026, 7, 31), (holding("HLAL"),), "raw")


def test_source_metadata_requires_utc_timestamp_and_url():
    metadata = SourceMetadata("SPUS", "https://example.test/a.csv", date(2026, 7, 31),
                              datetime(2026, 7, 31, 17, tzinfo=timezone.utc), 2)
    assert metadata.fund == "SPUS"
    with pytest.raises(ValueError, match="timezone"):
        SourceMetadata("SPUS", "https://example.test", date(2026, 7, 31),
                       datetime(2026, 7, 31, 17), 2)
