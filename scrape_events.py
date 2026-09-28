#!/usr/bin/env python3
"""Scrape event listings from configured sources and match them to the local TSI catalog."""

from __future__ import annotations

import argparse
import csv
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
import sys
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elib.csv_io import write_csv
from elib.fields import FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR, SOURCES_FILE
from elib.pipeline import EVENTS_SCRAPED, finalize_events
from elib.scrape_details import enrich_events_from_detail_pages

LOG = logging.getLogger("event-listing")
BROWSER_PROFILE: Path | None = None
BROWSER_HEADLESS = True
LOGIN_WAIT_SECONDS = 0
MAX_CRAWL_PAGES = 400
MAX_EVENTSEYE_LIST_PAGES = 4000
MAX_TRADEFEST_PAGES = 400
EVENTSEYE_FETCH_WORKERS = 16
EXPOFINDER_PAGE_SIZE = 100
TRADEFEST_PAGE_SIZE = 30
EXPOFINDER_SCROLL_MAX_ROUNDS = 100
EXPOFINDER_SCROLL_PER_FILTER = 25
TRADEGOV_SCROLL_MAX_ROUNDS = 40
EXPOFINDER_NON_CHINA_REGIONS = (
    "EUROPE",
    "UNITED_STATES",
    "JAPAN_SOUTH_KOREA",
    "SOUTHEAST_ASIA",
    "RUSSIA",
    "CENTRAL_ASIA",
    "SOUTH_AMERICA",
    "INDIA",
    "MIDDLE_EAST",
    "AFRICA",
)
EXPOFINDER_CHINA_REGIONS = ("MAINLAND_CHINA", "HONG_KONG", "TAIWAN")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}
EXPOFINDER_API = "https://www.expofinder.com/api/exhibitions/list"
EVENTSEYE_BASE = "https://www.eventseye.com/fairs/"
DATE_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,?\s+\d{4})\b",
    re.I,
)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip(" \t\r\n,;|")


