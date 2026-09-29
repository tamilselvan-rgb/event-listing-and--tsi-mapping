"""Parse Exhibitors Data product pages and API description HTML."""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from elib.listing_hosts import LISTING_HOSTS

_SOCIAL = re.compile(
    r"(facebook|linkedin|twitter|instagram|youtube|pinterest|whatsapp|t\.co)",
    re.I,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def parse_location(location: str) -> tuple[str, str, str]:
    """'Venue, City, Country' with flexible comma segments."""
    parts = [_clean(p) for p in (location or "").split(",") if _clean(p)]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return "", "", parts[0]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return parts[0], parts[1], parts[-1]


def _external_href(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if not href.startswith("http"):
        return ""
    host = (urlparse(href).netloc or "").lower().replace("www.", "")
    if not host or host in LISTING_HOSTS or "exhibitorsdata.com" in host:
        return ""
    if _SOCIAL.search(host):
        return ""
    return href


def official_site_from_description(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        site = _external_href(anchor.get("href") or "")
        if site:
            links.append(site)
    if not links:
        return ""
    # Prefer links whose anchor text mentions a year or looks like an event site name.
    scored: list[tuple[int, str]] = []
    for anchor in soup.select("a[href]"):
        site = _external_href(anchor.get("href") or "")
        if not site:
            continue
        text = _clean(anchor.get_text(" ", strip=True))
        score = 0
        if re.search(r"20\d{2}", text):
            score += 3
        if len(text) > 3:
            score += 1
        if "exhibitor" in text.lower() or "attendee" in text.lower():
            score -= 2
        scored.append((score, site))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1] if scored else links[0]


def parse_product_detail_html(html: str) -> str:
    """Fallback: official link from /product/{slug} page body."""
    return official_site_from_description(html)
