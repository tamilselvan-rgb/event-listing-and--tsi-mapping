"""Shared types and CSV output for site scrapers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from elib.csv_io import write_csv
from elib.fields import EVENT_CORE_FIELDS, FIELDS
from elib.paths import OUTPUT_DIR

EXPORT_CORE_FIELDS = EVENT_CORE_FIELDS + ["event_url"]

SCRAPED_DIR = OUTPUT_DIR / "scraped"

SOURCE_EXPOFINDER = {
    "name": "ExpoFinder",
    "url": "https://www.expofinder.com/en/list",
}


def empty_row(source: dict[str, str], status: str = "ok") -> dict[str, str]:
    return {
        "event_name": "",
        "event_start_date": "",
        "event_end_date": "",
        "event_domain": "",
        "venue": "",
        "city": "",
        "country": "",
        "source_name": source["name"],
        "source_url": source["url"],
        "event_url": "",
        "official_site_url": "",
        "domain_source": "",
        "scraped_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "scrape_status": status,
        "scrape_error": "",
    }


def base_stats() -> dict[str, Any]:
    return {
        "pages_visited": 0,
        "detail_pages_visited": 0,
        "raw_rows": 0,
        "status": "ok",
        "error": "",
        "truncated": False,
        "listing_mode": "",
    }


def write_site_csv(rows: list[dict[str, str]], slug: str) -> Path:
    path = SCRAPED_DIR / f"{slug}.csv"
    write_csv(path, rows, FIELDS)
    return path


def write_site_core_csv(rows: list[dict[str, str]], slug: str) -> Path:
    """Name, dates, domain, venue, city, country (+ event_url)."""
    path = SCRAPED_DIR / f"{slug}_events.csv"
    write_csv(path, rows, EXPORT_CORE_FIELDS)
    return path
