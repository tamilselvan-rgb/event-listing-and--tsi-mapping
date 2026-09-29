"""Parse Messe Frankfurt fair / event detail pages (interneturl targets)."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from elib.scrape_details import official_site_from_html


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_date(value: Any) -> str:
    text = _clean(value)
    if len(text) >= 10 and re.match(r"20\d{2}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return ""


def _event_from_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for node in soup.find_all("script", type="application/ld+json"):
        raw = node.string or node.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items: list[Any]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            items = data["@graph"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            if types == "Event" or (isinstance(types, list) and "Event" in types):
                return item
            if item.get("startDate") and item.get("name"):
                return item
    return {}


def parse_messefrankfurt_detail(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    event = _event_from_json_ld(soup)

    event_name = _clean(event.get("name"))
    start_date = _iso_date(event.get("startDate"))
    end_date = _iso_date(event.get("endDate"))

    venue = ""
    city = ""
    country = ""
    location = event.get("location")
    if isinstance(location, dict):
        venue = _clean(location.get("name"))
        address = location.get("address")
        if isinstance(address, dict):
            city = _clean(address.get("addressLocality"))
            country = _clean(address.get("addressCountry"))

    official = _clean(event.get("url") or page_url)
    if not official or official == page_url:
        official = official_site_from_html(html, page_url) or page_url

    host = host_of(official)
    domain = ""
    if host and host not in LISTING_HOSTS:
        domain = normalize_domain(host)

    return {
        "event_name": event_name,
        "event_start_date": start_date,
        "event_end_date": end_date,
        "venue": venue,
        "city": city,
        "country": country,
        "official_site_url": official,
        "event_domain": domain,
    }
