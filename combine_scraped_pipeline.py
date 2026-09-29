#!/usr/bin/env python3
"""Combine per-site scraped CSVs → dedupe → TSI → summary outputs."""

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
from site_scrapers.base import SCRAPED_DIR

LOG = logging.getLogger("event-listing.combine_scraped")

EVENTS_SCRAPED = OUTPUT_DIR / "events_scraped.csv"
EVENTS_DEDUPED = OUTPUT_DIR / "events_deduped.csv"
EVENTS_FINAL = OUTPUT_DIR / "events_final.csv"
EVENTS_NOT_ON_TSI = OUTPUT_DIR / "events_not_on_tsi.csv"
EVENTS_DEDUP_REMOVED = OUTPUT_DIR / "events_dedup_removed.csv"
COMBINED_REPORT = OUTPUT_DIR / "combined_scrape_report.md"


def scraped_site_csvs(scraped_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for path in sorted(scraped_dir.glob("*.csv")):
        if path.name.endswith("_events.csv"):
            continue
        paths.append(path)
    return paths


def combine_scraped(scraped_dir: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    combined: list[dict[str, str]] = []
    per_source: dict[str, int] = {}
    for path in scraped_site_csvs(scraped_dir):
        rows = read_csv(path)
        if not rows:
            LOG.warning("Empty scrape file: %s", path.name)
            continue
        source = rows[0].get("source_name") or path.stem
        per_source[source] = per_source.get(source, 0) + len(rows)
        combined.extend(rows)
        LOG.info("Loaded %d rows from %s", len(rows), path.name)
    return combined, per_source


def write_combined_report(
    path: Path,
    *,
    total_combined: int,
    total_deduped: int,
    dedupe_stats: dict,
    per_source: dict[str, int],
    final_rows: list[dict],
) -> None:
    ei_yes = sum(1 for r in final_rows if (r.get("ei_located") or "").strip() == "Yes")
    tsi_yes = sum(1 for r in final_rows if (r.get("tsi_located") or "").strip() == "Yes")
    not_ei = len(final_rows) - ei_yes
    not_tsi = len(final_rows) - tsi_yes

    lines = [
        "# Combined scrape report (all site scrapers)",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|---|---:|",
        f"| **Total events (combined, all sources)** | {total_combined} |",
        f"| **Total events after deduplication** | {total_deduped} |",
        f"| Duplicate rows removed | {dedupe_stats.get('removed_rows', 0)} |",
        f"| Duplicate groups | {dedupe_stats.get('duplicate_groups', 0)} |",
        f"| **Events present in Event Intelligence (EI)** | {ei_yes} |",
        f"| **Events present on TSI** (`tsi_located=Yes`) | {tsi_yes} |",
        f"| **Events not in Event Intelligence** | {not_ei} |",
        f"| **Events not on TSI** | {not_tsi} |",
        "",
        "## Per-source row counts (before cross-source dedupe)",
        "",
        "| Source | Rows |",
        "|---|---:|",
    ]
    for name, count in sorted(per_source.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {name} | {count} |")

    tier = Counter(r.get("listing_tier") or "" for r in final_rows)
    lines += [
        "",
        "## Listing tier (after dedupe)",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ]
    for label, count in sorted(tier.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {label or '(empty)'} | {count} |")

    lines += [
        "",
        "## Output files",
        "",
        f"- `{EVENTS_SCRAPED.name}` — all sources combined",
        f"- `{EVENTS_DEDUPED.name}` — unique rows (core + scrape meta, no TSI columns)",
        f"- `{EVENTS_FINAL.name}` — **deduplicated deliverable** with TSI/EI columns",
        f"- `{EVENTS_NOT_ON_TSI.name}` — deduped events where `tsi_located` ≠ Yes",
        f"- `{EVENTS_DEDUP_REMOVED.name}` — duplicate rows removed",
        f"- `dedupe_report.md` — duplicate group details",
        "",
        "## Deduplication rule",
        "",
        "Same normalized `event_domain` + `event_start_date` → one row kept.",
        "Rows without domain merge only when name and start date match.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scraped-dir", type=Path, default=SCRAPED_DIR)
    parser.add_argument("--skip-mongo", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.scraped_dir.is_dir():
        LOG.error("Scraped dir not found: %s", args.scraped_dir)
        return 1

    combined, per_source = combine_scraped(args.scraped_dir)
    if not combined:
        LOG.error("No rows in %s", args.scraped_dir)
        return 1

    total_combined = len(combined)
    combined = normalize_event_rows(combined, force_english=False)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(EVENTS_SCRAPED, combined, FIELDS)
    LOG.info("Wrote %s (%d rows)", EVENTS_SCRAPED, len(combined))

    combined, domain_stats = attach_event_domains(combined, env_path=ENV_FILE)
    LOG.info("Domain attachment: %s", domain_stats)

    kept, removed, dedupe_stats, summaries = dedupe_by_domain_and_start_date(combined)
    LOG.info(
        "Deduped: %d kept, %d removed, %d groups",
        len(kept),
        len(removed),
        dedupe_stats.get("duplicate_groups", 0),
    )

    write_csv(EVENTS_DEDUPED, kept, FIELDS + ["listing_domain"])

    LOG.info("TSI mapping on %d unique rows...", len(kept))
    engine = create_tsi_engine(env_path=ENV_FILE, skip_mongo=args.skip_mongo)
    final_rows: list[dict] = []
    for index, event in enumerate(kept, start=1):
        final_rows.append({**event, **tsi_match_row(event, engine)})
        if index % 1000 == 0 or index == len(kept):
            LOG.info("TSI progress %d / %d", index, len(kept))

    not_on_tsi = [r for r in final_rows if (r.get("tsi_located") or "").strip() != "Yes"]

    write_csv(EVENTS_FINAL, final_rows, FINAL_CSV_FIELDS)
    write_csv(EVENTS_NOT_ON_TSI, not_on_tsi, FINAL_CSV_FIELDS)
    write_csv(EVENTS_DEDUP_REMOVED, removed, FIELDS + ["listing_domain"] + DEDUPE_AUDIT_FIELDS)
    write_dedupe_report(
        OUTPUT_DIR / "dedupe_report.md",
        dedupe_stats,
        final_rows,
        summaries,
        kept_csv_name=EVENTS_FINAL.name,
        removed_csv_name=EVENTS_DEDUP_REMOVED.name,
    )
    write_combined_report(
        COMBINED_REPORT,
        total_combined=total_combined,
        total_deduped=len(final_rows),
        dedupe_stats=dedupe_stats,
        per_source=per_source,
        final_rows=final_rows,
    )

    LOG.info(
        "Done: %d combined → %d deduped | EI=%d TSI=%d | not on TSI=%d",
        total_combined,
        len(final_rows),
        sum(1 for r in final_rows if r.get("ei_located") == "Yes"),
        sum(1 for r in final_rows if r.get("tsi_located") == "Yes"),
        len(not_on_tsi),
    )
    LOG.info("Wrote %s, %s, %s", EVENTS_FINAL, EVENTS_NOT_ON_TSI, COMBINED_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
