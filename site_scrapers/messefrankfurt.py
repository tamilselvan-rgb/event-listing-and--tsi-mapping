"""Scrape https://www.messefrankfurt.com/frankfurt/en/event-search.html (API listing + detail URLs)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape
from typing import Any

import requests

from elib.event_domains import host_of, normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from site_scrapers.base import base_stats, empty_row
from site_scrapers.messefrankfurt_detail import parse_messefrankfurt_detail

LOG = logging.getLogger("event-listing.site_scrapers.messefrankfurt")

LIST_URL = "https://www.messefrankfurt.com/frankfurt/en/event-search.html"
SEARCH_API = (
    "https://api.messefrankfurt.com/service/esb_api"
    "/event-service/api/2.1/public/event/searchMultiParam/en-GB"
)
COUNTRY_API = (
    "https://api.messefrankfurt.com/service/esb_api"
    "/i18n-service/api/2.1/public/i18n/label/option/type/COUNTRY/language/en-GB"
)

# Public key shipped in eventsearch.messefrankfurt.com/assets/eventsearch.js (eventApikey, ENV prd).
EVENT_API_KEY = "dD9LNnLjFXr3wABPY1vOwoFHe8OgWY4Gabm6qA4pejRR7IrXHSs="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": LIST_URL,
}

SOURCE_MESSE_FRANKFURT = {
    "name": "Messe Frankfurt",
    "url": LIST_URL,
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def _api_headers() -> dict[str, str]:
    return {**HEADERS, "apikey": EVENT_API_KEY}


def normalize_internet_url(raw: str) -> str:
    url = _clean(raw)
    if not url or url.lower() == "dont use":
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = f"http://{url}"
    return url.split("#")[0].rstrip("/")


def fetch_json(url: str) -> dict[str, Any]:
    response = requests.get(url, headers=_api_headers(), timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"API error: {payload}")
    return payload


def load_country_labels() -> dict[str, str]:
    payload = fetch_json(COUNTRY_API)
    result = payload.get("result")
    if isinstance(result, dict):
        return {str(k): _clean(v) for k, v in result.items()}
    return {}


def fetch_all_hits() -> list[dict[str, Any]]:
    payload = fetch_json(SEARCH_API)
    hits = (payload.get("result") or {}).get("hits") or []
    return [h for h in hits if isinstance(h, dict)]


def _iso_from_hit(value: Any) -> str:
    text = _clean(value)
    if len(text) >= 10 and re.match(r"20\d{2}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return ""


def row_from_hit(hit: dict[str, Any], source: dict[str, str], countries: dict[str, str]) -> dict[str, str]:
    row = empty_row(source)
    official = normalize_internet_url(hit.get("interneturl") or "")
    host = host_of(official)
    domain = ""
    if host and host not in LISTING_HOSTS:
        domain = normalize_domain(host)

    iso3 = _clean(hit.get("iso3")).upper()
    country = countries.get(iso3, iso3)

    row.update(
        {
            "event_name": _clean(hit.get("eventname")),
            "event_start_date": _iso_from_hit(hit.get("startdate")),
            "event_end_date": _iso_from_hit(hit.get("enddate")),
            "venue": _clean(hit.get("venuename")),
            "city": _clean(hit.get("cityname")),
            "country": country,
            "event_url": official,
            "official_site_url": official,
            "event_domain": domain,
            "domain_source": "listing_interneturl" if domain else "",
        }
    )
    return row


def _is_upcoming(row: dict[str, str], *, today: date) -> bool:
    start = row.get("event_start_date") or ""
    end = row.get("event_end_date") or start
    if not end and not start:
        return True
    try:
        end_dt = date.fromisoformat(end[:10]) if end else date.fromisoformat(start[:10])
    except ValueError:
        return True
    return end_dt >= today


def _hit_is_listable(hit: dict[str, Any]) -> bool:
    if _clean(hit.get("cancelled")).upper() == "Y":
        return False
    if _clean(hit.get("active")).upper() == "N":
        return False
    if not _clean(hit.get("eventname")):
        return False
    if not normalize_internet_url(hit.get("interneturl") or ""):
        return False
    return True


def enrich_from_detail_pages(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 12,
    max_details: int = 0,
) -> None:
    urls = list(dict.fromkeys(
        (e.get("event_url") or "").strip() for e in events if (e.get("event_url") or "").strip()
    ))
    if max_details > 0:
        urls = urls[:max_details]
    if not urls:
        return

    LOG.info("Messe Frankfurt: fetching %d detail pages", len(urls))

    def fetch_one(url: str) -> tuple[str, str]:
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=90,
                allow_redirects=True,
            )
            response.raise_for_status()
            return url, response.text
        except Exception as exc:
            LOG.debug("detail failed %s: %s", url, exc)
            return url, ""

    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        url = (event.get("event_url") or "").strip()
        if url in urls:
            url_to_indices.setdefault(url, []).append(index)

    fetched = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, url) for url in url_to_indices]
        for future in as_completed(futures):
            url, html = future.result()
            if not html:
                continue
            fetched += 1
            detail = parse_messefrankfurt_detail(html, url)
            for idx in url_to_indices.get(url, []):
                for field in (
                    "event_name",
                    "event_start_date",
                    "event_end_date",
                    "venue",
                    "city",
                    "country",
                    "official_site_url",
                    "event_domain",
                ):
                    val = detail.get(field) or ""
                    if val:
                        events[idx][field] = val
                if detail.get("event_domain"):
                    events[idx]["domain_source"] = "detail_official_site"
                events[idx]["event_url"] = url

    stats["detail_pages_visited"] = fetched


def scrape_messefrankfurt(
    *,
    enrich_details: bool = True,
    max_details: int = 0,
    upcoming_only: bool = True,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_MESSE_FRANKFURT
    stats = base_stats()
    stats["listing_mode"] = "eventsearch_api_multiparam"

    try:
        countries = load_country_labels()
        hits = fetch_all_hits()
    except Exception as exc:
        LOG.warning("Messe Frankfurt API failed: %s", exc)
        stats["error"] = str(exc)
        stats["status"] = "failed"
        return [], stats

    stats["pages_visited"] = 1

    seen_ids: set[str] = set()
    events: list[dict[str, str]] = []
    today = date.today()

    for hit in hits:
        if not _hit_is_listable(hit):
            continue
        event_id = _clean(hit.get("eventid") or hit.get("eventnumber"))
        if event_id and event_id in seen_ids:
            continue
        if event_id:
            seen_ids.add(event_id)

        row = row_from_hit(hit, source, countries)
        if upcoming_only and not _is_upcoming(row, today=today):
            continue
        events.append(row)

    events.sort(key=lambda r: (r.get("event_start_date") or "9999", r.get("event_name") or ""))
    LOG.info("Messe Frankfurt API — %d listable upcoming events", len(events))

    if enrich_details and events:
        enrich_from_detail_pages(events, stats, max_details=max_details)

    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
    return events, stats
