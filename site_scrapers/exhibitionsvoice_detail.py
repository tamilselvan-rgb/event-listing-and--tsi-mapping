"""Parse Exhibitions Voice event detail pages (/event/{slug})."""

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


def _country_from_venue(venue: dict[str, Any]) -> str:
    country_obj = venue.get("country")
    if isinstance(country_obj, dict):
        return _clean(country_obj.get("value") or country_obj.get("label"))
    return _clean(country_obj)


def parse_detail_next_data(html: str, page_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if not node or not node.string:
        return {}
    props = json.loads(node.string).get("props", {}).get("pageProps", {})
    row = props.get("result")
    if not isinstance(row, dict):
        return {}

    venue = row.get("venue") if isinstance(row.get("venue"), dict) else {}
    official = _clean(row.get("website") or "")
    if not official:
        official = official_site_from_html(html, page_url)
    host = host_of(official)
    domain = ""
    if host and host not in LISTING_HOSTS:
        domain = normalize_domain(host)

    return {
        "event_name": _clean(row.get("title")),
        "event_start_date": _iso_date(row.get("start_date")),
        "event_end_date": _iso_date(row.get("end_date")),
        "venue": _clean(venue.get("name")),
        "city": _clean(venue.get("city")),
        "country": _country_from_venue(venue),
        "official_site_url": official,
        "event_domain": domain,
    }
