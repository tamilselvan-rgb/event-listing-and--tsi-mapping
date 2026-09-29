"""Scrape https://vendelux.com/b2b-events (by-month listings → insights → app/event)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from site_scrapers.base import base_stats, empty_row
from site_scrapers.vendelux_detail import (
    app_event_url_from_insights,
    parse_app_event_html,
    parse_insights_fallback,
    parse_listing_anchor,
)
from site_scrapers.vendelux_http import fetch_html

LOG = logging.getLogger("event-listing.site_scrapers.vendelux")

LIST_URL = "https://vendelux.com/b2b-events"
INSIGHTS_RE = re.compile(
    r"https://vendelux\.com/insights/[a-z0-9-]+-attendee-list",
    re.I,
)
MONTH_PATH_RE = re.compile(r"/b2b-events/[a-z0-9-]+-\d{4}/?$", re.I)

SOURCE_VENDELUX = {
    "name": "Vendelux",
    "url": LIST_URL,
}


def upcoming_month_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        text = anchor.get_text(" ", strip=True)
        if "past" in text.lower():
            continue
        if not MONTH_PATH_RE.search(href):
            continue
        full = urljoin(LIST_URL, href)
        if full not in urls:
            urls.append(full)
    return urls


def insights_from_month_html(html: str) -> dict[str, dict[str, str]]:
    """Map insights URL → fields parsed from listing card text."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(LIST_URL, anchor.get("href") or "")
        if not INSIGHTS_RE.fullmatch(href):
            continue
        meta = parse_listing_anchor(anchor.get_text(" ", strip=True))
        if href not in found:
            found[href] = meta
    return found


def collect_insights_listings(month_urls: list[str], stats: dict[str, Any]) -> dict[str, dict[str, str]]:
    listings: dict[str, dict[str, str]] = {}
    for month_url in month_urls:
        html = fetch_html(month_url)
        stats["pages_visited"] += 1
        batch = insights_from_month_html(html)
        for url, meta in batch.items():
            listings.setdefault(url, {}).update({k: v for k, v in meta.items() if v})
        LOG.info(
            "Vendelux month %s — %d insight links (%d unique total)",
            month_url.rstrip("/").split("/")[-1],
            len(batch),
            len(listings),
        )
    return listings


def _merge_listing(row: dict[str, str], listing: dict[str, str]) -> None:
    for field in (
        "event_name",
        "event_start_date",
        "event_end_date",
        "city",
        "country",
    ):
        if not row.get(field) and listing.get(field):
            row[field] = listing[field]


def enrich_insights_batch(
    insights_urls: list[str],
    listings: dict[str, dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 10,
) -> list[dict[str, str]]:
    source = SOURCE_VENDELUX
    rows: list[dict[str, str]] = []

    def process(insights_url: str) -> dict[str, str]:
        row = {
            **empty_row(source),
            "event_url": insights_url,
        }
        _merge_listing(row, listings.get(insights_url, {}))
        try:
            insights_html = fetch_html(insights_url)
        except Exception as exc:
            LOG.debug("insights failed %s: %s", insights_url, exc)
            row["scrape_status"] = "insights_error"
            row["scrape_error"] = str(exc)
            return row

        app_url = app_event_url_from_insights(insights_html, insights_url)
        if app_url:
            try:
                app_html = fetch_html(app_url)
                stats["detail_pages_visited"] += 1
                detail = parse_app_event_html(app_html, app_url)
                for field, val in detail.items():
                    if val and field != "app_event_url":
                        row[field] = val
                if detail.get("event_domain"):
                    row["domain_source"] = "app_event_jsonld"
                row["official_site_url"] = detail.get("official_site_url") or row.get(
                    "official_site_url", ""
                )
            except Exception as exc:
                LOG.debug("app event failed %s: %s", app_url, exc)
                fallback = parse_insights_fallback(insights_html, insights_url)
                _merge_listing(row, fallback)
        else:
            fallback = parse_insights_fallback(insights_html, insights_url)
            _merge_listing(row, fallback)

        if not row.get("event_name"):
            slug = insights_url.rstrip("/").split("/")[-1]
            row["event_name"] = slug.replace("-attendee-list", "").replace("-", " ").title()
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(process, url) for url in insights_urls]
        for index, future in enumerate(as_completed(futures), start=1):
            rows.append(future.result())
            if index % 100 == 0:
                LOG.info("Vendelux detail progress %d / %d", index, len(insights_urls))

    return rows


def scrape_vendelux(
    *,
    enrich_details: bool = True,
    max_events: int = 0,
    workers: int = 10,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    stats["listing_mode"] = "b2b_events_by_month"

    landing_html = fetch_html(LIST_URL)
    stats["pages_visited"] = 1
    month_urls = upcoming_month_urls(landing_html)
    if not month_urls:
        stats["status"] = "loaded_no_events"
        stats["error"] = "No upcoming month links on b2b-events"
        return [], stats

    listings = collect_insights_listings(month_urls, stats)
    insights_urls = sorted(listings.keys())
    if max_events > 0:
        insights_urls = insights_urls[:max_events]

    LOG.info("Vendelux: %d unique events across %d months", len(insights_urls), len(month_urls))

    if not enrich_details:
        events = []
        for url in insights_urls:
            row = {**empty_row(SOURCE_VENDELUX), "event_url": url}
            _merge_listing(row, listings.get(url, {}))
            events.append(row)
        stats["raw_rows"] = len(events)
        return events, stats

    events = enrich_insights_batch(insights_urls, listings, stats, workers=workers)
    stats["raw_rows"] = len(events)
    return events, stats
