#!/usr/bin/env python3
"""CLI: dedupe a CSV by event_domain + start date (see also run_pipeline.py)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv, write_csv
from elib.dedupe import REMOVED_EXTRA, dedupe_by_domain_and_start_date, write_dedupe_report
from elib.fields import FIELDS
from elib.paths import OUTPUT_DIR

LOG = logging.getLogger("event-listing.dedupe_domain")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=OUTPUT_DIR / "events_scraped.csv")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Missing input: %s", args.input)
        return 1

    rows = read_csv(args.input)
    kept, removed, stats, summaries = dedupe_by_domain_and_start_date(rows)
    out_dir = args.output_dir
    kept_path = out_dir / "events_deduped.csv"
    removed_path = out_dir / "events_dedup_removed.csv"
    write_csv(kept_path, kept, FIELDS + ["listing_domain", "dedupe_domain", "dedupe_start_date"])
    write_csv(removed_path, removed, FIELDS + REMOVED_EXTRA)
    write_dedupe_report(
        out_dir / "dedupe_report.md",
        stats,
        kept,
        summaries,
        kept_csv_name=kept_path.name,
        removed_csv_name=removed_path.name,
    )
    LOG.info("Wrote %d kept, %d removed", len(kept), len(removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
