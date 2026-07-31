#!/usr/bin/env python3
"""Refresh normalized SPUS, HLAL, and MNZL holdings files."""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from shariah_holdings.refresh import ValidationPolicy, refresh_to_directory
from shariah_holdings.sources import (HttpDownloader, MnzlAcquirer, acquire_all_sources,
                                      playwright_mnzl_download)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--mnzl-csv", type=Path,
                        help="dated official export named mnzl-official-holdings-YYYY-MM-DD.csv")
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not attempt Playwright if Manzil exposes no direct CSV")
    parser.add_argument("--dry-run", action="store_true", help="validate without replacing output files")
    args = parser.parse_args()

    downloader = HttpDownloader()
    browser = None if args.no_browser else playwright_mnzl_download
    mnzl = MnzlAcquirer(downloader, browser)
    sources = acquire_all_sources(downloader, lambda: mnzl.acquire(args.mnzl_csv))
    policy = ValidationPolicy(max_age_days=args.max_age_days)
    checked_at = datetime.now(timezone.utc)
    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="shariah-refresh-") as temporary:
            outputs = refresh_to_directory(sources, Path(temporary), policy, checked_at)
    else:
        outputs = refresh_to_directory(sources, args.output_dir, policy, checked_at)
    print(f"Validated {len(outputs['holdings.csv'].splitlines()) - 1} fund positions; "
          f"wrote {len(outputs['allowlist.csv'].splitlines()) - 1} unique symbols"
          + (" [dry run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
