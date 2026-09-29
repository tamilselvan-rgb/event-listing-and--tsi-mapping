"""Scrape https://nice-nusantaraconvex.com/events (paginated listing + detail pages)."""

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
from site_scrapers.nice_convex_detail import parse_nice_detail

LOG = logging.getLogger("event-listing.site_scrapers.nice_convex")

BASE = "https://nice-nusantaraconvex.com"
DEFAULT_LIST_URL = f"{BASE}/events?status=upcoming&year=2026"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_NICE_CONVEX = {
    "name": "Nice Nusantara Convex",
    "url": DEFAULT_LIST_URL,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def listing_page_url(page: int, *, list_url: str = DEFAULT_LIST_URL) -> str:
    if page <= 1:
        return list_url
    base = unescape(list_url)
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}page={page}"


def _normalize_event_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    full = urljoin(BASE, href)
    parsed = urlparse(full)
    if parsed.netloc and "nice-nusantaraconvex.com" not in parsed.netloc.lower():
        return ""
    path = parsed.path.rstrip("/")
    if not path.startswith("/events/") or path == "/events":
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def event_urls_from_listing_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        normalized = _normalize_event_url(anchor["href"])
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def max_page_from_html(html: str) -> int:
    text = unescape(html)
    pages = [1]
    for match in re.finditer(r"[?&]page=(\d+)", text):
        pages.append(int(match.group(1)))
    return max(pages)


def row_from_detail(detail: dict[str, str], event_url: str, source: dict[str, str]) -> dict[str, str]:
    row = empty_row(source)
    row.update(detail)
    row["event_url"] = event_url
    if row.get("event_domain"):
        row["domain_source"] = "detail_official_site"
    return row


def scrape_nice_convex(
    *,
    list_url: str = DEFAULT_LIST_URL,
    max_pages: int = 0,
    enrich_details: bool = True,
    max_details: int = 0,
    workers: int = 8,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = {**SOURCE_NICE_CONVEX, "url": list_url}
    stats = base_stats()
    stats["listing_mode"] = "events_html_pagination"

    all_urls: list[str] = []
    seen_urls: set[str] = set()
    total_pages = 1
    page = 1

    while page <= total_pages:
        if max_pages > 0 and page > max_pages:
            break
        url = listing_page_url(page, list_url=list_url)
        try:
            html = fetch_html(url)
        except Exception as exc:
            LOG.warning("NICE listing page %d failed: %s", page, exc)
            stats["error"] = str(exc)
            if not all_urls:
                stats["status"] = "failed"
            break

        stats["pages_visited"] += 1
        if page == 1:
            total_pages = max_page_from_html(html)

        batch = event_urls_from_listing_html(html)
        for event_url in batch:
            if event_url not in seen_urls:
                seen_urls.add(event_url)
                all_urls.append(event_url)

        LOG.info("NICE page %d/%d — %d event URLs", page, total_pages, len(all_urls))
        page += 1

    if max_details > 0:
        all_urls = all_urls[:max_details]

    events: list[dict[str, str]] = []
    if enrich_details and all_urls:
        LOG.info("NICE: fetching %d event detail pages", len(all_urls))

        def fetch_one(event_url: str) -> tuple[str, str]:
            try:
                return event_url, fetch_html(event_url)
            except Exception as exc:
                LOG.debug("detail failed %s: %s", event_url, exc)
                return event_url, ""

        fetched = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(fetch_one, u) for u in all_urls]
            for future in as_completed(futures):
                event_url, html = future.result()
                if not html:
                    row = empty_row(source, status="detail_failed")
                    row["event_url"] = event_url
                    events.append(row)
                    continue
                fetched += 1
                detail = parse_nice_detail(html, event_url)
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
