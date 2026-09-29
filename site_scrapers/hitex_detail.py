"""Parse HITEX event detail pages (/events/{slug}/index.html)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS

_COUNTRY_NAMES = {
    "IN": "India",
    "US": "United States",
    "GB": "United Kingdom",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_date(value: Any) -> str:
    text = _clean(value)
    if len(text) >= 10 and re.match(r"20\d{2}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return ""


def _event_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for node in soup.find_all("script", type="application/ld+json"):
        raw = node.string or node.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Event":
            return data
    return {}


def _compact_date_range(start_iso: str, end_iso: str) -> str:
    if not start_iso or not end_iso:
        return ""

    def part(iso: str) -> str:
        dt = datetime.strptime(iso[:10], "%Y-%m-%d")
        return f"{dt.day}{dt.strftime('%b')}{dt.year}"

    return f"{part(start_iso)}-{part(end_iso)}"


def _external_href(href: str) -> str:
    href = (href or "").strip()
    if not href.startswith("http"):
        return ""
    host = host_of(href)
    if not host or host in LISTING_HOSTS:
        return ""
    lower = host.lower()
    if any(x in lower for x in ("facebook.", "instagram.", "linkedin.", "youtube.", "twitter.")):
        return ""
    return href.split("#")[0].rstrip("/")


def _official_site(soup: BeautifulSoup, start_iso: str, end_iso: str) -> str:
    for anchor in soup.find_all("a", href=True):
        if _clean(anchor.get_text()).lower() == "website":
            site = _external_href(anchor["href"])
            if site:
                return site

    compact = _compact_date_range(start_iso, end_iso)
    if compact:
        for anchor in soup.find_all("a", href=True):
            if _clean(anchor.get_text()) == compact:
                site = _external_href(anchor["href"])
                if site:
                    return site
    return ""


def parse_hitex_detail(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    event = _event_json_ld(soup)

    event_name = _clean(event.get("name"))
    if not event_name:
        h1 = soup.find("h1")
        event_name = _clean(h1.get_text()) if h1 else ""

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
            region = _clean(address.get("addressCountry"))
            country = _COUNTRY_NAMES.get(region.upper(), region)

    official = _official_site(soup, start_date, end_date)
    host = host_of(official)
    domain = ""
    if host and host not in LISTING_HOSTS:
        domain = normalize_domain(host)

    return {
        "event_name": event_name,
        "event_start_date": start_date,
        "event_end_date": end_date,
        "venue": venue or "HITEX Exhibition Center",
        "city": city or "Hyderabad",
        "country": country or "India",
        "official_site_url": official,
        "event_domain": domain,
    }
