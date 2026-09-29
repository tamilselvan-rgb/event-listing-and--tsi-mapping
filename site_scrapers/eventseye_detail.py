"""Parse EventsEye fair detail pages (/fairs/f-*-1.html)."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

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


def _us_date_to_iso(text: str) -> str:
    """MM/DD/YYYY -> YYYY-MM-DD."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return ""
    month, day, year = m.group(1), m.group(2), m.group(3)
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _parse_eventseye_range(text: str) -> tuple[str, str]:
    """e.g. Nov. 02 - 05, 2026 or Nov. 02, 2026."""
    text = _clean(text)
    year_m = re.search(r"(20\d{2})", text)
    year = year_m.group(1) if year_m else ""
    month_m = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?",
        text,
        re.I,
    )
    if not month_m or not year:
        return _us_date_to_iso(text), ""
    month = _MONTHS[month_m.group(1).lower()[:3]]
    days = re.findall(r"\b(\d{1,2})\b", text)
    if not days:
        return "", ""
    start_day = days[0].zfill(2)
    end_day = (days[1] if len(days) > 1 else days[0]).zfill(2)
    start = f"{year}-{month}-{start_day}"
    end = f"{year}-{month}-{end_day}"
    return start, end if end != start else ""


def _parse_city_country(cell_text: str, city_link_text: str) -> tuple[str, str]:
    text = _clean(city_link_text or cell_text)
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
    if m:
        return _clean(m.group(1)), _clean(m.group(2))
    return text, ""


def official_website_from_eventseye(soup: BeautifulSoup) -> str:
    node = soup.select_one("div.more-info a.ev-web")
    if node and node.get("href"):
        return _clean(node.get("href"))
    for anchor in soup.select("a.ev-web"):
        label = _clean(anchor.get_text(" ", strip=True))
        title = _clean(anchor.get("title"))
        if label == "Official Web Site" or "Go to" in title and "website" in title.lower():
            href = _clean(anchor.get("href"))
            if href.startswith("http"):
                return href
    return ""


def parse_eventseye_detail(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}

    h1 = soup.select_one("h1")
    if h1:
        out["event_name"] = _clean(h1.get_text(" ", strip=True))

    date_row = soup.select_one("table.dates tbody tr")
    if date_row:
        cells = date_row.select("td")
        if cells:
            start, end = _parse_eventseye_range(cells[0].get_text(" ", strip=True))
            out["event_start_date"] = start
            out["event_end_date"] = end
        if len(cells) > 1:
            city_link = cells[1].select_one("a")
            city, country = _parse_city_country(
                cells[1].get_text(" ", strip=True),
                city_link.get_text(" ", strip=True) if city_link else "",
            )
            out["city"] = city
            if country:
                out["country"] = country
        if len(cells) > 2:
            out["venue"] = _clean(cells[2].get_text(" ", strip=True))

    country_link = soup.select_one("div.venues a.countrylink")
    if country_link and not out.get("country"):
        out["country"] = _clean(country_link.get_text(" ", strip=True))

    venue_link = soup.select_one("div.venues a.placelink")
    if venue_link and not out.get("venue"):
        out["venue"] = _clean(venue_link.get_text(" ", strip=True))

    official = official_website_from_eventseye(soup)
    if official:
        out["official_site_url"] = official

    return out
