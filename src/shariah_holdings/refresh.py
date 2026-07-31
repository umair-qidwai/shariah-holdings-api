"""Deterministic parsing, validation, merging, and fail-closed publication."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import CUSIP_RE, FundData, Holding, TICKER_RE

STANDARD_COLUMNS = {"Date", "Account", "StockTicker", "SecurityName", "Shares", "MarketValue", "Weightings"}
MNZL_COLUMNS = {"TICKER", "NAME", "CUSIP", "SHARES", "% of NET ASSETS"}
MNZL_FILENAME_RE = re.compile(r"^mnzl-official-holdings-(\d{4}-\d{2}-\d{2})\.csv$", re.I)


@dataclass(frozen=True)
class ValidationPolicy:
    max_age_days: int = 7
    minimum_holdings: Mapping[str, int] = field(default_factory=lambda: {"SPUS": 150, "HLAL": 150, "MNZL": 200})
    minimum_total_weight: Decimal = Decimal("98")
    maximum_total_weight: Decimal = Decimal("102")

    def __post_init__(self) -> None:
        if self.max_age_days < 0:
            raise ValueError("max_age_days must be non-negative")


def _decimal(value: str | None, field_name: str) -> Decimal:
    cleaned = (value or "").strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"invalid {field_name}: {value!r}")
    return result


def _validate_date(fund: str, value: date, policy: ValidationPolicy, today: date) -> None:
    age = (today - value).days
    if age < 0:
        raise ValueError(f"{fund}: holdings date {value} is in the future")
    if age > policy.max_age_days:
        raise ValueError(f"{fund}: holdings date {value} is {age} days old")


def _rows(raw_csv: str, required: set[str], fund: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw_csv.lstrip("\ufeff")))
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"{fund}: missing columns: {', '.join(sorted(missing))}")
    rows = list(reader)
    if not rows:
        raise ValueError(f"{fund}: no data rows")
    return rows


def _validate_total_and_count(fund: str, total: Decimal, count: int, policy: ValidationPolicy) -> None:
    if not policy.minimum_total_weight <= total <= policy.maximum_total_weight:
        raise ValueError(f"{fund}: total weight {total}% is outside permitted range")
    minimum = policy.minimum_holdings.get(fund, 1)
    if count < minimum:
        raise ValueError(f"{fund}: only {count} valid equity holdings; expected at least {minimum}")


def parse_standard_holdings(fund: str, raw_csv: str, source_url: str,
                            policy: ValidationPolicy, today: date | None = None) -> FundData:
    fund = fund.strip().upper()
    if fund not in {"SPUS", "HLAL"}:
        raise ValueError("standard parser only supports SPUS and HLAL")
    rows = _rows(raw_csv, STANDARD_COLUMNS, fund)
    accounts = {(row.get("Account") or "").strip().upper() for row in rows}
    if accounts != {fund}:
        raise ValueError(f"{fund}: unexpected Account value(s): {sorted(accounts)}")
    date_values = {(row.get("Date") or "").strip() for row in rows}
    if len(date_values) != 1:
        raise ValueError(f"{fund}: expected exactly one holdings date")
    try:
        holdings_date = datetime.strptime(next(iter(date_values)), "%m/%d/%Y").date()
    except ValueError as exc:
        raise ValueError(f"{fund}: invalid holdings date") from exc
    _validate_date(fund, holdings_date, policy, today or date.today())

    total_weight = sum((_decimal(row.get("Weightings"), "weight") for row in rows), Decimal())
    holdings: list[Holding] = []
    seen: set[str] = set()
    for row in rows:
        symbol = (row.get("StockTicker") or "").strip().upper()
        market_value = _decimal(row.get("MarketValue"), "market value")
        money_market = (row.get("MoneyMarketFlag") or "").strip().upper() == "Y"
        if market_value <= 0 or money_market or symbol in {"CASH", "CASH&OTHER"}:
            continue
        if not TICKER_RE.fullmatch(symbol):
            raise ValueError(f"{fund}: invalid positive-value ticker {symbol!r}")
        if symbol in seen:
            raise ValueError(f"{fund}: duplicate ticker {symbol}")
        seen.add(symbol)
        raw_cusip = (row.get("CUSIP") or "").strip().upper()
        cusip = raw_cusip if CUSIP_RE.fullmatch(raw_cusip) else None
        holdings.append(Holding(
            fund=fund, symbol=symbol, name=row.get("SecurityName") or "", cusip=cusip,
            weight=row.get("Weightings") or "", shares=row.get("Shares"),
            market_value=row.get("MarketValue"), holdings_date=holdings_date, source_url=source_url,
        ))
    _validate_total_and_count(fund, total_weight, len(holdings), policy)
    return FundData(fund, holdings_date, tuple(sorted(holdings, key=lambda h: h.symbol)), raw_csv,
                    source_url, len(rows), total_weight)


def parse_mnzl_text(raw_csv: str, filename: str, source_url: str,
                    policy: ValidationPolicy, today: date | None = None) -> FundData:
    match = MNZL_FILENAME_RE.fullmatch(Path(filename).name)
    if not match:
        raise ValueError("MNZL filename must be mnzl-official-holdings-YYYY-MM-DD.csv")
    holdings_date = date.fromisoformat(match.group(1))
    _validate_date("MNZL", holdings_date, policy, today or date.today())
    rows = _rows(raw_csv, MNZL_COLUMNS, "MNZL")
    total_weight = sum((_decimal(row.get("% of NET ASSETS"), "weight") for row in rows), Decimal())
    holdings: list[Holding] = []
    seen: set[str] = set()
    for row in rows:
        symbol = (row.get("TICKER") or "").strip().upper()
        weight = _decimal(row.get("% of NET ASSETS"), "weight")
        cusip = (row.get("CUSIP") or "").strip().upper()
        if symbol in {"CASH", "CASH&OTHER"} or weight <= 0:
            continue
        if not TICKER_RE.fullmatch(symbol):
            raise ValueError(f"MNZL: invalid equity ticker {symbol!r}")
        if not CUSIP_RE.fullmatch(cusip):
            raise ValueError(f"MNZL: {symbol!r} has invalid CUSIP {cusip!r}")
        if symbol in seen:
            raise ValueError(f"MNZL: duplicate ticker {symbol}")
        seen.add(symbol)
        holdings.append(Holding(
            fund="MNZL", symbol=symbol, name=row.get("NAME") or "", cusip=cusip,
            weight=row.get("% of NET ASSETS") or "", shares=row.get("SHARES"),
            market_value=None, holdings_date=holdings_date, source_url=source_url,
        ))
    _validate_total_and_count("MNZL", total_weight, len(holdings), policy)
    return FundData("MNZL", holdings_date, tuple(sorted(holdings, key=lambda h: h.symbol)),
                    raw_csv, source_url, len(rows), total_weight)


def parse_mnzl_holdings(path: Path, source_url: str, policy: ValidationPolicy,
                        today: date | None = None) -> FundData:
    return parse_mnzl_text(path.read_text(encoding="utf-8-sig"), path.name, source_url, policy, today)


def _format_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _csv(fieldnames: Sequence[str], rows: Iterable[Mapping[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_outputs(funds: Iterable[FundData], checked_at: datetime) -> dict[str, str]:
    ordered = sorted(funds, key=lambda item: item.fund)
    if [fund.fund for fund in ordered] != ["HLAL", "MNZL", "SPUS"]:
        raise ValueError("exactly one validated SPUS, HLAL, and MNZL dataset is required")
    all_holdings: dict[str, list[Holding]] = {}
    details: list[dict[str, str]] = []
    for fund in ordered:
        for holding in fund.holdings:
            all_holdings.setdefault(holding.symbol, []).append(holding)
            details.append({
                "fund": holding.fund, "symbol": holding.symbol, "name": holding.name,
                "cusip": holding.cusip or "", "weight": _format_decimal(holding.weight),
                "shares": _format_decimal(holding.shares),
                "market_value": _format_decimal(holding.market_value),
                "holdings_date": holding.holdings_date.isoformat(), "source_url": holding.source_url,
            })
    stamp = _timestamp(checked_at)
    allowlist = []
    for symbol in sorted(all_holdings):
        members = all_holdings[symbol]
        # Prefer the shortest non-empty issuer name; stable across input ordering.
        name = min((item.name for item in members), key=lambda value: (len(value), value.casefold()))
        allowlist.append({"symbol": symbol, "name": name,
                          "source": "+".join(sorted(item.fund for item in members)),
                          "checked_at": stamp})
    metadata = {
        "checked_at": stamp,
        "funds": [{"fund": fund.fund, "holdings_date": fund.holdings_date.isoformat(),
                   "holding_count": len(fund.holdings), "source_url": fund.source_url}
                  for fund in ordered],
        "unique_symbols": len(allowlist),
    }
    return {
        "allowlist.csv": _csv(["symbol", "name", "source", "checked_at"], allowlist),
        "holdings.csv": _csv(["fund", "symbol", "name", "cusip", "weight", "shares",
                              "market_value", "holdings_date", "source_url"], details),
        "metadata.json": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    }


def _publish_atomically(outputs: Mapping[str, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".refresh-", dir=output_dir))
    backups: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for name, content in outputs.items():
            path = staging / name
            path.write_text(content, encoding="utf-8", newline="")
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        for name in sorted(outputs):
            target = output_dir / name
            backups[target] = target.read_bytes() if target.exists() else None
            os.replace(staging / name, target)
            replaced.append(target)
    except Exception:
        for target in reversed(replaced):
            previous = backups[target]
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(previous)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def refresh_to_directory(sources: Mapping[str, tuple[str, ...]], output_dir: Path,
                         policy: ValidationPolicy | None = None,
                         checked_at: datetime | None = None) -> dict[str, str]:
    policy = policy or ValidationPolicy()
    checked_at = checked_at or datetime.now(timezone.utc)
    if set(sources) != {"SPUS", "HLAL", "MNZL"}:
        raise ValueError("sources must contain exactly SPUS, HLAL, and MNZL")
    funds = [
        parse_standard_holdings("SPUS", sources["SPUS"][0], sources["SPUS"][1], policy, checked_at.date()),
        parse_standard_holdings("HLAL", sources["HLAL"][0], sources["HLAL"][1], policy, checked_at.date()),
        parse_mnzl_text(sources["MNZL"][0], sources["MNZL"][2], sources["MNZL"][1], policy,
                        checked_at.date()),
    ]
    outputs = build_outputs(funds, checked_at)
    _publish_atomically(outputs, output_dir)
    return outputs
