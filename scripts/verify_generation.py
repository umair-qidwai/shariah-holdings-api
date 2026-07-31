#!/usr/bin/env python3
"""Fail CI unless one generated API snapshot is coherent and above safety floors."""

from __future__ import annotations

import argparse
from pathlib import Path

from shariah_holdings.repository import HoldingsRepository

FLOORS = {"SPUS": 150, "HLAL": 150, "MNZL": 200}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    snapshot = HoldingsRepository(args.data_dir).load()
    counts = {fund: len(rows) for fund, rows in snapshot.holdings_by_fund.items()}
    below = {fund: count for fund, count in counts.items() if count < FLOORS[fund]}
    if below:
        raise SystemExit(f"holding counts below floors: {below}")
    union = len(snapshot.allowlist)
    if union != len(snapshot.holdings_by_symbol):
        raise SystemExit("allowlist union and holdings index disagree")
    print(f"generation={snapshot.generation} counts={counts} union={union}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
