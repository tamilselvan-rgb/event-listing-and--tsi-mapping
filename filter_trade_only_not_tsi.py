#!/usr/bin/env python3
"""Export trade-only events that are not published on TSI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv, write_csv
from elib.paths import OUTPUT_DIR

LOG = logging.getLogger("event-listing.filter_not_tsi")

INPUT = OUTPUT_DIR / "events_trade_only_tsi.csv"
OUTPUT = OUTPUT_DIR / "events_trade_only_not_on_tsi.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Missing %s", args.input)
        return 1

    rows = read_csv(args.input)
    if not rows:
        LOG.error("Empty input")
        return 1

    not_tsi = [r for r in rows if (r.get("tsi_located") or "").strip() != "Yes"]
    fields = list(rows[0].keys())
    write_csv(args.output, not_tsi, fields)
    LOG.info(
        "Wrote %s (%d rows not on TSI, %d on TSI)",
        args.output,
        len(not_tsi),
        len(rows) - len(not_tsi),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
