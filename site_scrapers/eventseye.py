"""Scrape upcoming trade shows from EventsEye."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from elib.scrape_details import merge_detail_into_listing
from site_scrapers.base import base_stats, empty_row
from site_scrapers.eventseye_detail import _us_date_to_iso, parse_eventseye_detail

LOG = logging.getLogger("event-listing.site_scrapers.eventseye")

UPCOMING_URL = "https://www.eventseye.com/fairs/upcoming_trade_shows.html"
FAIR_LINK_RE = re.compile(r"^f-[a-z0-9-]+-\d+-1\.html$", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_EVENTSEYE = {
    "name": "EventsEye Upcoming",
    "url": UPCOMING_URL,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.text


def parse_upcoming_listing(html: str, source: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not FAIR_LINK_RE.match(href.split("/")[-1]):
            continue
        event_url = urljoin(UPCOMING_URL, href)
        if event_url in seen_urls:
            continue
        seen_urls.add(event_url)
        name = _clean(anchor.get_text(" ", strip=True))
        if not name:
            continue
        start_date = ""
        tr = anchor.find_parent("tr")
        if tr:
            for cell in tr.select("td"):
                cell_text = _clean(cell.get_text(" ", strip=True))
                iso = _us_date_to_iso(cell_text)
                if iso:
                    start_date = iso
                    break
        rows.append({
            **empty_row(source),
            "event_name": name,
            "event_start_date": start_date,
            "event_url": event_url,
        })
    return rows


def enrich_eventseye_details(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 12,
    max_details: int = 0,
) -> None:
    urls = list(dict.fromkeys(
        (e.get("event_url") or "").strip() for e in events if (e.get("event_url") or "").strip()
    ))
    if max_details > 0:
        urls = urls[:max_details]
    if not urls:
        return

    LOG.info("EventsEye: fetching %d detail pages", len(urls))

    def fetch_one(url: str) -> tuple[str, str]:
        try:
            return url, fetch_html(url)
        except Exception as exc:
            LOG.debug("EventsEye detail failed %s: %s", url, exc)
            return url, ""

    fetched: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, u) for u in urls]
        for index, future in enumerate(as_completed(futures), start=1):
            url, html = future.result()
            if html:
                fetched[url] = html
            if index % 50 == 0:
                LOG.info("EventsEye detail progress %d / %d", index, len(urls))

    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        url = (event.get("event_url") or "").strip()
        if url in fetched:
            url_to_indices.setdefault(url, []).append(index)

    for detail_url, html in fetched.items():
        detail = parse_eventseye_detail(html, detail_url)
        official = detail.pop("official_site_url", "") or ""
        for idx in url_to_indices.get(detail_url, []):
            events[idx] = merge_detail_into_listing(events[idx], detail, official)

    stats["detail_pages_visited"] = len(fetched)


def scrape_eventseye(
    *,
    enrich: bool = True,
    max_details: int = 0,
    listing_url: str = UPCOMING_URL,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = {**SOURCE_EVENTSEYE, "url": listing_url}
    stats = base_stats()
    stats["listing_mode"] = "upcoming"

    html = fetch_html(listing_url)
    stats["pages_visited"] = 1
    events = parse_upcoming_listing(html, source)
    LOG.info("EventsEye upcoming listing: %d events", len(events))

    if enrich and events:
        enrich_eventseye_details(events, stats, max_details=max_details)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
        stats["error"] = "No EventsEye upcoming fair links found"
    return events, stats
