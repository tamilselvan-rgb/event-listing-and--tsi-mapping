"""Fetch event detail pages and merge core fields + official website domain."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from elib.domain_enrich import infer_domain_from_urls
from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS

LOG = logging.getLogger("event-listing.scrape_details")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MERGE_FIELDS = (
    "event_name",
    "event_start_date",
    "event_end_date",
    "venue",
    "city",
    "country",
    "official_site_url",
)

WEBSITE_LINK = re.compile(r"web\s*site|official\s*site|organizer|visit\s+(the\s+)?site", re.I)


def _clean(text: object) -> str:
    return re.sub(r"\s+", " ", unescape(str(text or ""))).strip()


def _external_href(href: str, page_url: str) -> str:
    href = (href or "").strip()
    if not href or href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    full = urljoin(page_url, href)
    host = host_of(full)
    if not host or host in LISTING_HOSTS:
        return ""
    return full


def official_site_from_html(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text() or "{}")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            items = data["@graph"]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            if types != "Event" and not (isinstance(types, list) and "Event" in types):
                continue
            for candidate in (item.get("url"), (item.get("organizer") or {}).get("url")):
                if isinstance(candidate, str):
                    site = _external_href(candidate, page_url)
                    if site:
                        return site
                if isinstance(item.get("organizer"), dict):
                    site = _external_href(item["organizer"].get("url", ""), page_url)
                    if site:
                        return site
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        text = _clean(anchor.get_text(" ", strip=True))
        if WEBSITE_LINK.search(text):
            site = _external_href(href, page_url)
            if site:
                return site
    if "eventseye.com" in page_url.lower():
        for anchor in soup.select("a[href^='http']"):
            site = _external_href(anchor.get("href") or "", page_url)
            if site and not re.search(r"(facebook|linkedin|twitter|instagram|youtube|eventseye)", site, re.I):
                return site
    return ""


def _apply_domain_fields(event: dict[str, str]) -> None:
    dom = normalize_domain(infer_domain_from_urls(event) or "")
    if not dom:
        dom = normalize_domain(host_of(event.get("official_site_url") or ""))
    if dom and dom not in LISTING_HOSTS:
        event["event_domain"] = dom
        event["domain_source"] = event.get("domain_source") or "detail_page"
    else:
        event["event_domain"] = ""
        if event.get("domain_source") == "detail_page":
            event["domain_source"] = ""


def merge_detail_into_listing(listing: dict[str, str], detail: dict[str, str], official_site: str) -> dict[str, str]:
    merged = dict(listing)
    for field in MERGE_FIELDS:
        if detail.get(field):
            merged[field] = detail[field]
    if official_site:
        merged["official_site_url"] = official_site
    _apply_domain_fields(merged)
    return merged


def enrich_events_from_detail_pages(
    events: list[dict[str, str]],
    source: dict[str, str],
    stats: dict,
    *,
    is_detail_url: Callable[[str, str], bool],
    parse_events: Callable[[str, str], list[dict[str, str]]],
    max_details: int = 0,
    workers: int = 12,
) -> list[dict[str, str]]:
    """Visit each listing's detail URL; merge name, dates, venue, city, country, official domain."""
    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        if event.get("scrape_status") != "ok":
            continue
        url = (event.get("event_url") or "").strip()
        if not url or not is_detail_url(url, source["name"]):
            continue
        url_to_indices.setdefault(url, []).append(index)

    detail_urls = list(url_to_indices.keys())
    if max_details and max_details > 0:
        detail_urls = detail_urls[:max_details]

    if not detail_urls:
        return events

    LOG.info("%s: enriching %d event detail pages...", source["name"], len(detail_urls))

    def fetch_one(detail_url: str) -> tuple[str, str]:
        try:
            resp = requests.get(detail_url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            return detail_url, resp.text
        except Exception as exc:
            LOG.debug("Detail fetch failed %s: %s", detail_url, exc)
            return detail_url, ""

    fetched: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(fetch_one, url) for url in detail_urls]
        for index, future in enumerate(as_completed(futures), start=1):
            url, html = future.result()
            if html:
                fetched[url] = html
            if index % 500 == 0:
                LOG.info("%s: detail pages %d / %d", source["name"], index, len(detail_urls))

    enriched_count = 0
    for detail_url, html in fetched.items():
        page_events = parse_events(html, detail_url)
        detail = max(page_events, key=lambda row: len(row.get("event_name") or "")) if page_events else {}
        official = official_site_from_html(html, detail_url)
        for idx in url_to_indices.get(detail_url, []):
            events[idx] = merge_detail_into_listing(events[idx], detail, official)
            enriched_count += 1

    stats["detail_pages_visited"] = stats.get("detail_pages_visited", 0) + len(fetched)
    LOG.info("%s: merged detail into %d listing rows", source["name"], enriched_count)
    return events
