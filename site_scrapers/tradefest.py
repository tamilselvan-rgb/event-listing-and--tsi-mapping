"""Scrape https://tradefest.io/en/search (paginated Next.js / Apollo listing)."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from site_scrapers.base import base_stats, empty_row
from site_scrapers.tradefest_detail import apollo_from_html, parse_tradefest_detail

LOG = logging.getLogger("event-listing.site_scrapers.tradefest")

SEARCH_BASE = "https://tradefest.io/en/search"
EVENT_BASE = "https://tradefest.io/en/event"
PAGE_SIZE = 30
MAX_PAGES = 400

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_TRADEFEST = {
    "name": "Tradefest",
    "url": f"{SEARCH_BASE}?futureOnly=true",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def search_url(page: int, *, future_only: bool = True) -> str:
    flag = "true" if future_only else "false"
    return f"{SEARCH_BASE}?futureOnly={flag}&page={page}"


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def total_count_from_apollo(apollo: dict[str, Any]) -> int | None:
    root = apollo.get("ROOT_QUERY", {})
    for key, value in root.items():
        if key.startswith("events(") and isinstance(value, dict):
            total = value.get("totalCount")
            if isinstance(total, int):
                return total
    return None


def _official_from_url_field(official_url: str) -> tuple[str, str]:
    official = _clean(official_url)
    if not official:
        return "", ""
    host = host_of(official)
    if not host or host in LISTING_HOSTS:
        return official, ""
    return official, normalize_domain(host)


def events_from_search_html(html: str, source: dict[str, str]) -> list[dict[str, str]]:
    apollo = apollo_from_html(html)
    rows: list[dict[str, str]] = []
    for key, value in apollo.items():
        if not key.startswith("Event:") or not isinstance(value, dict):
            continue
        slug = _clean(value.get("slug"))
        if not slug:
            continue
        venue, city, country = "", "", ""
        venue_ref = value.get("venue")
        if isinstance(venue_ref, dict) and venue_ref.get("__ref"):
            venue_obj = apollo.get(venue_ref["__ref"], {})
            if isinstance(venue_obj, dict):
                venue = _clean(venue_obj.get("name"))
                location_ref = venue_obj.get("location")
                if isinstance(location_ref, dict) and location_ref.get("__ref"):
                    location = apollo.get(location_ref["__ref"], {})
                    if isinstance(location, dict):
                        city = _clean(location.get("localizedCity"))
                        country = _clean(location.get("localizedCountry"))
        official, domain = _official_from_url_field(value.get("url") or "")
        event_url = urljoin(source["url"], f"/en/event/{slug}")
        rows.append({
            **empty_row(source),
            "event_name": _clean(value.get("name")),
            "event_start_date": _clean(value.get("startDate"))[:10],
            "event_end_date": _clean(value.get("endDate"))[:10],
            "venue": venue,
            "city": city,
            "country": country,
            "event_url": event_url,
            "official_site_url": official,
            "event_domain": domain,
            "domain_source": "listing_apollo" if domain else "",
        })
    return rows


def is_upcoming(row: dict[str, str]) -> bool:
    today = date.today()
    for field in ("event_end_date", "event_start_date"):
        text = _clean(row.get(field, ""))[:10]
        if not text:
            continue
        try:
            if date.fromisoformat(text) >= today:
                return True
        except ValueError:
            continue
    return False


def enrich_missing_from_detail(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 12,
    max_details: int = 0,
) -> None:
    todo_urls = list(dict.fromkeys(
        (e.get("event_url") or "").strip()
        for e in events
        if not (e.get("event_domain") or "").strip() and (e.get("event_url") or "").strip()
    ))
    if max_details > 0:
        todo_urls = todo_urls[:max_details]
    if not todo_urls:
        return

    LOG.info("Tradefest: fetching %d detail pages for missing domains", len(todo_urls))

    def fetch_one(url: str) -> tuple[str, str]:
        try:
            return url, fetch_html(url)
        except Exception as exc:
            LOG.debug("Tradefest detail failed %s: %s", url, exc)
            return url, ""

    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        if (event.get("event_domain") or "").strip():
            continue
        url = (event.get("event_url") or "").strip()
        if url in todo_urls:
            url_to_indices.setdefault(url, []).append(index)

    fetched = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, url) for url in url_to_indices]
        for future in as_completed(futures):
            url, html = future.result()
            if not html:
                continue
            fetched += 1
            detail = parse_tradefest_detail(html)
            if not detail:
                continue
            for idx in url_to_indices.get(url, []):
                for field in (
                    "event_name",
                    "event_start_date",
                    "event_end_date",
                    "venue",
                    "city",
                    "country",
                    "official_site_url",
                    "event_domain",
                ):
                    val = detail.get(field) or ""
                    if val:
                        events[idx][field] = val
                if detail.get("event_domain"):
                    events[idx]["domain_source"] = "detail_apollo"

    stats["detail_pages_visited"] = fetched


def scrape_tradefest(
    *,
    future_only: bool = True,
    max_pages: int = 0,
    enrich_details: bool = True,
    max_details: int = 0,
    page_delay: float = 0.15,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_TRADEFEST
    stats = base_stats()
    stats["listing_mode"] = "search_apollo"
    events: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    total_count: int | None = None
    page = 1
    page_cap = max_pages if max_pages > 0 else MAX_PAGES

    while page <= page_cap:
        url = search_url(page, future_only=future_only)
        try:
            html = fetch_html(url)
        except Exception as exc:
            LOG.warning("Tradefest page %d failed: %s", page, exc)
            stats["error"] = str(exc)
            if not events:
                stats["status"] = "failed"
            break

        stats["pages_visited"] += 1
        if total_count is None:
            total_count = total_count_from_apollo(apollo_from_html(html))

        page_events = events_from_search_html(html, source)
        new_rows = 0
        for row in page_events:
            if future_only and not is_upcoming(row):
                continue
            slug = (row.get("event_url") or "").rstrip("/").split("/")[-1]
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            events.append(row)
            new_rows += 1

        if page == 1 or page % 25 == 0:
            LOG.info(
                "Tradefest page %d — %d events (total reported %s)",
                page,
                len(events),
                total_count if total_count is not None else "?",
            )

        if new_rows == 0:
            break
        if total_count is not None and len(seen_slugs) >= total_count:
            break
        if max_pages > 0 and page >= max_pages:
            break

        page += 1
        if page_delay > 0:
            time.sleep(page_delay)

    if enrich_details and events:
        enrich_missing_from_detail(events, stats, max_details=max_details)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
    elif stats.get("truncated"):
        pass
    return events, stats
