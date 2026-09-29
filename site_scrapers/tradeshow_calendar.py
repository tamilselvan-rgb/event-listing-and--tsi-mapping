"""Scrape The Trade Show Calendar listing (paginated, 100 rows per page)."""

from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from site_scrapers.base import base_stats, empty_row

LOG = logging.getLogger("event-listing.site_scrapers.tradeshowcalendar")

LIST_URL = "https://thetradeshowcalendar.com/ttn/index.php"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

SOURCE_TSC = {
    "name": "The Trade Show Calendar",
    "url": LIST_URL,
}

_MONTHS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _normalize_href(href: str, page_url: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(page_url, href)
    return href


def parse_tsc_dates(text: str) -> tuple[str, str]:
    """e.g. SEP/25 - SEP/29/2026 or SEP/30 - SEP/30/2026."""
    text = _clean(text).upper()
    m = re.match(
        r"([A-Z]{3})/(\d{1,2})\s*-\s*([A-Z]{3})/(\d{1,2})/(\d{4})",
        text,
    )
    if not m:
        return "", ""
    sm, sd, em, ed, year = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    if sm not in _MONTHS or em not in _MONTHS:
        return "", ""
    start = f"{year}-{_MONTHS[sm]}-{sd.zfill(2)}"
    end = f"{year}-{_MONTHS[em]}-{ed.zfill(2)}"
    return start, end if end != start else ""


def listing_page_url(offset: int) -> str:
    return (
        f"{LIST_URL}?vShow=search&vRpP={PAGE_SIZE}&vPos={offset}"
    )


def fetch_listing_html(offset: int) -> str:
    response = requests.get(listing_page_url(offset), headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def total_exhibitions(html: str) -> int:
    m = re.search(r"out of\s+([\d,]+)\s+exhibitions", html, re.I)
    if not m:
        return 0
    return int(m.group(1).replace(",", ""))


def parse_listing_page(html: str, source: dict[str, str], page_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, str]] = []
    for row in soup.select("tr.row"):
        name_cell = row.select_one("td.r-Name a[href]")
        if not name_cell:
            continue
        href = _normalize_href(name_cell.get("href") or "", page_url)
        if not href or "thetradeshowcalendar.com" in href.lower():
            continue
        if href.lower().startswith(("javascript:", "mailto:")):
            continue
        name = _clean(name_cell.get_text(" ", strip=True))
        if not name:
            continue
        dates_cell = row.select_one("td.r-Dates")
        city_cell = row.select_one("td.r-City")
        country_cell = row.select_one("td.r-Ctry")
        start, end = parse_tsc_dates(
            dates_cell.get_text(" ", strip=True) if dates_cell else ""
        )
        domain = normalize_domain(host_of(href))
        events.append({
            **empty_row(source),
            "event_name": name,
            "event_start_date": start,
            "event_end_date": end,
            "city": _clean(city_cell.get_text(" ", strip=True)) if city_cell else "",
            "country": _clean(country_cell.get_text(" ", strip=True)) if country_cell else "",
            "event_url": href,
            "official_site_url": href,
            "event_domain": domain,
            "domain_source": "listing_link" if domain else "",
        })
    return events


def scrape_tradeshow_calendar(
    *,
    max_pages: int = 0,
    pause_sec: float = 0.35,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_TSC
    stats = base_stats()
    stats["listing_mode"] = "all_exhibitions"
    all_events: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    html = fetch_listing_html(0)
    total = total_exhibitions(html)
    stats["pages_visited"] = 1
    page_url = listing_page_url(0)
    for event in parse_listing_page(html, source, page_url):
        key = (event["event_name"].lower(), event["event_start_date"], event["event_url"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        all_events.append(event)

    if total <= 0:
        total = len(all_events)

    max_offset = total if total else PAGE_SIZE * 50
    offsets = range(PAGE_SIZE, max_offset, PAGE_SIZE)
    pages_fetched = 1
    for offset in offsets:
        if max_pages and pages_fetched >= max_pages:
            stats["truncated"] = True
            break
        time.sleep(pause_sec)
        page_url = listing_page_url(offset)
        try:
            html = fetch_listing_html(offset)
        except Exception as exc:
            LOG.warning("Trade Show Calendar page vPos=%s failed: %s", offset, exc)
            break
        pages_fetched += 1
        stats["pages_visited"] = pages_fetched
        batch = parse_listing_page(html, source, page_url)
        if not batch:
            LOG.info("No rows at vPos=%s; stopping pagination", offset)
            break
        for event in batch:
            key = (event["event_name"].lower(), event["event_start_date"], event["event_url"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_events.append(event)
        if pages_fetched % 10 == 0:
            LOG.info(
                "Trade Show Calendar: %d pages, %d events (offset %d / ~%d)",
                pages_fetched,
                len(all_events),
                offset,
                total,
            )

    stats["raw_rows"] = len(all_events)
    LOG.info("Trade Show Calendar finished: %d events from %d pages", len(all_events), pages_fetched)
    if not all_events:
        stats["status"] = "loaded_no_events"
    return all_events, stats
