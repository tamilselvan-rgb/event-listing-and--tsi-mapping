"""Parse Vendelux insights + app/event detail pages."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from elib.scrape_details import official_site_from_html

APP_EVENT_URL_RE = re.compile(
    r"https://vendelux\.com/app/event/[a-z0-9-]+/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)

_MONTHS = {
    "jan": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "may": "05",
    "jun": "06",
    "jul": "07",
    "aug": "08",
    "sep": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _iso_from_text(text: str) -> tuple[str, str]:
    """e.g. Nov 1 to 4, 2026 or Nov 1, 2026."""
    text = _clean(text)
    year_m = re.search(r"(20\d{2})", text)
    if not year_m:
        return "", ""
    year = year_m.group(1)
    month_m = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b",
        text,
        re.I,
    )
    if not month_m:
        return "", ""
    month = _MONTHS[month_m.group(1).lower()[:3]]
    days = re.findall(r"\b(\d{1,2})\b", text)
    if not days:
        return "", ""
    start = f"{year}-{month}-{days[0].zfill(2)}"
    end_day = days[1].zfill(2) if len(days) > 1 else days[0].zfill(2)
    end = f"{year}-{month}-{end_day}"
    return start, end if end != start else ""


def parse_listing_anchor(text: str) -> dict[str, str]:
    """Parse month-page event card link text."""
    text = _clean(text)
    if not text:
        return {}
    start, end = _iso_from_text(text)
    name = text
    month_m = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b",
        text,
        re.I,
    )
    if month_m:
        name = _clean(text[: month_m.start()])
    city = country = ""
    if month_m:
        tail = text[month_m.start() :]
        loc_m = re.search(
            r"(20\d{2})\s*[,\u2013\-]\s*(.+)$",
            tail,
        )
        if loc_m:
            loc = _clean(loc_m.group(2))
            parts = [p.strip() for p in re.split(r",|\u2013", loc) if p.strip()]
            if len(parts) >= 2:
                city = parts[0]
                country = parts[-1]
            elif parts:
                city = parts[0]
    return {
        "event_name": name,
        "event_start_date": start,
        "event_end_date": end,
        "city": city,
        "country": country,
    }


def app_event_url_from_insights(html: str, page_url: str) -> str:
    match = APP_EVENT_URL_RE.search(html)
    if match:
        return match.group(0)
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]"):
        href = _clean(anchor.get("href") or "")
        if not href:
            continue
        full = urljoin(page_url, href)
        if APP_EVENT_URL_RE.search(full):
            return APP_EVENT_URL_RE.search(full).group(0)
        if "view event insight" in _clean(anchor.get_text(" ", strip=True)).lower():
            if "/app/event/" in full:
                return full
    return ""


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


def _place_fields(location: Any) -> tuple[str, str, str]:
    venue = city = country = ""
    places = location if isinstance(location, list) else [location]
    for place in places:
        if not isinstance(place, dict):
            continue
        if place.get("@type") == "VirtualLocation":
            continue
        if not venue:
            venue = _clean(place.get("name"))
        address = place.get("address")
        if isinstance(address, dict):
            city = _clean(address.get("addressLocality")) or city
            country = _clean(address.get("addressCountry")) or country
    if city and "," in city and not country:
        bits = [b.strip() for b in city.split(",")]
        if len(bits) >= 2:
            city, country = bits[0], bits[-1]
    return venue, city, country


def parse_app_event_html(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    event = _event_jsonld(soup)
    if not event:
        return {}

    venue, city, country = _place_fields(event.get("location"))
    official = _clean(event.get("url") or "")
    if not official:
        official = official_site_from_html(html, page_url)
    host = host_of(official)
    domain = normalize_domain(host) if host and host not in LISTING_HOSTS else ""

    start = _clean(event.get("startDate"))[:10]
    end = _clean(event.get("endDate"))[:10]

    return {
        "event_name": _clean(event.get("name")),
        "event_start_date": start,
        "event_end_date": end,
        "venue": venue,
        "city": city,
        "country": country,
        "official_site_url": official,
        "event_domain": domain,
        "app_event_url": page_url,
    }


def parse_insights_fallback(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        title = _clean(og["content"])
    if not title:
        h1 = soup.select_one("h1")
        if h1:
            title = _clean(h1.get_text(" ", strip=True))
    title = re.sub(r"\s+Attendee List\s*$", "", title, flags=re.I)

    text = soup.get_text(" ", strip=True)
    start = end = ""
    runs = re.search(
        r"runs in ([^,]+),\s*([^,]+),\s*([^.]+)\.",
        text,
        re.I,
    )
    city = country = ""
    if runs:
        city = _clean(runs.group(1))
        country = _clean(runs.group(3))

    return {
        "event_name": title,
        "event_start_date": start,
        "event_end_date": end,
        "city": city,
        "country": country,
        "official_site_url": "",
        "event_domain": "",
        "app_event_url": "",
    }
