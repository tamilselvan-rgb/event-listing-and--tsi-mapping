#!/usr/bin/env python3
"""Fast: dedupe events_scraped.csv + one TSI pass → events_final.csv + report."""

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
from elib.dedupe import dedupe_by_domain_and_start_date, write_dedupe_report
from elib.event_domains import attach_event_domains
from elib.fields import DEDUPE_AUDIT_FIELDS, FIELDS, FINAL_CSV_FIELDS, TSI_FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.row_normalize import normalize_event_rows
from elib.tsi_mapping import create_tsi_engine, tsi_match_row

LOG = logging.getLogger("event-listing.finalize_scraped")

INPUT = OUTPUT_DIR / "events_scraped.csv"
FINAL = OUTPUT_DIR / "events_final.csv"
REMOVED = OUTPUT_DIR / "events_dedup_removed.csv"
REPORT = OUTPUT_DIR / "events_final_report.md"


def write_final_report(
    path: Path,
    *,
    input_rows: int,
    dedupe_stats: dict,
    final_rows: list[dict],
    domain_stats: dict,
) -> None:
    tier = Counter(r.get("listing_tier") or "" for r in final_rows)
    dom_filled = sum(1 for r in final_rows if (r.get("event_domain") or "").strip())
    lines = [
        "# Events final report (scraped → dedupe → TSI)",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Source: `{INPUT.name}` ({input_rows} rows)",
        f"- Output: `{FINAL.name}` ({len(final_rows)} rows)",
        "",
        "## Deduplication (event_domain + start date)",
        "",
        f"- Removed as duplicates: {dedupe_stats.get('removed_rows', 0)}",
        f"- Duplicate groups: {dedupe_stats.get('duplicate_groups', 0)}",
        f"- Rows with `event_domain`: {dom_filled}",
        "",
        "## Domain resolution (before dedupe)",
        "",
    ]
    for key, count in sorted(domain_stats.items(), key=lambda x: -x[1]):
        lines.append(f"- {key}: {count}")
    lines += [
        "",
        "## TSI / Event Intelligence",
        "",
        f"- In Event Intelligence: {sum(1 for r in final_rows if r.get('ei_located') == 'Yes')}",
        f"- Published on TSI: {sum(1 for r in final_rows if r.get('tsi_located') == 'Yes')}",
        f"- EI not published: {tier.get('EI not published', 0)}",
        f"- Not in Event Intelligence: {tier.get('Not in Event Intelligence', 0)}",
        "",
        "## Listing tier",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ]
    for label, count in sorted(tier.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {label or '(empty)'} | {count} |")
    lines += [
        "",
        "## Files",
        "",
        f"- `{FINAL.name}` — deliverable",
        f"- `{REMOVED.name}` — dedupe audit",
        f"- `dedupe_report.md` — duplicate groups summary",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--skip-mongo", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Missing %s", args.input)
        return 1

    rows = read_csv(args.input)
    LOG.info("Loaded %d rows", len(rows))
    rows = normalize_event_rows(rows, force_english=False)
    rows, domain_stats = attach_event_domains(rows, env_path=ENV_FILE)
    LOG.info("Domains: %s", domain_stats)

    kept, removed, dedupe_stats, summaries = dedupe_by_domain_and_start_date(rows)
    LOG.info("Deduped: %d kept, %d removed", len(kept), len(removed))

    LOG.info("TSI mapping (single pass on %d rows)...", len(kept))
    engine = create_tsi_engine(env_path=ENV_FILE, skip_mongo=args.skip_mongo)
    final_rows: list[dict] = []
    total = len(kept)
    for index, event in enumerate(kept, start=1):
        final_rows.append({**event, **tsi_match_row(event, engine)})
        if index % 500 == 0 or index == total:
            LOG.info("TSI progress: %d / %d", index, total)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(FINAL, final_rows, FINAL_CSV_FIELDS)
    write_csv(REMOVED, removed, FIELDS + ["listing_domain"] + DEDUPE_AUDIT_FIELDS)
    write_dedupe_report(
        OUTPUT_DIR / "dedupe_report.md",
        dedupe_stats,
        final_rows,
        summaries,
        kept_csv_name=FINAL.name,
        removed_csv_name=REMOVED.name,
    )
    write_final_report(REPORT, input_rows=len(rows), dedupe_stats=dedupe_stats, final_rows=final_rows, domain_stats=domain_stats)
    LOG.info("Wrote %s and %s", FINAL, REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
