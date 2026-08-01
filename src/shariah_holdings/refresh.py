"""Deterministic parsing, validation, merging, and fail-closed publication."""

from __future__ import annotations

import csv
import errno
import fcntl
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

from .generation import generation_id
from .models import CUSIP_RE, Exclusion, FundData, Holding, TICKER_RE

STANDARD_COLUMNS = {"Date", "Account", "StockTicker", "SecurityName", "Shares", "MarketValue", "Weightings"}
MNZL_COLUMNS = {"TICKER", "NAME", "CUSIP", "SHARES", "% of NET ASSETS"}
MNZL_FILENAME_RE = re.compile(r"^mnzl-official-holdings-(\d{4}-\d{2}-\d{2})\.csv$", re.I)
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_ROWS = 10_000


@dataclass(frozen=True)
class ValidationPolicy:
    max_age_days: int = 7
    max_age_days_by_fund: Mapping[str, int] = field(default_factory=lambda: {"MNZL": 120})
    minimum_holdings: Mapping[str, int] = field(default_factory=lambda: {"SPUS": 150, "HLAL": 150, "MNZL": 200})
    minimum_source_rows: Mapping[str, int] = field(
        default_factory=lambda: {"SPUS": 200, "HLAL": 200, "MNZL": 480})
    minimum_total_weight: Decimal = Decimal("98")
    maximum_total_weight: Decimal = Decimal("102")
    maximum_malformed_rows: Mapping[str, int] = field(
        default_factory=lambda: {"SPUS": 0, "HLAL": 0, "MNZL": 2})
    maximum_malformed_rate: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if self.max_age_days < 0:
            raise ValueError("max_age_days must be non-negative")
        if any(fund not in {"SPUS", "HLAL", "MNZL"} or value < 0
               for fund, value in self.max_age_days_by_fund.items()):
            raise ValueError("max_age_days_by_fund must contain known funds and non-negative values")
        if any(value < 0 for value in self.maximum_malformed_rows.values()):
            raise ValueError("maximum_malformed_rows values must be non-negative")
        if not Decimal("0") <= self.maximum_malformed_rate <= Decimal("1"):
            raise ValueError("maximum_malformed_rate must be between zero and one")


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
    max_age_days = policy.max_age_days_by_fund.get(fund, policy.max_age_days)
    if age > max_age_days:
        raise ValueError(f"{fund}: holdings date {value} is {age} days old")


def _rows(raw_csv: str, required: set[str], fund: str) -> list[dict[str, str]]:
    if len(raw_csv.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"{fund}: source exceeds size limit")
    reader = csv.DictReader(io.StringIO(raw_csv.lstrip("\ufeff")))
    fieldnames = reader.fieldnames or []
    duplicates = sorted({name for name in fieldnames if fieldnames.count(name) > 1})
    if duplicates:
        raise ValueError(f"{fund}: duplicate column(s): {', '.join(duplicates)}")
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"{fund}: missing columns: {', '.join(sorted(missing))}")
    rows = []
    for row in reader:
        if len(rows) >= MAX_SOURCE_ROWS:
            raise ValueError(f"{fund}: source exceeds row limit")
        rows.append(row)
    if not rows:
        raise ValueError(f"{fund}: no data rows")
    if any(None in row for row in rows):
        raise ValueError(f"{fund}: row contains surplus field(s)")
    return rows


def _validate_total_and_count(fund: str, total: Decimal, count: int, policy: ValidationPolicy) -> None:
    if not policy.minimum_total_weight <= total <= policy.maximum_total_weight:
        raise ValueError(f"{fund}: total weight {total}% is outside permitted range")
    minimum = policy.minimum_holdings.get(fund, 1)
    if count < minimum:
        raise ValueError(f"{fund}: only {count} valid equity holdings; expected at least {minimum}")


def _validate_source_count(fund: str, count: int, policy: ValidationPolicy) -> None:
    minimum = policy.minimum_source_rows.get(fund, 1)
    if count < minimum:
        raise ValueError(f"{fund}: only {count} source rows; expected at least {minimum}")


def _validate_malformed_exclusions(fund: str, exclusions: Sequence[Exclusion],
                                   total_rows: int, policy: ValidationPolicy) -> None:
    malformed = sum(item.reason == "malformed_identifier" for item in exclusions)
    maximum = policy.maximum_malformed_rows.get(fund, 0)
    if malformed > maximum:
        raise ValueError(f"{fund}: malformed row budget exceeded: {malformed} > {maximum}")
    rate = Decimal(malformed) / Decimal(total_rows)
    if rate > policy.maximum_malformed_rate:
        raise ValueError(f"{fund}: malformed row rate {rate:.2%} exceeds permitted rate")


