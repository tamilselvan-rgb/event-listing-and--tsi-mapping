#!/usr/bin/env python3
"""Fill missing event_domain on events_scraped.csv using Serper (parallel, dual API keys)."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv, write_csv
from elib.fields import FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.serper_domain import (
    SerperDomainCache,
    SerperKeyPool,
    discover_domain_for_row,
    load_serper_keys,
)

LOG = logging.getLogger("event-listing.discover_serper")


def needs_domain(row: dict[str, str]) -> bool:
    if row.get("scrape_status") != "ok":
        return False
    if (row.get("event_domain") or "").strip():
        return False
    return bool((row.get("event_name") or "").strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=OUTPUT_DIR / "events_scraped.csv")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "events_scraped.csv")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers (8–10 avoids Serper 429s)")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to query (0 = all missing)")
    parser.add_argument("--seed", type=int, default=42, help="Shuffle seed for random order")
    parser.add_argument("--mongo-env", type=Path, default=ENV_FILE)
    parser.add_argument(
        "--purge-cache-errors",
        action="store_true",
        default=True,
        help="Drop cached Serper API failures before run (default: on).",
    )
    parser.add_argument(
        "--no-purge-cache-errors",
        action="store_false",
        dest="purge_cache_errors",
        help="Keep cached failure entries (not recommended).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    keys = load_serper_keys(args.mongo_env)
    if not keys:
        LOG.error("Set SERPER_API_KEYS in event-listing/.env (comma-separated)")
        return 1
    if not args.input.is_file():
        LOG.error("Input not found: %s", args.input)
        return 1

    rows = read_csv(args.input)
    cache = SerperDomainCache()
    if args.purge_cache_errors:
        removed = cache.purge_errors()
        if removed:
            cache.save()
            LOG.info("Purged %d stale Serper error entries from cache", removed)
    key_pool = SerperKeyPool(keys)

    indices = [i for i, row in enumerate(rows) if needs_domain(row)]
    random.Random(args.seed).shuffle(indices)
    if args.limit and args.limit > 0:
        indices = indices[: args.limit]

    LOG.info(
        "Serper domain discovery: %d rows to process (%d workers, %d API keys)",
        len(indices),
        args.workers,
        len(keys),
    )

    stats = {"filled": 0, "still_blank": 0, "errors": 0, "from_cache": 0, "api_calls": 0}

    def work(index: int) -> tuple[int, dict[str, str], bool]:
        row = rows[index]
        cached = cache.get(row)
        if cached is not None:
            return index, cached, True
        result = discover_domain_for_row(row, key_pool)
        cache.set(row, result)
        return index, result, False

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(work, i): i for i in indices}
        for future in as_completed(futures):
            index, result, from_cache = future.result()
            row = rows[index]
            if from_cache:
                stats["from_cache"] += 1
            else:
                stats["api_calls"] += 1
            if result.get("domain_source") == "serper_error":
                stats["errors"] += 1
            if result.get("event_domain"):
                row["event_domain"] = result["event_domain"]
                row["official_site_url"] = result.get("official_site_url") or row.get("official_site_url") or ""
                row["domain_source"] = result.get("domain_source") or "serper"
                stats["filled"] += 1
            else:
                stats["still_blank"] += 1
            done += 1
            if done % 100 == 0:
                cache.save()
                LOG.info(
                    "Progress %d / %d — filled %d, blank %d",
                    done,
                    len(indices),
                    stats["filled"],
                    stats["still_blank"],
                )

    cache.save()
    write_csv(args.output, rows, FIELDS)
    LOG.info("Wrote %s", args.output)
    LOG.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
