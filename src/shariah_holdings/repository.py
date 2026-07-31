"""Read-only access to one authoritative immutable data generation."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .generation import generation_id as compute_generation_id
from .models import CUSIP_RE, TICKER_RE

EXPECTED_FILES = {"allowlist.csv", "holdings.csv", "metadata.json"}
FUNDS = ("HLAL", "MNZL", "SPUS")
GENERATION_RE = re.compile(r"^[0-9a-f]{24}$")
ALLOWLIST_FIELDS = ("symbol", "name", "source", "checked_at")
HOLDING_FIELDS = (
    "fund", "symbol", "name", "cusip", "weight", "shares", "market_value",
    "holdings_date", "source_url",
)
OPTIONAL_HOLDING_FIELDS = {"cusip", "market_value"}
METADATA_FIELDS = {"acquired_at", "checked_at", "funds", "unique_symbols"}
FUND_METADATA_FIELDS = {
    "excluded_row_count", "exclusions", "fund", "holding_count", "holdings_date",
    "source_row_count", "source_url",
}
EXCLUSION_FIELDS = {"reason", "row_number", "symbol"}
EXCLUSION_REASONS = {"cash_or_money_market", "zero_value_residual", "malformed_identifier"}
MAX_MANIFEST_BYTES = 4096
MAX_FILE_BYTES = {"allowlist.csv": 5_000_000, "holdings.csv": 25_000_000, "metadata.json": 5_000_000}
MAX_ROWS = {"allowlist.csv": 100_000, "holdings.csv": 300_000}
MAX_FIELD_LENGTH = 4096
MAX_JSON_ITEMS = 300_000


class DataUnavailable(RuntimeError):
    """The authoritative pointer or generation cannot be safely loaded."""


@dataclass(frozen=True)
class Snapshot:
    generation: str
    allowlist: tuple[Mapping[str, str], ...]
    holdings: tuple[Mapping[str, str], ...]
    metadata: Mapping[str, Any]
    allowlist_csv: str
    holdings_csv: str
    holdings_by_symbol: Mapping[str, tuple[Mapping[str, str], ...]]
    holdings_by_fund: Mapping[str, tuple[Mapping[str, str], ...]]
    allowlist_by_symbol: Mapping[str, Mapping[str, str]]

    @property
    def checked_at(self) -> str:
        return str(self.metadata["checked_at"])


class HoldingsRepository:
    """Cache a validated snapshot until the pointer or a generation file changes."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self._cache: tuple[tuple[Any, ...], Snapshot] | None = None
        self._lock = threading.Lock()

    def load(self) -> Snapshot:
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> Snapshot:
        try:
            pointer = self.data_dir / "current.json"
            pointer_stat = pointer.stat()
            if pointer_stat.st_size > MAX_MANIFEST_BYTES:
                raise ValueError("manifest is too large")
            pointer_raw = pointer.read_bytes()
            if len(pointer_raw) > MAX_MANIFEST_BYTES:
                raise ValueError("manifest is too large")
            manifest = self._read_json(pointer_raw.decode("utf-8"))
            if not isinstance(manifest, dict) or set(manifest) != {"generation", "path", "files"}:
                raise ValueError("invalid manifest")
            generation_id = manifest.get("generation")
            if not isinstance(generation_id, str) or not GENERATION_RE.fullmatch(generation_id):
                raise ValueError("invalid generation")
            if manifest.get("path") != f"generations/{generation_id}":
                raise ValueError("invalid generation path")
            files = manifest.get("files")
            if not isinstance(files, list) or files != sorted(EXPECTED_FILES):
                raise ValueError("invalid generation files")

            generations = (self.data_dir / "generations").resolve()
            generation = self.data_dir / "generations" / generation_id
            if generation.resolve().parent != generations or not generation.is_dir():
                raise ValueError("invalid generation directory")
            paths: dict[str, Path] = {}
            file_identity: list[tuple[str, int, int, int, int]] = []
            for name in sorted(EXPECTED_FILES):
                path = generation / name
                if path.resolve().parent != generation.resolve() or not path.is_file():
                    raise ValueError("invalid generation file")
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES[name]:
                    raise ValueError("generation file is too large")
                paths[name] = path
                file_identity.append(
                    (name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                )

            identity = (
                pointer_stat.st_size,
                pointer_stat.st_mtime_ns,
                pointer_stat.st_ctime_ns,
                pointer_stat.st_ino,
                generation_id,
                *file_identity,
            )
            if self._cache is not None and self._cache[0] == identity:
                return self._cache[1]

            raw_outputs: dict[str, bytes] = {}
            outputs: dict[str, str] = {}
            for name in sorted(EXPECTED_FILES):
                raw = paths[name].read_bytes()
                if len(raw) > MAX_FILE_BYTES[name]:
                    raise ValueError("generation file is too large")
                raw_outputs[name] = raw
                outputs[name] = raw.decode("utf-8")
            if compute_generation_id(raw_outputs) != generation_id:
                raise ValueError("generation digest mismatch")

            allowlist = self._read_csv(outputs["allowlist.csv"], ALLOWLIST_FIELDS)
            holdings = self._read_csv(outputs["holdings.csv"], HOLDING_FIELDS)
            metadata = self._read_json(outputs["metadata.json"])
            self._validate(allowlist, holdings, metadata)
            frozen_allowlist = tuple(MappingProxyType(row) for row in allowlist)
            frozen_holdings = tuple(MappingProxyType(row) for row in holdings)
            by_symbol: dict[str, list[Mapping[str, str]]] = {}
            by_fund: dict[str, list[Mapping[str, str]]] = {fund: [] for fund in FUNDS}
            for row in frozen_holdings:
                by_symbol.setdefault(row["symbol"], []).append(row)
                by_fund[row["fund"]].append(row)
            snapshot = Snapshot(
                generation=generation_id,
                allowlist=frozen_allowlist,
                holdings=frozen_holdings,
                metadata=self._freeze(metadata),
                allowlist_csv=outputs["allowlist.csv"],
                holdings_csv=outputs["holdings.csv"],
                holdings_by_symbol=MappingProxyType({key: tuple(value) for key, value in by_symbol.items()}),
                holdings_by_fund=MappingProxyType({key: tuple(value) for key, value in by_fund.items()}),
                allowlist_by_symbol=MappingProxyType({row["symbol"]: row for row in frozen_allowlist}),
            )
            self._cache = (identity, snapshot)
            return snapshot
        except (
            OSError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError,
            json.JSONDecodeError, csv.Error, KeyError,
        ) as exc:
            raise DataUnavailable("authoritative data is unavailable") from exc

    @staticmethod
    def _freeze(value: Any) -> Any:
        if isinstance(value, dict):
            return MappingProxyType({
                key: HoldingsRepository._freeze(item) for key, item in value.items()
            })
        if isinstance(value, list):
            return tuple(HoldingsRepository._freeze(item) for item in value)
        return value

    @staticmethod
    def _read_json(raw: str) -> Any:
        def reject_constant(value: str) -> None:
            raise ValueError(f"invalid JSON numeric constant: {value}")

        value = json.loads(raw, parse_constant=reject_constant)

        seen_items = 0

        def validate_json(item: Any) -> None:
            nonlocal seen_items
            seen_items += 1
            if seen_items > MAX_JSON_ITEMS:
                raise ValueError("too many JSON values")
            if isinstance(item, str):
                if len(item) > MAX_FIELD_LENGTH:
                    raise ValueError("JSON value is too long")
                return
            if item is None or isinstance(item, (bool, int)):
                return
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ValueError("non-finite JSON number")
                return
            if isinstance(item, list):
                for child in item:
                    validate_json(child)
                return
            if isinstance(item, dict) and all(
                isinstance(key, str) and len(key) <= MAX_FIELD_LENGTH for key in item
            ):
                for child in item.values():
                    validate_json(child)
                return
            raise ValueError("invalid JSON type")

        validate_json(value)
        return value

    @staticmethod
    def _read_csv(raw: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(raw))
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError("unexpected CSV schema")
        limit = MAX_ROWS["holdings.csv" if fields == HOLDING_FIELDS else "allowlist.csv"]
        rows = []
        for row in reader:
            if len(rows) >= limit:
                raise ValueError("too many CSV rows")
            if any(value is not None and len(value) > MAX_FIELD_LENGTH for value in row.values()):
                raise ValueError("CSV field is too long")
            rows.append(row)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("malformed CSV row")
        required = set(fields) - (OPTIONAL_HOLDING_FIELDS if fields == HOLDING_FIELDS else set())
        if any(any(not row[field].strip() for field in required) for row in rows):
            raise ValueError("missing required CSV value")
        return rows

    @staticmethod
    def _validate(
        allowlist: list[dict[str, str]], holdings: list[dict[str, str]], metadata: dict[str, Any]
    ) -> None:
        if not isinstance(metadata, dict) or set(metadata) != METADATA_FIELDS:
            raise ValueError("invalid metadata")
        if not HoldingsRepository._is_timestamp(metadata["checked_at"]):
            raise ValueError("invalid checked_at")
        if not HoldingsRepository._is_timestamp(metadata["acquired_at"]):
            raise ValueError("invalid acquired_at")
        if not HoldingsRepository._is_nonnegative_int(metadata["unique_symbols"]):
            raise ValueError("invalid unique symbol count")
        funds = metadata.get("funds")
        if (
            not isinstance(funds, list)
            or len(funds) != len(FUNDS)
            or any(not isinstance(item, dict) or set(item) != FUND_METADATA_FIELDS for item in funds)
            or {item["fund"] for item in funds} != set(FUNDS)
        ):
            raise ValueError("invalid fund metadata")
        for item in funds:
            if (
                not HoldingsRepository._is_date(item["holdings_date"])
                or not HoldingsRepository._is_http_url(item["source_url"])
                or not HoldingsRepository._is_nonnegative_int(item["holding_count"])
                or not HoldingsRepository._is_nonnegative_int(item["source_row_count"])
                or not HoldingsRepository._is_nonnegative_int(item["excluded_row_count"])
                or not isinstance(item["exclusions"], list)
                or item["excluded_row_count"] != len(item["exclusions"])
                or item["source_row_count"] != item["holding_count"] + item["excluded_row_count"]
            ):
                raise ValueError("invalid fund metadata values")
            for exclusion in item["exclusions"]:
                if (
                    not isinstance(exclusion, dict)
                    or set(exclusion) != EXCLUSION_FIELDS
                    or not isinstance(exclusion["row_number"], int)
                    or isinstance(exclusion["row_number"], bool)
                    or exclusion["row_number"] < 2
                    or not HoldingsRepository._is_text(exclusion["symbol"])
                    or exclusion["reason"] not in EXCLUSION_REASONS
                ):
                    raise ValueError("invalid exclusion metadata")

        for row in allowlist:
            if (
                not TICKER_RE.fullmatch(row["symbol"])
                or row["symbol"] != row["symbol"].upper()
                or not HoldingsRepository._is_text(row["name"])
                or not HoldingsRepository._is_text(row["source"])
            ):
                raise ValueError("invalid allowlist value")

        fund_metadata = {item["fund"]: item for item in funds}
        for row in holdings:
            if (
                row["fund"] not in FUNDS
                or not TICKER_RE.fullmatch(row["symbol"])
                or row["symbol"] != row["symbol"].upper()
                or not HoldingsRepository._is_text(row["name"])
                or (row["cusip"] != "" and not CUSIP_RE.fullmatch(row["cusip"]))
                or not HoldingsRepository._is_number(row["weight"])
                or not HoldingsRepository._is_number(row["shares"])
                or (row["market_value"] != "" and not HoldingsRepository._is_number(row["market_value"]))
                or not HoldingsRepository._is_date(row["holdings_date"])
                or not HoldingsRepository._is_http_url(row["source_url"])
                or row["holdings_date"] != fund_metadata[row["fund"]]["holdings_date"]
                or row["source_url"] != fund_metadata[row["fund"]]["source_url"]
            ):
                raise ValueError("invalid holding value")

        symbols = [row["symbol"] for row in allowlist]
        if symbols != sorted(set(symbols)) or metadata.get("unique_symbols") != len(symbols):
            raise ValueError("invalid allowlist")
        checked_at = metadata["checked_at"]
        if any(row["checked_at"] != checked_at for row in allowlist):
            raise ValueError("mixed generation timestamps")
        positions = [(row["fund"], row["symbol"]) for row in holdings]
        if len(positions) != len(set(positions)) or any(fund not in FUNDS for fund, _ in positions):
            raise ValueError("invalid holdings")
        if set(symbols) != {symbol for _, symbol in positions}:
            raise ValueError("allowlist and holdings disagree")
        actual_counts = {fund: sum(row["fund"] == fund for row in holdings) for fund in FUNDS}
        if any(
            not isinstance(item["holding_count"], int)
            or item["holding_count"] != actual_counts[item["fund"]]
            for item in funds
        ):
            raise ValueError("fund metadata and holdings disagree")
        memberships: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        for fund, symbol in positions:
            memberships[symbol].add(fund)
        if any(
            row["source"] != "+".join(sorted(memberships[row["symbol"]]))
            for row in allowlist
        ):
            raise ValueError("allowlist membership and holdings disagree")
        names: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        for row in holdings:
            names[row["symbol"]].add(row["name"])
        if any(row["name"] not in names[row["symbol"]] for row in allowlist):
            raise ValueError("allowlist names and holdings disagree")

    @staticmethod
    def _is_text(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    @staticmethod
    def _is_nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _is_number(value: Any) -> bool:
        if not HoldingsRepository._is_text(value):
            return False
        try:
            number = Decimal(value)
        except InvalidOperation:
            return False
        return number.is_finite() and number >= 0

    @staticmethod
    def _is_date(value: Any) -> bool:
        if not HoldingsRepository._is_text(value):
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        return parsed.isoformat() == value

    @staticmethod
    def _is_timestamp(value: Any) -> bool:
        if not HoldingsRepository._is_text(value):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    @staticmethod
    def _is_http_url(value: Any) -> bool:
        if not HoldingsRepository._is_text(value) or any(character.isspace() for character in value):
            return False
        try:
            parsed = urlsplit(value)
            # Reading ``port`` also rejects malformed non-numeric/out-of-range ports.
            parsed.port
            return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        except ValueError:
            return False
