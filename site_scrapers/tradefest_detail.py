"""Parse Tradefest event detail pages (__NEXT_DATA__ / Apollo)."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def apollo_from_html(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if not node or not node.string:
        return {}
    root = json.loads(node.string)
    page_props = root.get("props", {}).get("pageProps", {})
    props = page_props.get("props") or page_props
    return props.get("__APOLLO_STATE__") or {}


def _resolve_venue(apollo: dict[str, Any], venue_ref: Any) -> tuple[str, str, str]:
    venue = city = country = ""
    if not isinstance(venue_ref, dict) or not venue_ref.get("__ref"):
        return venue, city, country
    venue_obj = apollo.get(venue_ref["__ref"], {})
    if not isinstance(venue_obj, dict):
        return venue, city, country
    venue = _clean(venue_obj.get("name"))
    location_ref = venue_obj.get("location")
    if isinstance(location_ref, dict) and location_ref.get("__ref"):
        location = apollo.get(location_ref["__ref"], {})
        if isinstance(location, dict):
            city = _clean(location.get("localizedCity"))
            country = _clean(location.get("localizedCountry"))
    return venue, city, country


def _official_domain(official_url: str) -> tuple[str, str]:
    official = _clean(official_url)
    if not official:
        return "", ""
    host = host_of(official)
    if not host or host in LISTING_HOSTS:
        return official, ""
    domain = normalize_domain(host)
    return official, domain


def primary_event_from_apollo(apollo: dict[str, Any]) -> dict[str, str] | None:
    """Best single Event node on a detail page (has description / url)."""
    best: dict[str, Any] | None = None
    for key, value in apollo.items():
        if not key.startswith("Event:") or not isinstance(value, dict):
            continue
        if not value.get("slug"):
            continue
        if best is None or (value.get("description") and not best.get("description")):
            best = value
    if not best:
        return None
    venue, city, country = _resolve_venue(apollo, best.get("venue"))
    official, domain = _official_domain(_clean(best.get("url")))
    return {
        "event_name": _clean(best.get("name")),
        "event_start_date": _clean(best.get("startDate"))[:10],
        "event_end_date": _clean(best.get("endDate"))[:10],
        "venue": venue,
        "city": city,
        "country": country,
        "official_site_url": official,
        "event_domain": domain,
    }


def parse_tradefest_detail(html: str) -> dict[str, str]:
    apollo = apollo_from_html(html)
    row = primary_event_from_apollo(apollo)
    return row or {}
