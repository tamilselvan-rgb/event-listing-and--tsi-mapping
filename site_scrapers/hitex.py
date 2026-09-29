"""Scrape https://hitex.co.in/events/upcoming.html (timeline listing + detail pages)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from site_scrapers.base import base_stats, empty_row
from site_scrapers.hitex_detail import parse_hitex_detail

LOG = logging.getLogger("event-listing.site_scrapers.hitex")

BASE = "https://hitex.co.in"
LIST_URL = f"{BASE}/events/upcoming.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_HITEX = {
    "name": "HITEX",
    "url": LIST_URL,
}

_SKIP_HEADINGS = frozenset({"Company", "Events", "Gallery", "Venue", "Resources"})


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def _normalize_event_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    full = urljoin(BASE, href)
    parsed = urlparse(full)
    if parsed.netloc and "hitex.co.in" not in parsed.netloc.lower():
        return ""
    path = parsed.path
    if not re.match(r"/events/[^/]+/index\.html$", path, re.I):
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def event_urls_from_listing(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []

    for heading in soup.find_all("h3"):
        name = _clean(heading.get_text())
        if not name or name in _SKIP_HEADINGS:
            continue
        container = heading.find_parent("div")
        event_url = ""
        if container:
            for anchor in container.find_all("a", href=True):
                event_url = _normalize_event_url(anchor["href"])
                if event_url:
                    break
        if not event_url:
            parent_link = heading.find_parent("a", href=True)
            if parent_link:
                event_url = _normalize_event_url(parent_link["href"])
        if event_url and event_url not in seen:
            seen.add(event_url)
            urls.append(event_url)

    if not urls:
        for anchor in soup.find_all("a", href=True):
            event_url = _normalize_event_url(anchor["href"])
            if event_url and event_url not in seen:
                seen.add(event_url)
                urls.append(event_url)

    return urls


def row_from_detail(detail: dict[str, str], event_url: str, source: dict[str, str]) -> dict[str, str]:
    row = empty_row(source)
    row.update(detail)
    row["event_url"] = event_url
    if row.get("event_domain"):
        row["domain_source"] = "detail_official_site"
    return row


def scrape_hitex(
    *,
    list_url: str = LIST_URL,
    enrich_details: bool = True,
    max_details: int = 0,
    workers: int = 8,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = {**SOURCE_HITEX, "url": list_url}
    stats = base_stats()
    stats["listing_mode"] = "upcoming_timeline_html"

    try:
        html = fetch_html(list_url)
    except Exception as exc:
        LOG.warning("HITEX listing failed: %s", exc)
        stats["error"] = str(exc)
        stats["status"] = "failed"
        return [], stats

    stats["pages_visited"] = 1
    all_urls = event_urls_from_listing(html)
    LOG.info("HITEX listing — %d event URLs", len(all_urls))

    if max_details > 0:
        all_urls = all_urls[:max_details]

    events: list[dict[str, str]] = []
    if enrich_details and all_urls:
        LOG.info("HITEX: fetching %d event detail pages", len(all_urls))

        def fetch_one(event_url: str) -> tuple[str, str]:
            try:
                return event_url, fetch_html(event_url)
            except Exception as exc:
                LOG.debug("detail failed %s: %s", event_url, exc)
                return event_url, ""

        fetched = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(fetch_one, url) for url in all_urls]
            for future in as_completed(futures):
                event_url, detail_html = future.result()
                if not detail_html:
                    row = empty_row(source, status="detail_failed")
                    row["event_url"] = event_url
                    events.append(row)
                    continue
                fetched += 1
                detail = parse_hitex_detail(detail_html, event_url)
                events.append(row_from_detail(detail, event_url, source))

        stats["detail_pages_visited"] = fetched
        events.sort(key=lambda r: (r.get("event_start_date") or "9999", r.get("event_name") or ""))
    else:
        for event_url in all_urls:
            row = empty_row(source)
            row["event_url"] = event_url
            events.append(row)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
    return events, stats
