"""Parse Expomap event detail pages (/en/expo/{slug}/)."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS

OFFICIAL_LINK = re.compile(
    r"official\s*website|go\s+to\s+official|официальн",
    re.I,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_from_event_date(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", text)
    return m.group(0) if m else text[:10]


def _event_jsonld(soup: BeautifulSoup) -> dict[str, Any]:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text() or "{}")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Event":
                return item
    return {}


def official_website_from_detail(soup: BeautifulSoup, page_url: str) -> str:
    for anchor in soup.select("a[href]"):
        href = _clean(anchor.get("href") or "")
        if not href.startswith("http"):
            continue
        text = _clean(anchor.get_text(" ", strip=True))
        if OFFICIAL_LINK.search(text):
            host = host_of(href)
            if host and host not in LISTING_HOSTS:
                return href
    return ""


def parse_expomap_detail(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    event = _event_jsonld(soup)
    official = official_website_from_detail(soup, page_url)

    venue = city = country = ""
    location = event.get("location") if isinstance(event, dict) else None
    if isinstance(location, dict):
        venue = _clean(location.get("name"))
        address = location.get("address")
        if isinstance(address, dict):
            city = _clean(address.get("addressLocality"))
            country = _clean(address.get("addressCountry"))

    start = _iso_from_event_date(_clean(event.get("startDate") if event else ""))
    end = _iso_from_event_date(_clean(event.get("endDate") if event else ""))
    name = _clean(event.get("name") if event else "")

    host = host_of(official)
    domain = normalize_domain(host) if host and host not in LISTING_HOSTS else ""

    return {
        "event_name": name,
        "event_start_date": start,
        "event_end_date": end,
        "venue": venue,
        "city": city,
        "country": country,
        "official_site_url": official,
        "event_domain": domain,
    }
