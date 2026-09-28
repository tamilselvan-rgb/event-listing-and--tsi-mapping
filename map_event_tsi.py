#!/usr/bin/env python3
"""Re-run TSI / Event Intelligence mapping on events_deduped.csv or events_final.csv."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv, write_csv
from elib.fields import FINAL_CSV_FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.pipeline import EVENTS_DEDUPED, EVENTS_FINAL
from elib.tsi_mapping import apply_tsi_mapping, create_tsi_engine

LOG = logging.getLogger("event-listing.map_tsi")


def write_mapping_report(path: Path, rows: list[dict], source_path: Path) -> None:
    tier_counts = Counter(row.get("listing_tier") or "" for row in rows)
    lines = [
        "# TSI mapping report",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Input: {source_path.name}",
        f"- Rows: {len(rows)}",
        f"- In Event Intelligence: {sum(1 for r in rows if r.get('ei_located') == 'Yes')}",
        f"- Published on TSI: {sum(1 for r in rows if r.get('tsi_located') == 'Yes')}",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ]
    for tier, count in sorted(tier_counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {tier or '(empty)'} | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=EVENTS_DEDUPED)
    parser.add_argument("--output", type=Path, default=EVENTS_FINAL)
    parser.add_argument("--mongo-env", type=Path, default=ENV_FILE)
    parser.add_argument("--skip-mongo", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Input not found: %s", args.input)
        return 1

    events = read_csv(args.input)
    engine = create_tsi_engine(env_path=args.mongo_env, skip_mongo=args.skip_mongo)
    rows = apply_tsi_mapping(events, engine)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, rows, FINAL_CSV_FIELDS)
    write_mapping_report(args.output.parent / "tsi_mapping_report.md", rows, args.input)
    LOG.info("Wrote %s (%d rows)", args.output, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
