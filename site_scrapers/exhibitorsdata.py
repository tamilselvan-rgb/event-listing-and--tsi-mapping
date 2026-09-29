"""Scrape https://exhibitorsdata.com/events via dashboard API + product detail pages."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from typing import Any

import requests

from elib.event_domains import host_of, normalize_domain
from site_scrapers.base import base_stats, empty_row
from site_scrapers.exhibitorsdata_detail import (
    official_site_from_description,
    parse_location,
    parse_product_detail_html,
)

LOG = logging.getLogger("event-listing.site_scrapers.exhibitorsdata")

LIST_URL = "https://exhibitorsdata.com/events"
API_URL = "https://dashboard.exhibitorsdata.com/api/v1/product-categories/get-products-new"
PRODUCT_BASE = "https://exhibitorsdata.com/product"
PAGE_SIZE = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://exhibitorsdata.com",
    "Referer": LIST_URL,
}

SOURCE_EXHIBITORS_DATA = {
    "name": "Exhibitors Data",
    "url": LIST_URL,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_date(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
        return m.group(1) if m else ""


def fetch_products_page(page: int, limit: int = PAGE_SIZE) -> dict[str, Any]:
    body = {
        "filters": [],
        "page": page,
        "limit": limit,
        "search_keyword": "",
        "from_year": 0,
        "end_year": 0,
        "from_month": 0,
        "end_month": 0,
        "product_type": "",
    }
    response = requests.post(API_URL, json=body, headers=HEADERS, timeout=90)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("message") or "API error")
    return payload["data"]


def row_from_item(item: dict[str, Any], source: dict[str, str]) -> dict[str, str]:
    slug = _clean(item.get("slug"))
    product_url = f"{PRODUCT_BASE}/{slug}/" if slug else ""
    venue, city, country = parse_location(_clean(item.get("location")))
    description = item.get("description") or ""
    official = official_site_from_description(description)
    domain = normalize_domain(host_of(official) if official else "")
    return {
        **empty_row(source),
        "event_name": _clean(item.get("event_name")),
        "event_start_date": _iso_date(item.get("events_date_start") or ""),
        "event_end_date": _iso_date(item.get("events_date_end") or ""),
        "venue": venue,
        "city": city,
        "country": country,
        "event_url": product_url,
        "official_site_url": official,
        "event_domain": domain,
        "domain_source": "description_link" if domain else "",
    }


def enrich_missing_domains(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 12,
) -> None:
    todo = [
        (i, e)
        for i, e in enumerate(events)
        if not (e.get("event_domain") or "").strip() and (e.get("event_url") or "").strip()
    ]
    if not todo:
        return
    LOG.info("Exhibitors Data: fetching %d product pages for missing domains", len(todo))

    def fetch_one(url: str) -> tuple[str, str]:
        try:
            resp = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
            resp.raise_for_status()
            return url, resp.text
        except Exception as exc:
            LOG.debug("product page failed %s: %s", url, exc)
            return url, ""

    url_to_indices: dict[str, list[int]] = {}
    for index, event in todo:
        url = event["event_url"]
        url_to_indices.setdefault(url, []).append(index)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, url) for url in url_to_indices]
        for future in as_completed(futures):
            url, html = future.result()
            if not html:
                continue
            official = parse_product_detail_html(html)
            if not official:
                continue
            domain = normalize_domain(host_of(official))
            if not domain:
                continue
            for idx in url_to_indices.get(url, []):
                events[idx]["official_site_url"] = official
                events[idx]["event_domain"] = domain
                events[idx]["domain_source"] = "product_page"

    stats["detail_pages_visited"] = len(url_to_indices)


def scrape_exhibitorsdata(
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = 0,
    enrich_details: bool = True,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_EXHIBITORS_DATA
    stats = base_stats()
    stats["listing_mode"] = "api_products"
    events: list[dict[str, str]] = []
    seen: set[str] = set()

    first = fetch_products_page(1, limit=page_size)
    total_pages = int(first.get("totalPages") or 1)
    stats["pages_visited"] = 0

    def consume(data: dict[str, Any]) -> None:
        for item in data.get("items") or []:
            row = row_from_item(item, source)
            key = row.get("event_url") or row.get("event_name")
            if not key or key in seen:
                continue
            seen.add(key)
            events.append(row)

    consume(first)
    stats["pages_visited"] = 1
    LOG.info(
        "Exhibitors Data page 1/%d (%d items, total %s)",
        total_pages,
        len(first.get("items") or []),
        first.get("totalItems"),
    )

    last_page = total_pages if not max_pages else min(total_pages, max_pages)
    for page in range(2, last_page + 1):
        data = fetch_products_page(page, limit=page_size)
        consume(data)
        stats["pages_visited"] = page
        if page % 5 == 0 or page == last_page:
            LOG.info("Exhibitors Data page %d/%d — %d events", page, last_page, len(events))

    if enrich_details:
        enrich_missing_domains(events, stats)

    stats["raw_rows"] = len(events)
    if max_pages and last_page < total_pages:
        stats["truncated"] = True
    if not events:
        stats["status"] = "loaded_no_events"
    return events, stats
