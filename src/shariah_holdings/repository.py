"""Read-only access to one authoritative immutable data generation."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_FILES = {"allowlist.csv", "holdings.csv", "metadata.json"}
FUNDS = ("HLAL", "MNZL", "SPUS")
GENERATION_RE = re.compile(r"^[0-9a-f]{24}$")
ALLOWLIST_FIELDS = ("symbol", "name", "source", "checked_at")
HOLDING_FIELDS = (
    "fund", "symbol", "name", "cusip", "weight", "shares", "market_value",
    "holdings_date", "source_url",
)
OPTIONAL_HOLDING_FIELDS = {"cusip", "market_value"}


class DataUnavailable(RuntimeError):
    """The authoritative pointer or generation cannot be safely loaded."""


@dataclass(frozen=True)
class Snapshot:
    generation: str
    allowlist: tuple[dict[str, str], ...]
    holdings: tuple[dict[str, str], ...]
    metadata: dict[str, Any]
    allowlist_csv: str
    holdings_csv: str

    @property
    def checked_at(self) -> str:
        return str(self.metadata["checked_at"])


class HoldingsRepository:
    """Loads a complete snapshot through ``current.json`` for every operation.

    Reloading per request avoids stale process-local state while reading all files
    from the single generation selected by one pointer read.
    """

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)

    def load(self) -> Snapshot:
        try:
            manifest = json.loads((self.data_dir / "current.json").read_text(encoding="utf-8"))
            generation_id = manifest.get("generation")
            if not isinstance(generation_id, str) or not GENERATION_RE.fullmatch(generation_id):
                raise ValueError("invalid generation")
            if manifest.get("path") != f"generations/{generation_id}":
                raise ValueError("invalid generation path")
            if set(manifest.get("files", ())) != EXPECTED_FILES:
                raise ValueError("invalid generation files")

            generations = (self.data_dir / "generations").resolve()
            generation = self.data_dir / "generations" / generation_id
            if generation.resolve().parent != generations or not generation.is_dir():
                raise ValueError("invalid generation directory")
            outputs: dict[str, str] = {}
            for name in EXPECTED_FILES:
                path = generation / name
                if path.resolve().parent != generation.resolve() or not path.is_file():
                    raise ValueError("invalid generation file")
                outputs[name] = path.read_text(encoding="utf-8")

            allowlist = self._read_csv(outputs["allowlist.csv"], ALLOWLIST_FIELDS)
            holdings = self._read_csv(outputs["holdings.csv"], HOLDING_FIELDS)
            metadata = json.loads(outputs["metadata.json"])
            self._validate(allowlist, holdings, metadata)
            return Snapshot(
                generation=generation_id,
                allowlist=tuple(allowlist),
                holdings=tuple(holdings),
                metadata=metadata,
                allowlist_csv=outputs["allowlist.csv"],
                holdings_csv=outputs["holdings.csv"],
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, csv.Error, KeyError) as exc:
            raise DataUnavailable("authoritative data is unavailable") from exc

    @staticmethod
    def _read_csv(raw: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(raw))
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError("unexpected CSV schema")
        rows = list(reader)
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
        if not isinstance(metadata, dict) or not isinstance(metadata.get("checked_at"), str):
            raise ValueError("invalid metadata")
        funds = metadata.get("funds")
        required_fund_fields = {"fund", "holdings_date", "holding_count", "source_url"}
        if (
            not isinstance(funds, list)
            or len(funds) != len(FUNDS)
            or any(not isinstance(item, dict) or not required_fund_fields <= set(item) for item in funds)
            or {item["fund"] for item in funds} != set(FUNDS)
        ):
            raise ValueError("invalid fund metadata")
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

    @staticmethod
    def holdings_by_symbol(snapshot: Snapshot) -> dict[str, list[dict[str, str]]]:
        result: dict[str, list[dict[str, str]]] = {}
        for row in snapshot.holdings:
            result.setdefault(row["symbol"], []).append(row)
        return result
