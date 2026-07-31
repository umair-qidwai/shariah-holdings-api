"""Installed command-line entry point for strict holdings refreshes."""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .refresh import ValidationPolicy, refresh_to_directory
from .sources import HttpDownloader, MnzlAcquirer, acquire_all_sources, playwright_mnzl_download


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh normalized SPUS, HLAL, and MNZL holdings files.")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--mnzl-csv", type=Path,
                        help="dated official export named mnzl-official-holdings-YYYY-MM-DD.csv")
    parser.add_argument("--mnzl-fallback-csv", type=Path,
                        help="dated official export used only if verified live MNZL acquisition fails")
    parser.add_argument("--mnzl-as-of", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="caller-validated issuer as-of date for an otherwise undated live export")
    parser.add_argument("--max-age-days", type=_nonnegative_int, default=7)
    parser.add_argument("--mnzl-max-age-days", type=_nonnegative_int, default=120,
                        help="MNZL freshness limit for dated quarterly exports (default: 120)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not attempt Playwright if Manzil exposes no direct download")
    parser.add_argument("--dry-run", action="store_true", help="validate without replacing output files")
    args = parser.parse_args(argv)
    if args.mnzl_csv is not None and args.mnzl_fallback_csv is not None:
        parser.error("--mnzl-csv and --mnzl-fallback-csv are mutually exclusive")
    policy = ValidationPolicy(max_age_days=args.max_age_days,
                              max_age_days_by_fund={"MNZL": args.mnzl_max_age_days})

    downloader = HttpDownloader()
    browser = None if args.no_browser else playwright_mnzl_download
    mnzl = MnzlAcquirer(downloader, browser)
    sources = acquire_all_sources(
        downloader, lambda: mnzl.acquire(args.mnzl_csv, as_of_date=args.mnzl_as_of,
                                         fallback_export=args.mnzl_fallback_csv))
    acquired_at = datetime.now(timezone.utc)
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="shariah-refresh-") as temporary:
            outputs = refresh_to_directory(sources, Path(temporary), policy, acquired_at)
    else:
        outputs = refresh_to_directory(sources, args.output_dir, policy, acquired_at)
    print(f"Validated {len(outputs['holdings.csv'].splitlines()) - 1} fund positions; "
          f"wrote {len(outputs['allowlist.csv'].splitlines()) - 1} unique symbols"
          + (" [dry run]" if args.dry_run else ""))
    return 0


def run() -> int:
    try:
        return main()
    except Exception as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
