"""Parse NICE (nice-nusantaraconvex.com) event detail pages."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from elib.scrape_details import official_site_from_html

_SOCIAL_OR_SKIP = (
    "instagram.",
    "facebook.",
    "youtube.",
    "google.",
    "linkedin.",
    "twitter.",
    "x.com",
    "wa.me",
    "whatsapp.",
    "tiktok.",
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _yyyymmdd(raw: str) -> str:
    text = _clean(raw)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return ""


def _gcal_end_inclusive(end_raw: str) -> str:
    """Google Calendar all-day end dates are exclusive."""
    iso = _yyyymmdd(end_raw)
    if not iso:
        return ""
    try:
        end_dt = datetime.strptime(iso, "%Y-%m-%d") - timedelta(days=1)
    except ValueError:
        return iso
    return end_dt.strftime("%Y-%m-%d")


def _external_official(href: str) -> str:
    href = (href or "").strip()
    if not href.startswith("http"):
        return ""
    host = host_of(href)
    if not host or host in LISTING_HOSTS:
        return ""
    lower = host.lower()
    if any(token in lower for token in _SOCIAL_OR_SKIP):
        return ""
    return href.split("#")[0].rstrip("/")


def _official_from_hero(soup: BeautifulSoup, page_url: str, html: str) -> str:
    hero = soup.select_one(".event-detail-hero") or soup
    candidates: list[str] = []
    for anchor in hero.find_all("a", href=True):
        site = _external_official(anchor["href"])
        if site:
            candidates.append(site)
    if candidates:
        for site in candidates:
            host = host_of(site) or ""
            if host and "linktr.ee" not in host:
                return site
        return candidates[0]
    return official_site_from_html(html, page_url)


def parse_nice_detail(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.find("h1")
    event_name = _clean(title_node.get_text()) if title_node else ""

    start_date = ""
    end_date = ""
    venue = ""
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "calendar.google.com" not in href:
            continue
        match = re.search(r"dates=(\d{8})T\d+/(\d{8})T", href)
        if match:
            start_date = _yyyymmdd(match.group(1))
            end_date = _gcal_end_inclusive(match.group(2))
        loc = re.search(r"location=([^&]+)", href)
        if loc:
            venue = _clean(unquote(loc.group(1).replace("+", " ")))

    official = _official_from_hero(soup, page_url, html)
    host = host_of(official)
    domain = ""
    if host and host not in LISTING_HOSTS:
        domain = normalize_domain(host)

    return {
        "event_name": event_name,
        "event_start_date": start_date,
        "event_end_date": end_date,
        "venue": venue,
        "city": "Tangerang",
        "country": "Indonesia",
        "official_site_url": official,
        "event_domain": domain,
    }
