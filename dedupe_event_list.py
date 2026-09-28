#!/usr/bin/env python3
"""Fast dedupe event_list.csv by event_domain + start date; keep TSI from best row."""

from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from elib.csv_io import read_csv, write_csv
from elib.dedupe import REMOVED_EXTRA, dedupe_by_domain_and_start_date, write_dedupe_report
from elib.event_domains import attach_event_domains
from elib.fields import TSI_FIELDS
from elib.row_normalize import normalize_event_rows
from elib.paths import ENV_FILE, OUTPUT_DIR

LOG = logging.getLogger("event-listing.dedupe_event_list")

INPUT = OUTPUT_DIR / "event_list.csv"
FINAL = OUTPUT_DIR / "event_list_final.csv"
REMOVED = OUTPUT_DIR / "event_list_dedup_removed.csv"
REPORT = OUTPUT_DIR / "event_list_final_report.md"


def write_final_report(path: Path, input_rows: int, kept: list[dict], dedupe_stats: dict) -> None:
    tier = Counter(r.get("listing_tier") or "" for r in kept)
    lines = [
        "# event_list.csv — dedupe + TSI report",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Input: `{INPUT.name}` ({input_rows} rows)",
        f"- Output: `{FINAL.name}` ({len(kept)} rows)",
        "",
        "## Deduplication",
        "",
        "Key = **event_domain** + **event_start_date** (official site, not listing directory).",
        "When several listings share the same domain and date, the row with the best",
        "Event Intelligence / TSI match and fullest fields is kept; its TSI columns are preserved.",
        "",
        f"- Removed as duplicates: {dedupe_stats.get('removed_rows', 0)}",
        f"- Duplicate groups: {dedupe_stats.get('duplicate_groups', 0)}",
        f"- Rows with `event_domain` filled: {sum(1 for r in kept if (r.get('event_domain') or '').strip())}",
        "",
        "## TSI / Event Intelligence (final file)",
        "",
        f"- In Event Intelligence: {sum(1 for r in kept if r.get('ei_located') == 'Yes')}",
        f"- Published on TSI: {sum(1 for r in kept if r.get('tsi_located') == 'Yes')}",
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
        f"- `{FINAL.name}` — deduplicated events with TSI columns",
        f"- `{REMOVED.name}` — removed duplicate rows",
        f"- `{OUTPUT_DIR.name}/dedupe_report.md` — duplicate group summary",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--translate-tradegov",
        action="store_true",
        help="Translate Trade.gov.in Tamil text to English (slow; uses Google Translate).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not INPUT.is_file():
        LOG.error("Missing %s", INPUT)
        return 1

    rows = read_csv(INPUT)
    LOG.info("Loaded %d rows", len(rows))

    LOG.info("Cleaning invalid URLs/domains (unknown official domain → blank)...")
    rows = normalize_event_rows(rows, force_english=args.translate_tradegov)

    LOG.info("Resolving event_domain (Mongo + URLs); unknown domains left blank...")
    with_domains, domain_stats = attach_event_domains(rows, env_path=ENV_FILE)
    LOG.info("Domains: %s", domain_stats)

    LOG.info("Deduplicating by domain + start date...")
    kept, removed, stats, summaries = dedupe_by_domain_and_start_date(with_domains)
    LOG.info("Kept %d, removed %d", len(kept), len(removed))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    extra = ["listing_domain", "dedupe_domain", "dedupe_start_date"]
    write_csv(FINAL, kept, list(dict.fromkeys(TSI_FIELDS + extra)))
    write_csv(REMOVED, removed, list(dict.fromkeys(TSI_FIELDS + ["listing_domain"] + REMOVED_EXTRA)))
    write_dedupe_report(
        OUTPUT_DIR / "dedupe_report.md",
        stats,
        kept,
        summaries,
        kept_csv_name=FINAL.name,
        removed_csv_name=REMOVED.name,
    )
    write_final_report(REPORT, len(rows), kept, stats)
    LOG.info("Wrote %s", FINAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