def _mnzl_shares(value: str | None) -> Decimal:
    """Parse MNZL integral shares, whose periods are grouping separators."""
    raw = (value or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})*|\d+", raw):
        raise ValueError(f"invalid shares: {value!r}")
    return _decimal(raw.replace(".", ""), "shares")


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

    # Parse every numeric field first, including rows that may later be excluded.
    parsed = [(_decimal(row.get("Weightings"), "weight"),
               _decimal(row.get("Shares"), "shares"),
               _decimal(row.get("MarketValue"), "market value")) for row in rows]
    if any(value < 0 for values in parsed for value in values):
        raise ValueError(f"{fund}: negative weight, shares, or market value")
    total_weight = sum((values[0] for values in parsed), Decimal())
    holdings: list[Holding] = []
    exclusions: list[Exclusion] = []
    seen: set[str] = set()
    for row_number, (row, (weight, shares, market_value)) in enumerate(zip(rows, parsed), 2):
        symbol = (row.get("StockTicker") or "").strip().upper()
        money_market = (row.get("MoneyMarketFlag") or "").strip().upper() == "Y"
        if money_market or symbol in {"CASH", "CASH&OTHER"}:
            exclusions.append(Exclusion(row_number, symbol, "cash_or_money_market"))
            continue
        if not TICKER_RE.fullmatch(symbol):
            if weight > 0:
                raise ValueError(f"{fund}: positive-weight malformed identifier {symbol!r}")
            exclusions.append(Exclusion(row_number, symbol, "malformed_identifier"))
            continue
        if market_value <= 0 or shares <= 0:
            exclusions.append(Exclusion(row_number, symbol, "zero_value_residual"))
            continue
        if symbol in seen:
            raise ValueError(f"{fund}: duplicate ticker {symbol}")
        seen.add(symbol)
        raw_cusip = (row.get("CUSIP") or "").strip().upper()
        cusip = raw_cusip if CUSIP_RE.fullmatch(raw_cusip) else None
        holdings.append(Holding(
            fund=fund, symbol=symbol, name=row.get("SecurityName") or "", cusip=cusip,
            weight=weight, shares=shares, market_value=market_value,
            holdings_date=holdings_date, source_url=source_url,
        ))
    _validate_source_count(fund, len(rows), policy)
    _validate_malformed_exclusions(fund, exclusions, len(rows), policy)
    _validate_total_and_count(fund, total_weight, len(holdings), policy)
    return FundData(fund, holdings_date, tuple(sorted(holdings, key=lambda h: h.symbol)), raw_csv,
                    source_url, len(rows), total_weight, tuple(exclusions))


def parse_mnzl_text(raw_csv: str, filename: str, source_url: str,
                    policy: ValidationPolicy, today: date | None = None) -> FundData:
    match = MNZL_FILENAME_RE.fullmatch(Path(filename).name)
    if not match:
        raise ValueError("MNZL filename must be mnzl-official-holdings-YYYY-MM-DD.csv")
    holdings_date = date.fromisoformat(match.group(1))
    _validate_date("MNZL", holdings_date, policy, today or date.today())
    rows = _rows(raw_csv, MNZL_COLUMNS, "MNZL")
    # Validate all numerics before classification or aggregate checks.
    parsed = [(_decimal(row.get("% of NET ASSETS"), "weight"),
               _mnzl_shares(row.get("SHARES"))) for row in rows]
    if any(value < 0 for values in parsed for value in values):
        raise ValueError("MNZL: negative weight or shares")
    total_weight = sum((values[0] for values in parsed), Decimal())
    holdings: list[Holding] = []
    exclusions: list[Exclusion] = []
    seen: set[str] = set()
    for row_number, (row, (weight, shares)) in enumerate(zip(rows, parsed), 2):
        symbol = (row.get("TICKER") or "").strip().upper()
        cusip = (row.get("CUSIP") or "").strip().upper()
        if symbol in {"CASH", "CASH&OTHER"}:
            exclusions.append(Exclusion(row_number, symbol, "cash_or_money_market"))
            continue
        if not TICKER_RE.fullmatch(symbol) or not CUSIP_RE.fullmatch(cusip):
            if weight > 0:
                raise ValueError(f"MNZL: positive-weight malformed identifier {symbol!r}")
            exclusions.append(Exclusion(row_number, symbol, "malformed_identifier"))
            continue
        if shares <= 0:
            exclusions.append(Exclusion(row_number, symbol, "zero_value_residual"))
            continue
        if symbol in seen:
            raise ValueError(f"MNZL: duplicate ticker {symbol}")
        seen.add(symbol)
        holdings.append(Holding(
            fund="MNZL", symbol=symbol, name=row.get("NAME") or "", cusip=cusip,
            weight=weight, shares=shares, market_value=None,
            holdings_date=holdings_date, source_url=source_url,
        ))
    _validate_source_count("MNZL", len(rows), policy)
    _validate_malformed_exclusions("MNZL", exclusions, len(rows), policy)
    _validate_total_and_count("MNZL", total_weight, len(holdings), policy)
    return FundData("MNZL", holdings_date, tuple(sorted(holdings, key=lambda h: h.symbol)),
                    raw_csv, source_url, len(rows), total_weight, tuple(exclusions))


def parse_mnzl_holdings(path: Path, source_url: str, policy: ValidationPolicy,
                        today: date | None = None) -> FundData:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("MNZL: source exceeds size limit")
    return parse_mnzl_text(path.read_text(encoding="utf-8-sig"), path.name, source_url, policy, today)


