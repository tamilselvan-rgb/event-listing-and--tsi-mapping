#!/usr/bin/env python3
"""TSI / EI check for events_trade_only.csv (domain if present, else name+date+location)."""

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

from elib.event_domains import host_of, normalize_domain
from elib.fields import TSI_MATCH_FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.tsi_mapping import create_tsi_engine

LOG = logging.getLogger("event-listing.map_trade_only")

INPUT = OUTPUT_DIR / "events_trade_only.csv"
OUTPUT = OUTPUT_DIR / "events_trade_only_tsi.csv"
REPORT = OUTPUT_DIR / "events_trade_only_tsi_report.md"

EXTRA_OUT = ["event_domain", "domain_match_used"]


def country_for_row(row: dict[str, str]) -> str:
    return (row.get("country") or row.get("country_code") or "").strip()


def website_domain(row: dict[str, str]) -> str:
    return normalize_domain(host_of(row.get("event_website") or ""))


def write_report(path: Path, rows: list[dict], stats: dict) -> None:
    tier = Counter(r.get("listing_tier") or "" for r in rows)
    lines = [
        "# TSI report — events_trade_only.csv",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Rows: {len(rows)}",
        "",
        "## Matching rules",
        "",
        "1. If `event_website` is set → match tries **domain** first, then name/date/location.",
        "2. If **no website** → match uses **event name**, **start_date**, **venue**, **city**, **country** only.",
        "",
        "## Website coverage",
        "",
        f"- Rows with event website: {stats['with_website']}",
        f"- Rows without event website: {stats['without_website']}",
        "",
        "## TSI / Event Intelligence (all rows)",
        "",
        f"- In Event Intelligence (`ei_located=Yes`): {stats['ei_yes']}",
        f"- Published on TSI (`tsi_located=Yes`): {stats['tsi_yes']}",
        f"- Not in Event Intelligence: {tier.get('Not in Event Intelligence', 0)}",
        f"- EI not published: {tier.get('EI not published', 0)}",
        "",
        "## By website presence",
        "",
        "| Segment | Rows | EI Yes | TSI Yes |",
        "|---|---:|---:|---:|",
        f"| Has website | {stats['with_website']} | {stats['ei_web']} | {stats['tsi_web']} |",
        f"| No website (name/date/venue/city/country) | {stats['without_website']} | {stats['ei_noweb']} | {stats['tsi_noweb']} |",
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
        "## Top match methods",
        "",
        "| Method | Count |",
        "|---|---:|",
    ]
    for method, count in stats["methods"].most_common(15):
        lines.append(f"| {method or '(empty)'} | {count} |")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--skip-mongo", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.is_file():
        LOG.error("Missing %s", args.input)
        return 1

    import csv

    with args.input.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        LOG.error("Empty input")
        return 1

    fieldnames = list(rows[0].keys()) + [
        c for c in EXTRA_OUT + TSI_MATCH_FIELDS if c not in rows[0]
    ]
    LOG.info("Loaded %d rows; building TSI engine...", len(rows))
    engine = create_tsi_engine(env_path=ENV_FILE, skip_mongo=args.skip_mongo)

    stats: dict = {
        "with_website": 0,
        "without_website": 0,
        "ei_yes": 0,
        "tsi_yes": 0,
        "ei_web": 0,
        "tsi_web": 0,
        "ei_noweb": 0,
        "tsi_noweb": 0,
        "methods": Counter(),
    }

    total = len(rows)
    for index, row in enumerate(rows, start=1):
        dom = website_domain(row)
        has_web = bool(dom)
        if has_web:
            stats["with_website"] += 1
        else:
            stats["without_website"] += 1

        match = engine.match_event(
            event_name=row.get("event_name") or "",
            domain=dom,
            start_date=row.get("start_date") or "",
            city=row.get("city") or "",
            country=country_for_row(row),
            venue=row.get("venue") or "",
        )
        row["event_domain"] = dom
        row["domain_match_used"] = "yes" if has_web else "no"
        for key in TSI_MATCH_FIELDS:
            val = match.get(key, "")
            row[key] = val if val is not None else ""

        if row.get("ei_located") == "Yes":
            stats["ei_yes"] += 1
            if has_web:
                stats["ei_web"] += 1
            else:
                stats["ei_noweb"] += 1
        if row.get("tsi_located") == "Yes":
            stats["tsi_yes"] += 1
            if has_web:
                stats["tsi_web"] += 1
            else:
                stats["tsi_noweb"] += 1
        stats["methods"][match.get("match_method") or ""] += 1

        if index % 1000 == 0 or index == total:
            LOG.info("TSI progress: %d / %d", index, total)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    write_report(REPORT, rows, stats)
    LOG.info("Wrote %s and %s", args.output, REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
