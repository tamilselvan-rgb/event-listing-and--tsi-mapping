"""Scrape https://exhibitionsvoice.com/tradeshows (paginated Next.js listing)."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from site_scrapers.base import base_stats, empty_row
from site_scrapers.exhibitionsvoice_detail import parse_detail_next_data

LOG = logging.getLogger("event-listing.site_scrapers.exhibitionsvoice")

LIST_URL = "https://exhibitionsvoice.com/tradeshows"
EVENT_PATH = "/event"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_EXHIBITIONS_VOICE = {
    "name": "Exhibitions Voice",
    "url": LIST_URL,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_date(value: Any) -> str:
    text = _clean(value)
    if len(text) >= 10 and re.match(r"20\d{2}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return ""


def _country_from_venue(venue: dict[str, Any]) -> str:
    country_obj = venue.get("country")
    if isinstance(country_obj, dict):
        return _clean(country_obj.get("value") or country_obj.get("label"))
    return _clean(country_obj)


def listing_page_url(page: int) -> str:
    if page <= 1:
        return LIST_URL
    return f"{LIST_URL}?page={page}"


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def page_props_from_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if not node or not node.string:
        return {}
    return json.loads(node.string).get("props", {}).get("pageProps", {})


def row_from_listing_item(item: dict[str, Any], source: dict[str, str]) -> dict[str, str]:
    venue = item.get("venue") if isinstance(item.get("venue"), dict) else {}
    slug = _clean(item.get("slug"))
    event_url = urljoin(LIST_URL, f"{EVENT_PATH}/{slug}") if slug else ""
    official = _clean(venue.get("website") or "")
    host = host_of(official)
    domain = normalize_domain(host) if host and host not in LISTING_HOSTS else ""
    return {
        **empty_row(source),
        "event_name": _clean(item.get("title")),
        "event_start_date": _iso_date(item.get("start_date")),
        "event_end_date": _iso_date(item.get("end_date")),
        "venue": _clean(venue.get("name")),
        "city": _clean(venue.get("city")),
        "country": _country_from_venue(venue),
        "event_url": event_url,
        "official_site_url": official,
        "event_domain": domain,
        "domain_source": "listing_venue" if domain else "",
    }


def enrich_from_detail_pages(
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

    LOG.info("Exhibitions Voice: fetching %d event detail pages", len(urls))

    def fetch_one(url: str) -> tuple[str, str]:
        try:
            return url, fetch_html(url)
        except Exception as exc:
            LOG.debug("detail failed %s: %s", url, exc)
            return url, ""

    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        url = (event.get("event_url") or "").strip()
        if url in urls:
            url_to_indices.setdefault(url, []).append(index)

    fetched = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, url) for url in url_to_indices]
        for future in as_completed(futures):
            url, html = future.result()
            if not html:
                continue
            fetched += 1
            detail = parse_detail_next_data(html, url)
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
                    events[idx]["domain_source"] = "detail_official_site"

    stats["detail_pages_visited"] = fetched


def scrape_exhibitionsvoice(
    *,
    max_pages: int = 0,
    enrich_details: bool = True,
    max_details: int = 0,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_EXHIBITIONS_VOICE
    stats = base_stats()
    stats["listing_mode"] = "tradeshows_next_data"
    events: list[dict[str, str]] = []
    seen_slugs: set[str] = set()

    page = 1
    total_pages = 1

    while page <= total_pages:
        if max_pages > 0 and page > max_pages:
            break
        url = listing_page_url(page)
        try:
            html = fetch_html(url)
        except Exception as exc:
            LOG.warning("Exhibitions Voice page %d failed: %s", page, exc)
            stats["error"] = str(exc)
            if not events:
                stats["status"] = "failed"
            break

        stats["pages_visited"] += 1
        props = page_props_from_html(html)
        if not props:
            break

        total = int(props.get("total") or 0)
        page_size = int(props.get("pageSize") or 20)
        if total and page_size:
            total_pages = max(1, (total + page_size - 1) // page_size)
        elif page == 1:
            total_pages = 1

        batch = props.get("result") or []
        for item in batch:
            if not isinstance(item, dict):
                continue
            row = row_from_listing_item(item, source)
            slug = _clean(item.get("slug"))
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            if row.get("event_name") or row.get("event_url"):
                events.append(row)

        LOG.info(
            "Exhibitions Voice page %d/%d — %d events",
            page,
            total_pages,
            len(events),
        )
        page += 1

    if enrich_details and events:
        enrich_from_detail_pages(events, stats, max_details=max_details)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
    return events, stats