def _format_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _csv(fieldnames: Sequence[str], rows: Iterable[Mapping[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: _spreadsheet_safe(value) for key, value in row.items()} for row in rows)
    return output.getvalue()


def _spreadsheet_safe(value: str) -> str:
    """Neutralize formula-like CSV cells without changing normalized models."""
    return "'" + value if value.startswith(("=", "+", "-", "@")) else value


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
        "acquired_at": stamp,
        "funds": [{"fund": fund.fund, "holdings_date": fund.holdings_date.isoformat(),
                   "holding_count": len(fund.holdings),
                   "source_row_count": fund.total_rows,
                   "excluded_row_count": len(fund.exclusions),
                   "exclusions": [
                       {"row_number": item.row_number, "symbol": item.symbol, "reason": item.reason}
                       for item in fund.exclusions
                   ],
                   "source_url": fund.source_url}
                  for fund in ordered],
        "unique_symbols": len(allowlist),
    }
    return {
        "allowlist.csv": _csv(["symbol", "name", "source", "checked_at"], allowlist),
        "holdings.csv": _csv(["fund", "symbol", "name", "cusip", "weight", "shares",
                              "market_value", "holdings_date", "source_url"], details),
        "metadata.json": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    }


def _generation_id(outputs: Mapping[str, str]) -> str:
    return generation_id(outputs)


def load_current_outputs(output_dir: Path) -> dict[str, str]:
    """Read all authoritative files through the single atomic generation pointer."""
    manifest = json.loads((output_dir / "current.json").read_text(encoding="utf-8"))
    expected_files = {"allowlist.csv", "holdings.csv", "metadata.json"}
    if set(manifest.get("files", ())) != expected_files:
        raise ValueError("invalid file set in current.json")
    generation_id = manifest.get("generation")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[0-9a-f]{24}", generation_id):
        raise ValueError("invalid generation ID in current.json")
    if manifest.get("path") != f"generations/{generation_id}":
        raise ValueError("invalid generation path in current.json")
    generation = output_dir / manifest["path"]
    if generation.resolve().parent != (output_dir / "generations").resolve():
        raise ValueError("invalid generation path in current.json")
    return {name: (generation / name).read_text(encoding="utf-8") for name in manifest["files"]}


def _fsync_directory(path: Path) -> None:
    """Persist a rename boundary where the platform supports directory fsync."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _PublicationLock:
    def __init__(self, output_dir: Path):
        self.path = output_dir / ".publish.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        assert self.handle is not None
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def _semantically_equal(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    """Compare normalized positions; timestamps and excluded source rows are not publications."""
    return left.get("holdings.csv") == right.get("holdings.csv")


def _publish_atomically_unlocked(outputs: Mapping[str, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    generations = output_dir / "generations"
    generations.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=generations))
    generation_id = _generation_id(outputs)
    generation = generations / generation_id
    try:
        previous = load_current_outputs(output_dir)
    except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError):
        previous = None
    committed = False
    try:
        for name, content in outputs.items():
            path = staging / name
            path.write_text(content, encoding="utf-8", newline="")
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(staging)
        if not generation.exists():
            try:
                # Generations are immutable. An identical concurrent writer may
                # win the rename race, in which case its complete directory wins.
                os.rename(staging, generation)
                _fsync_directory(generations)
            except OSError as exc:
                # POSIX may report EEXIST or ENOTEMPTY when the target is an
                # already-published directory, depending on the filesystem.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not generation.is_dir():
                    raise
        # Convenience files are non-authoritative, but all of their fallible
        # writes happen before committing the authoritative generation pointer.
        for name, content in outputs.items():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{name}-", suffix=".tmp", dir=output_dir)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, output_dir / name)
            finally:
                temporary.unlink(missing_ok=True)
        _fsync_directory(output_dir)

        manifest = {"generation": generation_id, "path": f"generations/{generation_id}",
                    "files": sorted(outputs)}
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".current-", suffix=".tmp", dir=output_dir)
        pointer = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(manifest, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Commit point: the authoritative pointer changes only after all files are durable.
            os.replace(pointer, output_dir / "current.json")
            committed = True
            _fsync_directory(output_dir)
        except Exception:
            pointer.unlink(missing_ok=True)
            raise
    except Exception:
        if not committed:
            if previous is None:
                for name in outputs:
                    (output_dir / name).unlink(missing_ok=True)
            else:
                for name, content in previous.items():
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{name}-restore-", suffix=".tmp", dir=output_dir)
                    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.rename(temporary_name, output_dir / name)
                _fsync_directory(output_dir)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _publish_atomically(outputs: Mapping[str, str], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with _PublicationLock(output_dir):
        _publish_atomically_unlocked(outputs, output_dir)


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
    output_dir.mkdir(parents=True, exist_ok=True)
    with _PublicationLock(output_dir):
        try:
            current = load_current_outputs(output_dir)
        except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError):
            current = None
        if current is not None and _semantically_equal(outputs, current):
            return current
        _publish_atomically_unlocked(outputs, output_dir)
        return outputs
