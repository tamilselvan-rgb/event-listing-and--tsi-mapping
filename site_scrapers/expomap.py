"""Scrape https://expomap.ru/en/expo/ (paginated exhibition catalog)."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from site_scrapers.base import base_stats, empty_row
from site_scrapers.expomap_detail import parse_expomap_detail

LOG = logging.getLogger("event-listing.site_scrapers.expomap")

LIST_URL = "https://expomap.ru/en/expo/"
PAGE_SIZE = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SOURCE_EXPOMAP = {
    "name": "Expomap",
    "url": LIST_URL,
}

DATE_RANGE_RE = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{2})\s*[—–\-]\s*(\d{2})\.(\d{2})\.(\d{2})",
)
LOCATION_RE = re.compile(
    r"^([A-Za-zÀ-ÿ][^,]+),\s*(.+)$",
)

RESERVED_SLUGS = frozenset({"theme", "city", "country", "tag"})


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _ddmmyy_to_iso(day: str, month: str, year2: str) -> str:
    y = int(year2)
    year = 2000 + y if y < 100 else y
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def listing_page_url(page: int) -> str:
    if page <= 1:
        return LIST_URL
    return f"{LIST_URL}?page={page}"


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=90)
    response.raise_for_status()
    return response.text


def is_event_slug_path(href: str) -> bool:
    path = urlparse(urljoin(LIST_URL, href)).path.strip("/")
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "en" or parts[1] != "expo":
        return False
    slug = parts[2]
    if slug in RESERVED_SLUGS or re.fullmatch(r"\d{4}", slug):
        return False
    return True


def parse_card_text(card_text: str) -> dict[str, str]:
    lines = [_clean(line) for line in card_text.split("\n") if _clean(line)]
    out: dict[str, str] = {}
    for line in lines:
        dm = DATE_RANGE_RE.search(line)
        if dm and not out.get("event_start_date"):
            out["event_start_date"] = _ddmmyy_to_iso(dm.group(1), dm.group(2), dm.group(3))
            end = _ddmmyy_to_iso(dm.group(4), dm.group(5), dm.group(6))
            out["event_end_date"] = end if end != out["event_start_date"] else ""
            continue
        if "," in line and not line.startswith("#") and not out.get("country"):
            loc = LOCATION_RE.match(line)
            if loc:
                out["country"] = _clean(loc.group(1))
                out["city"] = _clean(loc.group(2))
    for line in lines:
        if line.startswith("#") or DATE_RANGE_RE.search(line):
            continue
        if line.lower() == "details":
            continue
        if "," in line and out.get("country"):
            continue
        if not out.get("event_name") and len(line) > 2:
            out["event_name"] = line
            break
    return out


def parse_listing_page(html: str, source: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for details in soup.find_all("a", string=re.compile(r"^\s*Details\s*$", re.I)):
        href = details.get("href") or ""
        if not is_event_slug_path(href):
            continue
        event_url = urljoin(LIST_URL, href)
        if event_url in seen:
            continue
        seen.add(event_url)

        card = details.find_parent("li")
        if not card:
            card = details.find_parent("div", recursive=True)
        card_text = card.get_text("\n", strip=True) if card else ""
        meta = parse_card_text(card_text)

        rows.append({
            **empty_row(source),
            "event_name": meta.get("event_name", ""),
            "event_start_date": meta.get("event_start_date", ""),
            "event_end_date": meta.get("event_end_date", ""),
            "city": meta.get("city", ""),
            "country": meta.get("country", ""),
            "event_url": event_url,
        })
    return rows


def total_pages_from_html(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    last = 1
    for anchor in soup.select("a[href*='page=']"):
        m = re.search(r"page=(\d+)", anchor.get("href") or "")
        if m:
            last = max(last, int(m.group(1)))
    return last


def enrich_expomap_details(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 12,
    max_details: int = 0,
) -> None:
    indices = list(range(len(events)))
    if max_details > 0:
        indices = indices[:max_details]

    def fetch_one(idx: int) -> tuple[int, dict[str, str]]:
        url = (events[idx].get("event_url") or "").strip()
        if not url:
            return idx, {}
        try:
            html = fetch_html(url)
            return idx, parse_expomap_detail(html, url)
        except Exception as exc:
            LOG.debug("Expomap detail failed %s: %s", url, exc)
            return idx, {}

    LOG.info("Expomap: fetching %d detail pages", len(indices))
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, i) for i in indices]
        for future in as_completed(futures):
            idx, detail = future.result()
            if detail:
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
                    events[idx]["domain_source"] = "official_website_link"
            done += 1
            if done % 200 == 0:
                LOG.info("Expomap detail progress %d / %d", done, len(indices))

    stats["detail_pages_visited"] = done


def scrape_expomap(
    *,
    max_pages: int = 0,
    enrich_details: bool = True,
    max_details: int = 0,
    page_delay: float = 0.25,
    workers: int = 12,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_EXPOMAP
    stats = base_stats()
    stats["listing_mode"] = "expo_catalog"
    events: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    html = fetch_html(listing_page_url(1))
    stats["pages_visited"] = 1
    total_pages = total_pages_from_html(html)
    if max_pages > 0:
        total_pages = min(total_pages, max_pages)

    def consume(page_html: str) -> int:
        batch = parse_listing_page(page_html, source)
        added = 0
        for row in batch:
            url = row.get("event_url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            events.append(row)
            added += 1
        return added

    consume(html)
    LOG.info("Expomap page 1/%d — %d events", total_pages, len(events))

    for page in range(2, total_pages + 1):
        if page_delay > 0:
            time.sleep(page_delay)
        try:
            page_html = fetch_html(listing_page_url(page))
        except Exception as exc:
            LOG.warning("Expomap page %d failed: %s", page, exc)
            stats["error"] = str(exc)
            break
        stats["pages_visited"] += 1
        added = consume(page_html)
        if page % 25 == 0 or page == total_pages:
            LOG.info("Expomap page %d/%d — %d events (+%d)", page, total_pages, len(events), added)
        if added == 0:
            LOG.info("Expomap: no new events on page %d; stopping", page)
            break

    if enrich_details and events:
        enrich_expomap_details(events, stats, workers=workers, max_details=max_details)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
    elif max_pages > 0 and total_pages > max_pages:
        stats["truncated"] = True
    return events, stats
