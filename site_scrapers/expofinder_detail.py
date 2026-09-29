"""Parse ExpoFinder event detail pages (JSON-LD + embedded officialWebsite)."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from elib.scrape_details import official_site_from_html

_DATE_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def official_website_from_expofinder_html(html: str) -> str:
    for pattern in (
        r'"officialWebsite"\s*:\s*"([^"]+)"',
        r'officialWebsite\\":\\"([^\\"]+)\\"',
    ):
        for match in re.finditer(pattern, html):
            url = _clean(match.group(1))
            if url and url.startswith("http"):
                return url
    return ""


def parse_location_name(name: str) -> tuple[str, str, str]:
    """ExpoFinder uses Country·City·Venue in location strings."""
    text = _clean(name)
    if not text:
        return "", "", ""
    parts = [p.strip() for p in text.split("·") if p.strip()]
    if len(parts) >= 3:
        return parts[2], parts[1], parts[0]
    if len(parts) == 2:
        return "", parts[1], parts[0]
    return "", "", text


def parse_jsonld_event(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        raw = node.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            if types != "Event" and not (isinstance(types, list) and "Event" in types):
                continue
            out["event_name"] = _clean(item.get("name"))
            start = _clean(item.get("startDate"))
            end = _clean(item.get("endDate"))
            m = _DATE_ISO.search(start)
            out["event_start_date"] = m.group(1) if m else start[:10]
            m = _DATE_ISO.search(end)
            out["event_end_date"] = m.group(1) if m else end[:10]
            loc = item.get("location") or {}
            if isinstance(loc, dict):
                loc_name = _clean(loc.get("name") or loc.get("address"))
                venue, city, country = parse_location_name(loc_name)
                out["venue"] = venue
                out["city"] = city
                out["country"] = country
            break
        if out:
            break
    return out


def parse_expofinder_detail(html: str, page_url: str) -> tuple[dict[str, str], str]:
    detail = parse_jsonld_event(html)
    official = official_website_from_expofinder_html(html) or official_site_from_html(html, page_url)
    return detail, official