def parse_date(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    match = DATE_RE.search(text)
    return match.group(0) if match else text


def empty_event(source: dict[str, str], status: str = "ok") -> dict[str, str]:
    return {
        "event_name": "", "event_start_date": "", "event_end_date": "", "event_domain": "",
        "venue": "", "city": "", "country": "", "source_name": source["name"], "source_url": source["url"],
        "event_url": "", "official_site_url": "", "domain_source": "",
        "scraped_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "scrape_status": status, "scrape_error": "",
    }


def event_from_jsonld(item: dict[str, Any], source: dict[str, str]) -> dict[str, str]:
    location = item.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    address = location.get("address") if isinstance(location, dict) else {}
    if isinstance(address, str):
        address = {"streetAddress": address}
    organizer = item.get("organizer") or {}
    if isinstance(organizer, list):
        organizer = organizer[0] if organizer else {}
    org_url = clean(organizer.get("url")) if isinstance(organizer, dict) else ""
    page_url = clean(item.get("url"))
    return {
        **empty_event(source),
        "event_name": clean(item.get("name")),
        "event_start_date": parse_date(item.get("startDate")),
        "event_end_date": parse_date(item.get("endDate")),
        "venue": clean(location.get("name") if isinstance(location, dict) else ""),
        "city": clean(address.get("addressLocality") if isinstance(address, dict) else ""),
        "country": clean(address.get("addressCountry") if isinstance(address, dict) else ""),
        "event_url": page_url,
        "official_site_url": org_url or page_url,
    }


def jsonld_events(soup: BeautifulSoup, source: dict[str, str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])
                if item.get("@type") == "Event" or (
                    isinstance(item.get("@type"), list) and "Event" in item["@type"]
                ):
                    event = event_from_jsonld(item, source)
                    if event["event_name"]:
                        found.append(event)
    return found


def fallback_events(soup: BeautifulSoup, source: dict[str, str]) -> list[dict[str, str]]:
    """Best-effort extraction for directory cards when JSON-LD is absent."""
    selectors = [
        "[class*='event']", "[class*='exhibition']", "[class*='fair']",
        "[class*='listing']", "article", "li",
    ]
    cards = []
    for selector in selectors:
        cards = soup.select(selector)
        if len(cards) >= 2:
            break
    events = []
    for card in cards:
        text = clean(card.get_text(" ", strip=True))
        if len(text) < 8 or len(text) > 1200:
            continue
        heading = card.select_one("h1,h2,h3,h4,h5,[class*='title'],[class*='name']")
        name = clean(heading.get_text(" ", strip=True) if heading else "")
        if not name:
            name = clean(text.split(" | ")[0].split(" - ")[0])[:180]
        name = re.sub(r"^(?:coming soon|in \d+ days?)\s+", "", name, flags=re.I)
        if (
            not name
            or len(name) < 4
            or re.fullmatch(r"in \d+ days?", name.lower())
            or name.lower() in {"coming soon", "more details"}
            or re.match(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}", name)
            or "·" in name
            or len(name.split()) < 2
        ):
            continue
        dates = DATE_RE.findall(text)
        link = card.select_one("a[href]")
        events.append({
            **empty_event(source),
            "event_name": name,
            "event_start_date": dates[0] if dates else "",
            "event_end_date": dates[1] if len(dates) > 1 else "",
            "event_url": urljoin(source["url"], link.get("href")) if link else "",
        })
    return events


def eventseye_table_events(soup: BeautifulSoup, source: dict[str, str]) -> list[dict[str, str]]:
    """EventsEye country/month listings use table.tradeshows."""
    country = ""
    match = re.search(r"c1_trade-shows_([a-z0-9-]+)", source.get("url", ""), re.I)
    if match:
        country = clean(match.group(1).replace("-", " ").title())
    events = []
    for row in soup.select("table.tradeshows tbody tr"):
        cells = row.select("td")
        if len(cells) < 4:
            continue
        link = cells[0].select_one("a[href]")
        if not link:
            continue
        name_node = link.select_one("b") or link
        name = clean(name_node.get_text(" ", strip=True))
        if not name:
            continue
        venue_links = cells[2].select("a[href]")
        city = clean(venue_links[0].get_text(" ", strip=True)) if venue_links else ""
        venue = clean(venue_links[1].get_text(" ", strip=True)) if len(venue_links) > 1 else ""
        date_text = clean(cells[3].get_text(" ", strip=True))
        dates = DATE_RE.findall(date_text)
        events.append({
            **empty_event(source),
            "event_name": name,
            "event_start_date": dates[0] if dates else date_text.split("_")[0],
            "event_end_date": dates[1] if len(dates) > 1 else "",
            "venue": venue,
            "city": city,
            "country": country,
            "event_url": urljoin(source["url"], link.get("href", "")),
        })
    return events


def table_events(soup: BeautifulSoup, source: dict[str, str]) -> list[dict[str, str]]:
    """Extract directory rows such as Trade Show Calendar's listing table."""
    if soup.select("table.tradeshows"):
        eventseye = eventseye_table_events(soup, source)
        if eventseye:
            return eventseye
    events = []
    for row in soup.select("table tr"):
        cells = row.select("th,td")
        if len(cells) < 3:
            continue
        values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
        link = row.select_one("a[href]")
        name = values[0]
        if not name or name.lower() in {"show names", "event", "events"}:
            continue
        dates = DATE_RE.findall(" ".join(values[1:2]))
        events.append({
            **empty_event(source),
            "event_name": name,
            "event_start_date": dates[0] if dates else values[1],
            "event_end_date": dates[1] if len(dates) > 1 else "",
            "city": values[2] if len(values) > 2 else "",
            "country": values[3] if len(values) > 3 else "",
            "event_url": urljoin(source["url"], link.get("href")) if link else "",
        })
    return events


def browser_html(source: dict[str, str]) -> str:
    """Render JS-heavy/anti-bot pages using the installed Chromium runtime."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if BROWSER_PROFILE:
            context = playwright.chromium.launch_persistent_context(
                str(BROWSER_PROFILE), headless=BROWSER_HEADLESS, user_agent=HEADERS["User-Agent"]
            )
            page = context.pages[0] if context.pages else context.new_page()
            browser = None
        else:
            browser = playwright.chromium.launch(headless=BROWSER_HEADLESS)
            context = browser
            page = context.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(source["url"], wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if LOGIN_WAIT_SECONDS and source["name"] in {"10times", "Vendelux"}:
            LOG.info("Waiting %d seconds for login/challenge completion on %s", LOGIN_WAIT_SECONDS, source["name"])
            page.wait_for_timeout(LOGIN_WAIT_SECONDS * 1000)
        title = (page.title() or "").lower()
        body_text = (page.locator("body").inner_text(timeout=5000) or "").lower()
        if "just a moment" in title or "verify you are human" in body_text:
            raise RuntimeError("Cloudflare human-verification challenge remained active")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        html = page.content()
        if browser:
            browser.close()
        else:
            context.close()
        return html


def base_stats(**overrides: Any) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "pages_visited": 0,
        "detail_pages_visited": 0,
        "pagination_links": 0,
        "raw_rows": 0,
        "status": "ok",
        "error": "",
        "truncated": False,
    }
    stats.update(overrides)
    return stats


def parse_expofinder_location(location: str) -> tuple[str, str]:
    text = clean(location)
    if "·" in text:
        country, city = text.split("·", 1)
        return clean(city), clean(country)
    return "", text


def expofinder_list_body(
    page_num: int,
    *,
    region: list[str] | None = None,
    time: list[str] | None = None,
    country_scope: str = "",
) -> dict[str, Any]:
    return {
        "language": "en",
        "pageNum": page_num,
        "pageSize": EXPOFINDER_PAGE_SIZE,
        "keyword": "",
        "industry": [],
        "region": region or [],
        "time": time or [],
        "countryScope": country_scope,
        "joinStatus": [],
        "timeStatus": [],
        "timeRange": [],
        "includeSearchContext": False,
    }


def append_expofinder_items(
    payload: dict[str, Any],
    source: dict[str, str],
    seen: set[str],
    events: list[dict[str, str]],
) -> None:
    for item in payload.get("exhibitions") or []:
        public_id = clean(item.get("publicId"))
        if not public_id or public_id in seen:
            continue
        seen.add(public_id)
        city, country = parse_expofinder_location(item.get("location") or "")
        time_text = clean(item.get("time") or "")
        date_parts = re.findall(r"\d{4}\.\d{2}\.\d{2}", time_text)
        events.append({
            **empty_event(source),
            "event_name": clean(item.get("name")),
            "event_start_date": date_parts[0].replace(".", "-") if date_parts else time_text,
            "event_end_date": date_parts[-1].replace(".", "-") if len(date_parts) > 1 else "",
            "city": city,
            "country": country,
            "event_url": f"https://www.expofinder.com/en/detail/{public_id}",
        })


def fetch_expofinder_payload_http(body: dict[str, Any]) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update({
        **HEADERS,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.expofinder.com",
        "Referer": "https://www.expofinder.com/en/list",
    })
    session.get("https://www.expofinder.com/en/list", timeout=45)
    response = session.post(EXPOFINDER_API, json=body, timeout=60)
    response.raise_for_status()
    return response.json()


def fetch_expofinder_payload_browser(page: Any, body: dict[str, Any]) -> dict[str, Any]:
    result = page.evaluate(
        """async (body) => {
            const response = await fetch('/api/exhibitions/list', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                body: JSON.stringify(body),
            });
            const text = await response.text();
            return { ok: response.ok, status: response.status, text };
        }""",
        body,
    )
    if not result.get("ok"):
        raise RuntimeError(f"ExpoFinder browser API HTTP {result.get('status')}: {result.get('text', '')[:200]}")
    return json.loads(result["text"])


def paginate_expofinder(
    source: dict[str, str],
    stats: dict[str, Any],
    fetch_payload: Any,
    *,
    region: list[str] | None = None,
    time: list[str] | None = None,
    country_scope: str = "",
    seen: set[str],
    events: list[dict[str, str]],
) -> None:
    page_num = 1
    total_pages = 1
    while page_num <= total_pages:
        try:
            payload = fetch_payload(
                expofinder_list_body(
                    page_num, region=region, time=time, country_scope=country_scope
                )
            )
        except Exception as exc:
            prior = clean(stats.get("error", ""))
            stats["error"] = f"{prior}; {exc}".strip("; ") if prior else str(exc)
            stats["truncated"] = page_num <= total_pages
            break
        total_pages = int(payload.get("totalPages") or 1)
        stats["pages_visited"] += 1
        append_expofinder_items(payload, source, seen, events)
        page_num += 1


EXPOFINDER_COLLECT_JS = """() => {
  const map = new Map();
  for (const anchor of document.querySelectorAll('a[href*="/detail/"]')) {
    const match = anchor.href.match(/\\/detail\\/([^/?]+)/);
    if (!match) continue;
    const id = match[1];
    if (map.has(id)) continue;
    let name = (anchor.innerText || '').replace(/\\s+/g, ' ').trim();
    name = name.replace(/^(Ongoing|Coming Soon|In \\d+ days?)\\s*/i, '');
    let time = '';
    let loc = '';
    let block = anchor.parentElement;
    for (let depth = 0; depth < 14 && block; depth++) {
      const lines = (block.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
      for (const line of lines) {
        if (/^\\d{4}[.\\-]\\d{2}[.\\-]\\d{2}/.test(line)) time = line;
        if (line.includes('·')) loc = line;
        if (/20\\d{2}/.test(line) && line.length > 8 && line.length < 200
            && !/^(Ongoing|Coming|Date|Location|In \\d+ days?)/i.test(line)) {
          const candidate = line.replace(/^(Ongoing|In \\d+ days?)\\s*/i, '');
          if (candidate.length > (name || '').length) name = candidate;
        }
      }
      if (name && time && loc) break;
      block = block.parentElement;
    }
    map.set(id, {
      id, name, time, loc,
      href: 'https://www.expofinder.com/en/detail/' + id,
    });
  }
  return [...map.values()];
}"""

TRADEGOV_COLLECT_JS = """() => {
  return [...document.querySelectorAll('.card.tarde-event-card')].map(card => {
    const lines = (card.querySelector('.trade-event-title-details')?.innerText || '')
      .split('\\n').map(s => s.trim()).filter(Boolean);
    let title = '';
    for (const line of lines) {
      if (line.length < 6 || /^\\d{4}-\\d{2}-\\d{2}/.test(line)) continue;
      if (line.includes('00:00')) continue;
      title = line;
    }
    const cal = card.querySelector('.event-calander')?.innerText?.trim() || '';
    const loc = card.querySelector('.event-location')?.innerText?.trim() || '';
    const anchors = [...card.querySelectorAll('a[href]')].map(a => a.href || '');
    let link = anchors.find(h => h && !h.toLowerCase().startsWith('javascript') && !h.includes('trade.gov.in')) || '';
    if (!link) {
      link = anchors.find(h => h && !h.toLowerCase().startsWith('javascript')) || '';
    }
    return { title, cal, loc, link };
  });
}"""


def append_expofinder_dom_items(
    items: list[dict[str, Any]],
    source: dict[str, str],
    seen: set[str],
    events: list[dict[str, str]],
) -> int:
    added = 0
    for item in items:
        public_id = clean(item.get("id"))
        if not public_id or public_id in seen:
            continue
        name = clean(item.get("name"))
        if not name:
            continue
        seen.add(public_id)
        city, country = parse_expofinder_location(item.get("loc") or "")
        time_text = clean(item.get("time") or "")
        date_parts = re.findall(r"\d{4}[.\-]\d{2}[.\-]\d{2}", time_text)
        events.append({
            **empty_event(source),
            "event_name": name,
            "event_start_date": date_parts[0].replace(".", "-") if date_parts else time_text,
            "event_end_date": date_parts[-1].replace(".", "-") if len(date_parts) > 1 else "",
            "city": city,
            "country": country,
            "event_url": clean(item.get("href")) or f"https://www.expofinder.com/en/detail/{public_id}",
        })
        added += 1
    return added


def expofinder_list_filter_urls() -> list[str]:
    """Listing URLs for global + per-region ExpoFinder filters."""
    base = "https://www.expofinder.com/en/list"
    urls = [base]
    for region in EXPOFINDER_NON_CHINA_REGIONS:
        urls.append(f"{base}?region={region}&countryScope=NON_CHINA")
    for region in EXPOFINDER_CHINA_REGIONS:
        urls.append(f"{base}?region={region}&countryScope=CHINA")
    return list(dict.fromkeys(urls))


def scrape_expofinder_dom_scroll(
    page: Any,
    source: dict[str, str],
    stats: dict[str, Any],
    seen: set[str],
    events: list[dict[str, str]],
    *,
    max_rounds: int = EXPOFINDER_SCROLL_MAX_ROUNDS,
) -> None:
    stagnant = 0
    for round_num in range(max_rounds):
        batch = page.evaluate(EXPOFINDER_COLLECT_JS)
        added = append_expofinder_dom_items(batch, source, seen, events)
        if added == 0:
            stagnant += 1
        else:
            stagnant = 0
        page.mouse.wheel(0, 3200)
        page.wait_for_timeout(700)
        for label in ("Show more", "Load more", "Show more..."):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=1500)
                page.wait_for_timeout(1200)
                stagnant = 0
            except Exception:
                pass
        if stagnant >= 8:
            break
    stats["pages_visited"] = max(stats.get("pages_visited", 0), round_num + 1)
    LOG.info("ExpoFinder DOM scroll: %d unique cards", len(seen))


def parse_trade_gov_location(location: str) -> tuple[str, str]:
    text = clean(location)
    if not text:
        return "", ""
    if "," in text:
        city, country = text.rsplit(",", 1)
        return clean(city), clean(country)
    return "", text


def scrape_trade_gov(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    events: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=BROWSER_HEADLESS)
        context = browser.new_context(
            locale="en-US",
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        page.goto(source["url"], wait_until="domcontentloaded", timeout=120000)
        try:
            page.wait_for_selector(".card.tarde-event-card", timeout=90000)
        except Exception as exc:
            stats["status"] = "loaded_no_events"
            stats["error"] = f"Event cards not found: {exc}"
            browser.close()
            return events, stats
        stagnant = 0
        for round_num in range(TRADEGOV_SCROLL_MAX_ROUNDS):
            batch = page.evaluate(TRADEGOV_COLLECT_JS)
            stats["pages_visited"] = round_num + 1
            added = 0
            for item in batch:
                title = clean(item.get("title"))
                if not title:
                    continue
                cal = clean(item.get("cal") or "")
                dates = re.findall(r"\d{4}-\d{2}-\d{2}", cal)
                key = (title.lower(), dates[0] if dates else cal)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                city, country = parse_trade_gov_location(item.get("loc") or "")
                link = clean(item.get("link"))
                if link.lower().startswith(("javascript:", "mailto:", "#")):
                    link = ""
                events.append({
                    **empty_event(source),
                    "event_name": title,
                    "event_start_date": dates[0] if dates else "",
                    "event_end_date": dates[-1] if len(dates) > 1 else "",
                    "city": city,
                    "country": country,
                    "event_url": link or source["url"],
                })
                added += 1
            if added == 0:
                stagnant += 1
            else:
                stagnant = 0
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(900)
            if stagnant >= 5:
                break
        browser.close()
    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
        stats["error"] = "No Trade.gov.in event cards extracted"
    LOG.info("Trade.gov.in: %d events from %d scroll rounds", len(events), stats["pages_visited"])
    return events, stats


def scrape_expofinder_with_browser_page(page: Any) -> Any:
    return lambda body: fetch_expofinder_payload_browser(page, body)


def scrape_expofinder(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    events: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []
    try:
        paginate_expofinder(
            source, stats, fetch_expofinder_payload_http, seen=seen, events=events
        )
    except Exception as http_exc:
        errors.append(str(http_exc))
        LOG.warning("ExpoFinder HTTP API blocked (%s); trying browser session", http_exc)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            if BROWSER_PROFILE:
                context = playwright.chromium.launch_persistent_context(
                    str(BROWSER_PROFILE),
                    headless=BROWSER_HEADLESS,
                    user_agent=HEADERS["User-Agent"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                browser = None
            else:
                browser = playwright.chromium.launch(headless=BROWSER_HEADLESS)
                context = browser
                page = context.new_page(user_agent=HEADERS["User-Agent"])
            first_url = expofinder_list_filter_urls()[0]
            page.goto(first_url, wait_until="domcontentloaded", timeout=120000)
            if LOGIN_WAIT_SECONDS:
                LOG.info(
                    "Waiting %d seconds for ExpoFinder login in browser",
                    LOGIN_WAIT_SECONDS,
                )
                page.wait_for_timeout(LOGIN_WAIT_SECONDS * 1000)
            else:
                page.wait_for_timeout(4000)
            try:
                paginate_expofinder(
                    source,
                    stats,
                    scrape_expofinder_with_browser_page(page),
                    seen=seen,
                    events=events,
                )
            except Exception as api_exc:
                errors.append(str(api_exc))
                LOG.warning("ExpoFinder in-browser API unavailable: %s", api_exc)
            filter_urls = expofinder_list_filter_urls()
            for index, list_url in enumerate(filter_urls):
                if index:
                    page.goto(list_url, wait_until="domcontentloaded", timeout=120000)
                    page.wait_for_timeout(2500)
                LOG.info("ExpoFinder region filter %d/%d: %s", index + 1, len(filter_urls), list_url)
                scrape_expofinder_dom_scroll(
                    page,
                    source,
                    stats,
                    seen,
                    events,
                    max_rounds=EXPOFINDER_SCROLL_PER_FILTER,
                )
            if browser:
                browser.close()
            else:
                context.close()
    except Exception as browser_exc:
        errors.append(str(browser_exc))
        LOG.warning("ExpoFinder browser session failed: %s", browser_exc)
    stats["raw_rows"] = len(events)
    if errors:
        stats["error"] = "; ".join(errors)
    if not events:
        stats["status"] = "loaded_no_events"
        hint = (
            "ExpoFinder may need a logged-in browser profile for the full catalog. "
            "Re-run with --browser-profile, --headed, and --login-wait-seconds if counts look low."
        )
        stats["error"] = f"{stats['error']}; {hint}" if stats["error"] else hint
    return events, stats


def normalize_eventseye_listing_url(href: str) -> str | None:
    """Keep only on-site EventsEye listing paths (ignore share URLs embedding our paths)."""
    if not href or href.startswith("#"):
        return None
    if "://" in href and "eventseye.com" not in href.lower():
        return None
    full = urljoin(EVENTSEYE_BASE, href.split("?", 1)[0].split("#", 1)[0])
    host = urlparse(full).netloc.lower()
    if host not in {"www.eventseye.com", "eventseye.com"}:
        return None
    path = urlparse(full).path
    if not re.search(
        r"/fairs/(z1_trade-shows_|c1_trade-shows_|d1_trade-shows_)[a-z0-9_-]+\.html$",
        path,
        re.I,
    ):
        return None
    return full


def eventseye_listing_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for anchor in soup.select("a[href]"):
        listing = normalize_eventseye_listing_url(anchor.get("href", ""))
        if listing:
            found.append(listing)
    return found


def eventseye_paginated_continuations(page_url: str, html: str, stem_pattern: str) -> list[str]:
    """Enqueue listing continuation pages such as c1_trade-shows_france_1.html."""
    stem = page_url.rsplit("/", 1)[-1].replace(".html", "")
    if not re.match(stem_pattern, stem, re.I):
        return []
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(r"\b1\s*/\s*(\d+)\b", text)
    links: list[str] = []
    if match:
        max_page = int(match.group(1))
        links.extend(
            urljoin(page_url, f"{stem}_{index}.html")
            for index in range(1, max_page)
        )
    for anchor in soup.select("a[href]"):
        listing = normalize_eventseye_listing_url(anchor.get("href", ""))
        if listing and re.search(rf"/{re.escape(stem)}_\d+\.html$", urlparse(listing).path, re.I):
            links.append(listing)
    return list(dict.fromkeys(links))


def eventseye_country_continuations(page_url: str, html: str) -> list[str]:
    return eventseye_paginated_continuations(
        page_url, html, r"c1_trade-shows_[a-z0-9-]+$"
    )


def eventseye_zone_continuations(page_url: str, html: str) -> list[str]:
    return eventseye_paginated_continuations(
        page_url, html, r"z1_trade-shows_[a-z0-9-]+$"
    )


def scrape_eventseye_page(
    url: str, source: dict[str, str]
) -> tuple[str, list[dict[str, str]], list[str]]:
    html = fetch_page(url, source)
    page_source = {**source, "url": url}
    page_events = extract_events(html, page_source)
    links: list[str] = []
    links.extend(eventseye_country_continuations(url, html))
    links.extend(eventseye_zone_continuations(url, html))
    links.extend(eventseye_listing_urls(html))
    return url, page_events, links


def scrape_eventseye(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    pending: deque[str] = deque([
        source["url"],
        urljoin(EVENTSEYE_BASE, "trade-shows-by-location.html"),
        urljoin(EVENTSEYE_BASE, "trade-shows-by-date.html"),
    ])
    for zone in ("america", "europe", "africa-middle-east", "asia-pacific"):
        pending.append(urljoin(EVENTSEYE_BASE, f"z1_trade-shows_{zone}.html"))
    visited: set[str] = set()
    events: list[dict[str, str]] = []
    while pending and len(visited) < MAX_EVENTSEYE_LIST_PAGES:
        batch: list[str] = []
        while pending and len(batch) < EVENTSEYE_FETCH_WORKERS * 3:
            url = pending.popleft()
            if url in visited:
                continue
            visited.add(url)
            batch.append(url)
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=EVENTSEYE_FETCH_WORKERS) as executor:
            futures = [executor.submit(scrape_eventseye_page, url, source) for url in batch]
            for future in as_completed(futures):
                try:
                    url, page_events, links = future.result()
                except Exception:
                    continue
                stats["pages_visited"] += 1
                events.extend(page_events)
                if stats["pages_visited"] % 100 == 0:
                    LOG.info(
                        "EventsEye: %d pages, %d events queued, %d pending links",
                        stats["pages_visited"],
                        len(events),
                        len(pending),
                    )
                for link in links:
                    if link not in visited and link not in pending:
                        pending.append(link)
                        stats["pagination_links"] += 1
    stats["truncated"] = bool(pending)
    stats["raw_rows"] = len(events)
    if not events:
        stats["status"] = "loaded_no_events"
        stats["error"] = "No EventsEye table rows extracted"
    return events, stats


def scrape_exhibitions_voice(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    events: list[dict[str, str]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        url = f"{source['url'].rstrip('/')}" + (f"?page={page}" if page > 1 else "")
        html = fetch_page(url, source)
        stats["pages_visited"] += 1
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("script#__NEXT_DATA__")
        if not node or not node.string:
            break
        props = json.loads(node.string)["props"]["pageProps"]
        total = int(props.get("total") or 0)
        page_size = int(props.get("pageSize") or 20)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        for row in props.get("result") or []:
            venue = row.get("venue") or {}
            country = ""
            country_obj = venue.get("country")
            if isinstance(country_obj, dict):
                country = clean(country_obj.get("value") or country_obj.get("label"))
            slug = clean(row.get("slug"))
            events.append({
                **empty_event(source),
                "event_name": clean(row.get("title")),
                "event_start_date": parse_date(row.get("start_date")),
                "event_end_date": parse_date(row.get("end_date")),
                "venue": clean(venue.get("name")),
                "city": clean(venue.get("city")),
                "country": country,
                "event_url": urljoin(source["url"], f"/tradeshows/{slug}") if slug else "",
            })
        page += 1
    stats["raw_rows"] = len(events)
    return events, stats


def tradefest_events_from_html(html: str, source: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if not node or not node.string:
        return []
    apollo = json.loads(node.string)["props"]["pageProps"].get("props", {}).get("__APOLLO_STATE__", {})
    events: list[dict[str, str]] = []
    for key, value in apollo.items():
        if not key.startswith("Event:") or not isinstance(value, dict):
            continue
        venue_ref = value.get("venue")
        city = country = venue = ""
        if isinstance(venue_ref, dict) and venue_ref.get("__ref"):
            venue_obj = apollo.get(venue_ref["__ref"], {})
            venue = clean(venue_obj.get("name"))
            location_ref = venue_obj.get("location")
            if isinstance(location_ref, dict) and location_ref.get("__ref"):
                location = apollo.get(location_ref["__ref"], {})
                city = clean(location.get("localizedCity"))
                country = clean(location.get("localizedCountry"))
        slug = clean(value.get("slug"))
        events.append({
            **empty_event(source),
            "event_name": clean(value.get("name")),
            "event_start_date": clean(value.get("startDate")),
            "event_end_date": clean(value.get("endDate")),
            "venue": venue,
            "city": city,
            "country": country,
            "event_url": urljoin(source["url"], f"/en/event/{slug}") if slug else "",
        })
    return events


def tradefest_total_count(html: str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("script#__NEXT_DATA__")
    if not node or not node.string:
        return None
    root = json.loads(node.string)["props"]["pageProps"].get("props", {}).get("__APOLLO_STATE__", {}).get("ROOT_QUERY", {})
    for key, value in root.items():
        if key.startswith("events(") and isinstance(value, dict):
            total = value.get("totalCount")
            if isinstance(total, int):
                return total
    return None


def tradefest_is_upcoming(row: dict[str, str]) -> bool:
    today = date.today()
    for field in ("event_end_date", "event_start_date"):
        text = clean(row.get(field, ""))[:10]
        if not text:
            continue
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            continue
        if parsed >= today:
            return True
    return False


def scrape_tradefest(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    stats = base_stats()
    events: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    page = 1
    total_count: int | None = None
    while True:
        url = f"https://tradefest.io/en/search?futureOnly=true&page={page}"
        try:
            html = fetch_page(url, {**source, "url": url})
        except Exception as exc:
            LOG.warning("Tradefest page %d failed: %s", page, exc)
            stats["error"] = str(exc)
            if not events:
                stats["status"] = "failed"
            break
        stats["pages_visited"] += 1
        if stats["pages_visited"] % 25 == 0:
            LOG.info("Tradefest: page %d, %d events collected", page, len(events))
        page_events = tradefest_events_from_html(html, source)
        new_rows = 0
        for row in page_events:
            if not tradefest_is_upcoming(row):
                continue
            slug = row.get("event_url", "").rstrip("/").split("/")[-1]
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            events.append(row)
            new_rows += 1
        if new_rows == 0:
            break
        if total_count is None:
            total_count = tradefest_total_count(html)
        if total_count is not None and len(seen_slugs) >= total_count:
            break
        page += 1
        if page > MAX_TRADEFEST_PAGES:
            stats["truncated"] = True
            break
    stats["raw_rows"] = len(events)
    return events, stats


def scrape_source_listing(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Route sources that need API or deep pagination to dedicated adapters."""
    adapters = {
        "ExpoFinder": scrape_expofinder,
        "EventsEye Upcoming": scrape_eventseye,
        "Exhibitions Voice": scrape_exhibitions_voice,
        "Tradefest": scrape_tradefest,
        "Trade.gov.in": scrape_trade_gov,
    }
    adapter = adapters.get(source["name"])
    if adapter:
        return adapter(source)
    return crawl_source(source)


def extract_events(html: str, source: dict[str, str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select("table.tradeshows"):
        events = eventseye_table_events(soup, source)
        if events:
            return events
    events = table_events(soup, source)
    if events:
        return events
    return jsonld_events(soup, source) or fallback_events(soup, source)


def crawl_links(html: str, source_url: str) -> tuple[set[str], set[str]]:
    """Return likely pagination/navigation links and event detail links."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(source_url).netloc
    pages: set[str] = set()
    details: set[str] = set()
    page_words = re.compile(
        r"(next|more|load|page[=/\-]?\d|month|january|february|march|april|may|"
        r"june|july|august|september|october|november|december|country|region)",
        re.I,
    )
    detail_words = re.compile(r"(/event|/expo|/fair|/f-|/detail|/exhibition)", re.I)
    for anchor in soup.select("a[href]"):
        href = urljoin(source_url, anchor.get("href", ""))
        text = clean(anchor.get_text(" ", strip=True))
        parsed = urlparse(href)
        if parsed.netloc != base_host or parsed.scheme not in {"http", "https"}:
            continue
        if page_words.search(text) or page_words.search(href):
            pages.add(href.split("#", 1)[0])
        if detail_words.search(parsed.path):
            details.add(href.split("#", 1)[0])
    return pages, details


def fetch_page(url: str, source: dict[str, str]) -> str:
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    html = response.text
    if "just a moment" in html.lower() or "verify you are human" in html.lower():
        raise RuntimeError("Cloudflare human-verification challenge")
    return html


def is_detail_url(url: str, source_name: str) -> bool:
    path = urlparse(url).path.lower()
    patterns = {
        "ExpoFinder": r"/en/detail/",
        "EventsEye Upcoming": r"/fairs/f-",
        "The Trade Show Calendar": r"/ttn/",
        "Tradefest": r"/en/event/",
        "Vendelux": r"/insights/",
        "Nice Nusantara Convex": r"/events/",
        "Expomap": r"/expo/[^/?]+",
        "HITEX": r"/events/[^/?]+/index\.html",
        "Exhibitions Voice": r"/tradeshows/[^/?]+",
    }
    pattern = patterns.get(source_name)
    return bool(re.search(pattern, path)) if pattern else False


def crawl_source(source: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Crawl listing/navigation pages and enrich extracted events from detail pages."""
    stats: dict[str, Any] = {
        "pages_visited": 0, "detail_pages_visited": 0, "pagination_links": 0,
        "raw_rows": 0, "status": "ok", "error": "", "truncated": False,
    }
    queue = [source["url"]]
    visited: set[str] = set()
    raw_events: list[dict[str, str]] = []
    detail_urls: set[str] = set()
    while queue and len(visited) < MAX_CRAWL_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html = fetch_page(url, source)
        except Exception as exc:
            if url == source["url"]:
                try:
                    html = browser_html(source)
                except Exception as browser_exc:
                    stats["status"] = "failed"
                    stats["error"] = f"{type(browser_exc).__name__}: {browser_exc}"
                    break
            else:
                continue
        page_source = {**source, "url": url}
        page_events = extract_events(html, page_source)
        raw_events.extend(page_events)
        stats["pages_visited"] += 1
        pages, details = crawl_links(html, url)
        detail_urls.update(details)
        detail_urls.update(
            event["event_url"] for event in page_events if event.get("event_url")
        )
        for link in pages:
            if link not in visited and len(visited) + len(queue) < MAX_CRAWL_PAGES:
                queue.append(link)
        stats["pagination_links"] += len(pages)
    stats["truncated"] = len(visited) >= MAX_CRAWL_PAGES
    stats["raw_rows"] = len(raw_events)

    detail_urls = {url for url in detail_urls if is_detail_url(url, source["name"])}
    # Detail pages are followed concurrently to keep broad crawls practical.
    def fetch_detail(detail_url: str) -> tuple[str, list[dict[str, str]]]:
        try:
            detail_html = fetch_page(detail_url, source)
            detail_events = extract_events(detail_html, {**source, "url": detail_url})
        except Exception:
            return detail_url, []
        return detail_url, detail_events

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch_detail, url) for url in detail_urls]
        for future in as_completed(futures):
            detail_url, detail_events = future.result()
            if detail_events:
                stats["detail_pages_visited"] += 1
                detail = max(detail_events, key=lambda event: len(event.get("event_name", "")))
                matching = next(
                    (event for event in raw_events if event.get("event_url") == detail_url),
                    None,
                )
                if matching:
                    for field in (
                        "event_name", "event_start_date", "event_end_date",
                        "venue", "city", "country", "official_site_url", "event_domain",
                    ):
                        if detail.get(field):
                            matching[field] = detail[field]
                else:
                    raw_events.append(detail)
    if not raw_events and stats["status"] == "ok":
        stats["status"] = "loaded_no_events"
        stats["error"] = "No event records extracted from crawled pages"
    return raw_events, stats


def scrape_source(
    source: dict[str, str],
    local_html: Path | None = None,
    saved_file: Path | None = None,
) -> list[dict[str, str]]:
    try:
        if saved_file and saved_file.suffix.lower() == ".csv":
            events = []
            with saved_file.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    event = empty_event(source)
                    for field in ("event_name", "event_start_date", "event_end_date", "venue", "city", "country", "event_url"):
                        event[field] = clean(row.get(field) or row.get(field.replace("event_", "")))
                    events.append(event)
            return events or [empty_event(source, "loaded_no_events")]
        if saved_file:
            html = saved_file.read_text(encoding="utf-8", errors="replace")
        elif local_html:
            html = local_html.read_text(encoding="utf-8", errors="replace")
        else:
            response = requests.get(source["url"], headers=HEADERS, timeout=45)
            response.raise_for_status()
            html = response.text
        events = extract_events(html, source)
        # Browser-rendered pages expose the listings only after JS execution.
        if not events and not local_html:
            events = extract_events(browser_html(source), source)
        for event in events:
            event["event_url"] = urljoin(source["url"], event["event_url"]) if event["event_url"] else ""
        if events:
            return events
        result = empty_event(source, "loaded_no_events")
        result["scrape_error"] = "No event records extracted from returned HTML"
        return [result]
    except Exception as exc:
        if not local_html:
            try:
                events = extract_events(browser_html(source), source)
                if events:
                    for event in events:
                        event["event_url"] = urljoin(source["url"], event["event_url"]) if event["event_url"] else ""
                    return events
            except Exception as browser_exc:
                exc = browser_exc
        LOG.warning("%s failed: %s", source["name"], exc)
        result = empty_event(source, "failed")
        result["event_name"] = f"[SOURCE FAILED] {source['name']}"
        result["scrape_error"] = f"{type(exc).__name__}: {exc}"
        return [result]


DEDUPE_KEY_FIELDS = ("event_name", "event_start_date", "venue", "city", "country")


def key(event: dict[str, str]) -> tuple[str, ...]:
    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()
    return tuple(norm(event.get(field, "")) for field in DEDUPE_KEY_FIELDS)


def _dedupe_key_part(event: dict[str, str], field: str) -> str:
    return key(event)[DEDUPE_KEY_FIELDS.index(field)]


def deduplicate(events: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[tuple[str, ...], dict[str, str]] = {}
    for event in events:
        if event.get("scrape_status") != "ok":
            by_key[(event["source_name"], event["scrape_status"])] = event
            continue
        k = key(event)
        if not k[0]:
            continue
        # Directory cards often expose the same event multiple times with
        # different optional fields. Merge compatible records by name/date
        # before applying the full six-field identity rule.
        compatible_key = next(
            (
                existing_key for existing_key, existing in by_key.items()
                if existing_key[0] == k[0]
                and (not k[1] or not existing_key[1] or k[1] == existing_key[1])
                and all(
                    not event.get(field) or not existing.get(field) or
                    _dedupe_key_part(event, field) == _dedupe_key_part(existing, field)
                    for field in ("event_start_date", "venue", "city", "country")
                )
            ),
            None,
        )
        if compatible_key and compatible_key != k:
            existing = by_key.pop(compatible_key)
            for field in FIELDS:
                if not existing.get(field) and event.get(field):
                    existing[field] = event[field]
            by_key[key(existing)] = existing
            continue
        existing = by_key.get(k)
        if not existing or sum(bool(event.get(f)) for f in FIELDS) > sum(bool(existing.get(f)) for f in FIELDS):
            by_key[k] = event
    return list(by_key.values())


def write_report(
    path: Path,
    sources: list[dict[str, str]],
    rows: list[dict[str, Any]],
    total_scraped: int,
    source_stats: dict[str, dict[str, Any]],
) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["source_name"]] = counts.get(row["source_name"], 0) + 1
    on_tsi = sum(row.get("tsi_located") == "Yes" for row in rows)
    in_ei = sum(row.get("ei_located") == "Yes" for row in rows)
    ei_not_published = sum(row.get("listing_tier") == "EI not published" for row in rows)
    not_in_ei = sum(row.get("listing_tier") == "Not in Event Intelligence" for row in rows)
    failed = sum(row.get("scrape_status") == "failed" for row in rows)
    lines = [
        "# Event listing scrape report", "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Configured sources: {len(sources)}",
        f"- Total events scraped across sources: {total_scraped}",
        f"- Unique events after deduplication: {len(rows)}",
        f"- In Event Intelligence (name matched): {in_ei}",
        f"- Published on TSI (has tradeintelshow_event_id): {on_tsi}",
        f"- In Event Intelligence, not published on TSI: {ei_not_published}",
        f"- Not in Event Intelligence: {not_in_ei}",
        f"- Sources that failed: {failed}", "",
        "## Source results", "",
        "| Source | Pages | Detail pages | Raw rows | Unique rows | Status | Truncated |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for source in sources:
        source_rows = [r for r in rows if r["source_name"] == source["name"]]
        stats = source_stats.get(source["name"], {})
        lines.append(
            f"| {source['name']} | {stats.get('pages_visited', 0)} | "
            f"{stats.get('detail_pages_visited', 0)} | {stats.get('raw_rows', 0)} | "
            f"{counts.get(source['name'], 0)} | {stats.get('status', 'not_run')} | "
            f"{'Yes' if stats.get('truncated') else 'No'} |"
        )
    problem_rows = [
        row for row in rows
        if row.get("scrape_status") in {"failed", "loaded_no_events"}
    ]
    problem_sources = [
        (source["name"], source["url"], stats.get("status", "unknown"), stats.get("error", ""))
        for source in sources
        for stats in [source_stats.get(source["name"], {})]
        if stats.get("status") in {"failed", "loaded_no_events"}
    ]
    lines += ["", "## Failed or empty sources", "",
              "| Source | URL | Status | Reason |", "|---|---|---|---|"]
    if problem_sources:
        for name, url, status, error in problem_sources:
            reason = clean(error) or "No event records extracted"
            reason = reason.replace("|", "\\|")
            lines.append(f"| {name} | {url} | {status} | {reason} |")
    elif problem_rows:
        for row in problem_rows:
            reason = clean(row.get("scrape_error")) or "No reason recorded"
            reason = reason.replace("|", "\\|")
            lines.append(
                f"| {row['source_name']} | {row['source_url']} | "
                f"{row['scrape_status']} | {reason} |"
            )
    else:
        lines.append("| None | — | — | All sources returned event records |")
    lines += [
        "", "## Deduplication rule",
        "Rows are deduplicated using normalized event name, start date, venue, city, and country "
        "(end date is kept on the row but not part of the dedupe key).",
        "", "## Event Intelligence / TSI mapping",
        "When `event-listing/.env` is available, events are matched read-only against "
        "`events_central` by name (fuzzy, with date/location checks). "
        "`ei_located=Yes` means a catalog row matched; `tsi_located=Yes` means `tradeintelshow_event_id` "
        "is set (published on TradeShowIntel). "
        "`listing_tier=EI not published` means in Event Intelligence without a TSI id.",
        "", "## Notes",
        "Dynamic, blocked, login-gated, or anti-bot pages are reported as failed or loaded_no_events; "
        "they are not represented as successfully scraped events.",
    ]
    errors = [
        (name, data.get("error", ""))
        for name, data in source_stats.items()
        if data.get("error")
    ]
    if errors:
        lines += ["", "## Crawl warnings", ""]
        for name, error in errors:
            lines.append(f"- **{name}:** {error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scrape_one_source(
    index: int,
    source: dict[str, str],
    saved_file: Path | None,
    local_html: Path | None,
    *,
    skip_detail_enrich: bool = False,
    max_detail_pages: int = 0,
    detail_workers: int = 12,
) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Scrape a single configured source (used for parallel source runs)."""
    name = source["name"]
    try:
        if saved_file or (local_html and index == 2):
            events = scrape_source(source, local_html if index == 2 else None, saved_file)
            stats = {
                "pages_visited": 1,
                "detail_pages_visited": 0,
                "raw_rows": len(events),
                "status": events[0].get("scrape_status", "ok") if events else "ok",
                "error": events[0].get("scrape_error", "") if events else "",
                "pagination_links": 0,
                "truncated": False,
            }
        else:
            events, stats = scrape_source_listing(source)
            if not skip_detail_enrich and events and events[0].get("scrape_status") == "ok":
                events = enrich_events_from_detail_pages(
                    events,
                    source,
                    stats,
                    is_detail_url=is_detail_url,
                    parse_events=lambda html, url: extract_events(html, {**source, "url": url}),
                    max_details=max_detail_pages,
                    workers=detail_workers,
                )
        return name, events, stats
    except Exception as exc:
        LOG.exception("%s failed: %s", name, exc)
        failed = empty_event(source, "failed")
        failed["event_name"] = f"[SOURCE FAILED] {name}"
        failed["scrape_error"] = f"{type(exc).__name__}: {exc}"
        return name, [failed], base_stats(status="failed", error=str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SOURCES_FILE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--local-html", type=Path, help="Use a saved HTML file for the first configured source")
    parser.add_argument(
        "--source-file",
        action="append",
        default=[],
        metavar="SOURCE=PATH",
        help="Use a saved HTML or CSV for a source, e.g. '10times=exports/10times.html'",
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        help="Persistent Playwright profile directory for sites requiring browser cookies/session",
    )
    parser.add_argument("--headed", action="store_true", help="Show the Chromium window during browser fallback")
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=0,
        help="Keep visible login-protected pages open this many seconds before scraping",
    )
    parser.add_argument(
        "--source-workers",
        type=int,
        default=2,
        metavar="N",
        help="Scrape up to N sources in parallel (default: 2).",
    )
    parser.add_argument(
        "--only-sources",
        type=str,
        default="",
        help="Comma-separated source names to scrape (default: all in sources.json).",
    )
    parser.add_argument(
        "--mongo-env",
        type=Path,
        default=ENV_FILE,
        help="Path to .env with MONGODB_URI (default: event-listing/.env). Read-only.",
    )
    parser.add_argument(
        "--skip-mongo",
        action="store_true",
        help="Do not load Event Intelligence catalog from MongoDB (local files only).",
    )
    parser.add_argument(
        "--skip-detail-enrich",
        action="store_true",
        help="Do not fetch per-event detail pages (faster; fewer domains and fields).",
    )
    parser.add_argument(
        "--max-detail-pages",
        type=int,
        default=0,
        help="Cap detail-page fetches per source (0 = no cap).",
    )
    parser.add_argument(
        "--detail-workers",
        type=int,
        default=12,
        help="Parallel workers for detail-page enrichment.",
    )
    parser.add_argument(
        "--domain-web-search",
        action="store_true",
        help="Slow web search for missing official event_domain (default: EI Mongo + URL only).",
    )
    parser.add_argument(
        "--domain-workers",
        type=int,
        default=8,
        help="Parallel workers when --domain-web-search is set (default: 8).",
    )
    args = parser.parse_args()
    global BROWSER_PROFILE
    global BROWSER_HEADLESS, LOGIN_WAIT_SECONDS
    BROWSER_PROFILE = args.browser_profile
    BROWSER_HEADLESS = not args.headed
    LOGIN_WAIT_SECONDS = max(0, args.login_wait_seconds)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sources = json.loads(args.config.read_text(encoding="utf-8"))
    if args.only_sources.strip():
        allowed = {name.strip() for name in args.only_sources.split(",") if name.strip()}
        sources = [source for source in sources if source["name"] in allowed]
        if not sources:
            parser.error(f"No sources matched --only-sources: {args.only_sources!r}")
    saved_sources: dict[str, Path] = {}
    for item in args.source_file:
        name, separator, path = item.partition("=")
        if not separator or not name or not path:
            parser.error(f"Invalid --source-file value: {item!r}; expected SOURCE=PATH")
        saved_sources[name.strip()] = Path(path.strip())
    raw: list[dict[str, str]] = []
    source_stats: dict[str, dict[str, Any]] = {}
    workers = max(1, min(args.source_workers, len(sources)))
    LOG.info("Scraping %d sources with %d parallel workers", len(sources), workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                scrape_one_source,
                index,
                source,
                saved_sources.get(source["name"]),
                args.local_html if index == 2 else None,
                skip_detail_enrich=args.skip_detail_enrich,
                max_detail_pages=args.max_detail_pages,
                detail_workers=args.detail_workers,
            )
            for index, source in enumerate(sources)
        ]
        for future in as_completed(futures):
            name, events, stats = future.result()
            source_stats[name] = stats
            raw.extend(events)
            LOG.info(
                "%s: %d pages, %d details, %d raw rows, status=%s",
                name, stats.get("pages_visited", 0),
                stats.get("detail_pages_visited", 0),
                stats.get("raw_rows", len(events)),
                stats.get("status", "ok"),
            )
    events = deduplicate(raw)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.output_dir / EVENTS_SCRAPED.name, events, FIELDS)
    scrape_summary = {
        "raw_rows": len(raw),
        "unique_rows": len(events),
        "source_count": len(sources),
    }
    write_report(args.output_dir / "scrape_report.md", sources, events, len(raw), source_stats)
    finalize_events(
        events,
        env_path=args.mongo_env,
        skip_mongo=args.skip_mongo,
        skip_domain_web_search=not args.domain_web_search,
        domain_workers=args.domain_workers,
        output_dir=args.output_dir,
        scrape_stats=scrape_summary,
    )
    print(f"Pipeline complete — see {args.output_dir / 'events_final.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
