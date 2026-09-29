#!/usr/bin/env python3
"""Run individual site scrapers; writes per-site CSV under output/scraped/."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from site_scrapers.base import SCRAPED_DIR, write_site_core_csv, write_site_csv
from site_scrapers.eventseye import scrape_eventseye
from site_scrapers.expofinder import scrape_expofinder
from site_scrapers.expomap import scrape_expomap
from site_scrapers.exhibitionsvoice import scrape_exhibitionsvoice
from site_scrapers.hitex import scrape_hitex
from site_scrapers.messefrankfurt import scrape_messefrankfurt
from site_scrapers.exhibitorsdata import scrape_exhibitorsdata
from site_scrapers.tradeshow_calendar import scrape_tradeshow_calendar
from site_scrapers.nice_convex import scrape_nice_convex
from site_scrapers.tradefest import scrape_tradefest
from site_scrapers.vendelux import scrape_vendelux

LOG = logging.getLogger("event-listing.site_scrapers")

SCRAPERS = {
    "expofinder": scrape_expofinder,
    "eventseye": scrape_eventseye,
    "tradeshowcalendar": scrape_tradeshow_calendar,
    "exhibitorsdata": scrape_exhibitorsdata,
    "tradefest": scrape_tradefest,
    "exhibitionsvoice": scrape_exhibitionsvoice,
    "hitex": scrape_hitex,
    "messefrankfurt": scrape_messefrankfurt,
    "niceconvex": scrape_nice_convex,
    "vendelux": scrape_vendelux,
    "expomap": scrape_expomap,
}


def write_scrape_report(slug: str, stats: dict, row_count: int, csv_path: Path) -> Path:
    path = SCRAPED_DIR / f"{slug}_report.md"
    lines = [
        f"# Scrape report — {slug}",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Rows: {row_count}",
        f"- Output: `{csv_path.name}`",
        f"- Listing mode: {stats.get('listing_mode', '')}",
        f"- API/list pages visited: {stats.get('pages_visited', 0)}",
        f"- Detail pages: {stats.get('detail_pages_visited', 0)}",
        f"- Status: {stats.get('status', '')}",
        f"- Error: {stats.get('error', '') or '—'}",
        f"- Session file: {stats.get('session_file', '') or '—'}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "site",
        choices=sorted(SCRAPERS),
        help="Site scraper to run",
    )
    parser.add_argument("--no-details", action="store_true", help="Skip detail page enrichment")
    parser.add_argument("--serper-domains", action="store_true", help="Serper search for missing domains")
    parser.add_argument("--serper-max", type=int, default=0, help="Cap Serper lookups (0 = all missing)")
    parser.add_argument("--max-api-pages", type=int, default=0, help="Limit API pages per region (0 = all)")
    parser.add_argument(
        "--max-list-pages",
        type=int,
        default=0,
        help="Limit listing pagination pages (Trade Show Calendar, 0 = all)",
    )
    parser.add_argument("--max-details", type=int, default=0, help="Limit detail fetches (0 = all)")
    parser.add_argument("--include-ongoing", action="store_true", help="Do not restrict to upcoming/Coming Soon")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--browser-profile", type=str, default="", help="Persistent Chromium profile path")
    parser.add_argument("--login-wait-seconds", type=int, default=0)
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Visit every ExpoFinder region filter URL (slower; may need login for full API)",
    )
    parser.add_argument(
        "--session-file",
        type=str,
        default="",
        help="Playwright storage_state or cookies JSON (default: config/expofinder_session.json if present)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    SCRAPED_DIR.mkdir(parents=True, exist_ok=True)

    scrape_fn = SCRAPERS[args.site]
    if args.site == "exhibitorsdata":
        rows, stats = scrape_fn(
            max_pages=args.max_list_pages,
            enrich_details=not args.no_details,
        )
    elif args.site == "tradeshowcalendar":
        rows, stats = scrape_fn(max_pages=args.max_list_pages)
    elif args.site == "eventseye":
        rows, stats = scrape_fn(
            enrich=not args.no_details,
            max_details=args.max_details,
        )
    elif args.site == "expofinder":
        rows, stats = scrape_fn(
            upcoming_only=not args.include_ongoing,
            enrich=not args.no_details,
            serper_domains=args.serper_domains,
            serper_max=args.serper_max,
            max_api_pages=args.max_api_pages,
            max_details=args.max_details,
            all_regions=args.all_regions,
            headless=not args.headed,
            browser_profile=args.browser_profile,
            login_wait_seconds=args.login_wait_seconds,
            session_file=args.session_file.strip() or None,
        )
    elif args.site == "tradefest":
        rows, stats = scrape_fn(
            max_pages=args.max_list_pages,
            enrich_details=not args.no_details,
            max_details=args.max_details,
            future_only=not args.include_ongoing,
        )
    elif args.site == "exhibitionsvoice":
        rows, stats = scrape_fn(
            max_pages=args.max_list_pages,
            enrich_details=not args.no_details,
            max_details=args.max_details,
        )
    elif args.site == "hitex":
        rows, stats = scrape_fn(
            enrich_details=not args.no_details,
            max_details=args.max_details,
        )
    elif args.site == "messefrankfurt":
        rows, stats = scrape_fn(
            enrich_details=not args.no_details,
            max_details=args.max_details,
            upcoming_only=not args.include_ongoing,
        )
    elif args.site == "niceconvex":
        rows, stats = scrape_fn(
            max_pages=args.max_list_pages,
            enrich_details=not args.no_details,
            max_details=args.max_details,
        )
    elif args.site == "vendelux":
        rows, stats = scrape_fn(
            enrich_details=not args.no_details,
            max_events=args.max_details,
        )
    elif args.site == "expomap":
        rows, stats = scrape_fn(
            max_pages=args.max_list_pages,
            enrich_details=not args.no_details,
            max_details=args.max_details,
        )
    else:
        rows, stats = scrape_fn()

    csv_path = write_site_csv(rows, args.site)
    core_path = write_site_core_csv(rows, args.site)
    report_path = write_scrape_report(args.site, stats, len(rows), csv_path)
    LOG.info(
        "Wrote %s, %s (%d rows), and %s",
        csv_path,
        core_path,
        len(rows),
        report_path,
    )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
