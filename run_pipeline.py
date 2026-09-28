#!/usr/bin/env python3
"""Run domain resolution, dedupe, and TSI mapping on an existing events_scraped.csv."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.pipeline import EVENTS_SCRAPED, finalize_events

LOG = logging.getLogger("event-listing.run_pipeline")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve event domains, dedupe by domain+start date, map TSI."
    )
    parser.add_argument("--input", type=Path, default=EVENTS_SCRAPED)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--mongo-env", type=Path, default=ENV_FILE)
    parser.add_argument("--skip-mongo", action="store_true")
    parser.add_argument(
        "--domain-web-search",
        action="store_true",
        help="Run slow web search for missing official domains (off by default).",
    )
    parser.add_argument("--domain-workers", type=int, default=8)
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Input already has TSI columns (e.g. event_list.csv); skip slow pre-dedupe rematch.",
    )
    parser.add_argument(
        "--final-csv",
        type=str,
        default="",
        help="Final output CSV name (default: events_final.csv, or event_list_final.csv for event_list input).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Input not found: %s (run scrape_events.py first)", args.input)
        return 1

    events = read_csv(args.input)
    LOG.info("Loaded %d rows from %s", len(events), args.input)
    final_name = args.final_csv.strip()
    if not final_name and args.input.name == "event_list.csv":
        final_name = "event_list_final.csv"
    if not final_name:
        final_name = "events_final.csv"
    fast = args.fast or args.input.name == "event_list.csv"
    finalize_events(
        events,
        env_path=args.mongo_env,
        skip_mongo=args.skip_mongo,
        skip_domain_web_search=not args.domain_web_search,
        domain_workers=args.domain_workers,
        output_dir=args.output_dir,
        scrape_stats={"unique_rows": len(events), "raw_rows": len(events)},
        fast_existing_tsi=fast,
        final_csv_name=final_name,
    )
    LOG.info("Done — %s", args.output_dir / final_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
