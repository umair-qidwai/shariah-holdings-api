"""Validated normalized domain models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-/]{0,14}$")
CUSIP_RE = re.compile(r"^[A-Z0-9]{9}$")


def _text(value: str, field: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} is required")
    return result


def decimal_value(value: Decimal | str | int | float | None, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise ValueError(f"{field} is required")
    cleaned = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return result


def validate_url(value: str) -> str:
    value = _text(value, "source_url")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source_url must be an HTTP(S) URL")
    return value


@dataclass(frozen=True)
class Holding:
    fund: str
    symbol: str
    name: str
    cusip: str | None
    weight: Decimal | str
    shares: Decimal | str | None
    market_value: Decimal | str | None
    holdings_date: date
    source_url: str

    def __post_init__(self) -> None:
        fund = _text(self.fund, "fund").upper()
        symbol = _text(self.symbol, "symbol").upper()
        name = _text(self.name, "name")
        if not TICKER_RE.fullmatch(symbol) or symbol in {"CASH", "CASH&OTHER"}:
            raise ValueError(f"invalid equity ticker {symbol!r}")
        cusip = self.cusip.strip().upper() if self.cusip else None
        if cusip is not None and not CUSIP_RE.fullmatch(cusip):
            raise ValueError(f"invalid CUSIP {cusip!r}")
        if not isinstance(self.holdings_date, date):
            raise ValueError("holdings_date must be a date")
        object.__setattr__(self, "fund", fund)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "cusip", cusip)
        object.__setattr__(self, "weight", decimal_value(self.weight, "weight"))
        object.__setattr__(self, "shares", decimal_value(self.shares, "shares", optional=True))
        object.__setattr__(self, "market_value", decimal_value(self.market_value, "market_value", optional=True))
        object.__setattr__(self, "source_url", validate_url(self.source_url))


@dataclass(frozen=True)
class FundData:
    fund: str
    holdings_date: date
    holdings: tuple[Holding, ...]
    raw_csv: str
    source_url: str = "https://example.invalid/"
    total_rows: int = 0
    total_weight: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        fund = _text(self.fund, "fund").upper()
        holdings = tuple(self.holdings)
        symbols = [holding.symbol for holding in holdings]
        if any(holding.fund != fund for holding in holdings):
            raise ValueError("holding fund does not match FundData fund")
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"{fund}: duplicate ticker")
        if any(holding.holdings_date != self.holdings_date for holding in holdings):
            raise ValueError(f"{fund}: mixed holdings dates")
        object.__setattr__(self, "fund", fund)
        object.__setattr__(self, "holdings", holdings)


@dataclass(frozen=True)
class SourceMetadata:
    fund: str
    source_url: str
    holdings_date: date
    checked_at: datetime
    holding_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "fund", _text(self.fund, "fund").upper())
        object.__setattr__(self, "source_url", validate_url(self.source_url))
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if self.holding_count < 0:
            raise ValueError("holding_count must be non-negative")
