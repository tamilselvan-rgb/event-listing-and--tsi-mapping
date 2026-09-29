"""Scrape upcoming exhibitions from https://www.expofinder.com/en/list."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from html import unescape
from typing import Any, Callable

import requests

from elib.event_domains import normalize_domain
from elib.scrape_details import merge_detail_into_listing
from site_scrapers.base import SOURCE_EXPOFINDER, base_stats, empty_row
from site_scrapers.expofinder_detail import parse_expofinder_detail
from site_scrapers.session_cookies import (
    build_requests_session,
    load_session_file,
    resolve_session_path,
)

LOG = logging.getLogger("event-listing.site_scrapers.expofinder")

API_URL = "https://www.expofinder.com/api/exhibitions/list"
LIST_URL = "https://www.expofinder.com/en/list"
PAGE_SIZE = 100
SCROLL_MAX_ROUNDS = 120
SCROLL_PER_FILTER = 35

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

NON_CHINA_REGIONS = (
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
CHINA_REGIONS = ("MAINLAND_CHINA", "HONG_KONG", "TAIWAN")

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
    map.set(id, { id, name, time, loc, href: 'https://www.expofinder.com/en/detail/' + id });
  }
  return [...map.values()];
}"""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def parse_expofinder_location(location: str) -> tuple[str, str]:
    text = _clean(location)
    if "·" in text:
        country, city = text.split("·", 1)
        return _clean(city), _clean(country)
    return "", text


def list_body(
    page_num: int,
    *,
    region: list[str] | None = None,
    country_scope: str = "",
) -> dict[str, Any]:
    return {
        "language": "en",
        "pageNum": page_num,
        "pageSize": PAGE_SIZE,
        "keyword": "",
        "industry": [],
        "region": region or [],
        "time": [],
        "countryScope": country_scope,
        "joinStatus": [],
        "timeStatus": [],
        "timeRange": [],
        "includeSearchContext": False,
    }


def _parse_dates(time_text: str) -> tuple[str, str]:
    parts = re.findall(r"\d{4}[.\-]\d{2}[.\-]\d{2}", time_text)
    if not parts:
        return _clean(time_text), ""
    start = parts[0].replace(".", "-")
    end = parts[-1].replace(".", "-") if len(parts) > 1 else ""
    return start, end


def _time_status_code(item: dict[str, Any]) -> str:
    ts = item.get("timeStatus")
    if isinstance(ts, dict):
        return _clean(ts.get("code")).lower()
    return _clean(ts).lower()


def is_upcoming_item(item: dict[str, Any]) -> bool:
    """Keep Coming Soon / future starts; drop Ongoing."""
    code = _time_status_code(item)
    if code == "ongoing":
        return False
    if code in {"coming_soon", "in_days", "future", "not_started"}:
        return True
    start, _ = _parse_dates(_clean(item.get("time")))
    if re.match(r"\d{4}-\d{2}-\d{2}", start):
        return start >= date.today().isoformat()
    return code != "ongoing"


def row_from_api_item(item: dict[str, Any], source: dict[str, str]) -> dict[str, str]:
    public_id = _clean(item.get("publicId"))
    start, end = _parse_dates(_clean(item.get("time")))
    city, country = parse_expofinder_location(item.get("location") or "")
    return {
        **empty_row(source),
        "event_name": _clean(item.get("name")),
        "event_start_date": start,
        "event_end_date": end,
        "city": city,
        "country": country,
        "event_url": f"https://www.expofinder.com/en/detail/{public_id}",
    }


def append_api_items(
    payload: dict[str, Any],
    source: dict[str, str],
    seen: set[str],
    events: list[dict[str, str]],
    *,
    upcoming_only: bool,
) -> int:
    added = 0
    for item in payload.get("exhibitions") or []:
        if upcoming_only and not is_upcoming_item(item):
            continue
        public_id = _clean(item.get("publicId"))
        if not public_id or public_id in seen:
            continue
        seen.add(public_id)
        events.append(row_from_api_item(item, source))
        added += 1
    return added


def append_dom_items(
    items: list[dict[str, Any]],
    source: dict[str, str],
    seen: set[str],
    events: list[dict[str, str]],
) -> int:
    added = 0
    for item in items:
        public_id = _clean(item.get("id"))
        if not public_id or public_id in seen:
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        seen.add(public_id)
        start, end = _parse_dates(_clean(item.get("time")))
        city, country = parse_expofinder_location(item.get("loc") or "")
        events.append({
            **empty_row(source),
            "event_name": name,
            "event_start_date": start,
            "event_end_date": end,
            "city": city,
            "country": country,
            "event_url": _clean(item.get("href"))
            or f"https://www.expofinder.com/en/detail/{public_id}",
        })
        added += 1
    return added


def fetch_api_http(body: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    owns_session = session is None
    if session is None:
        session = requests.Session()
    session.headers.update({
        **HEADERS,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.expofinder.com",
        "Referer": LIST_URL,
    })
    session.get(LIST_URL, timeout=45)
    response = session.post(API_URL, json=body, timeout=60)
    response.raise_for_status()
    data = response.json()
    if owns_session:
        session.close()
    return data


def fetch_api_browser(page: Any, body: dict[str, Any]) -> dict[str, Any]:
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
        raise RuntimeError(
            f"ExpoFinder API HTTP {result.get('status')}: {result.get('text', '')[:200]}"
        )
    return json.loads(result["text"])


def paginate_api(
    source: dict[str, str],
    stats: dict[str, Any],
    fetch_payload: Callable[[dict[str, Any]], dict[str, Any]],
    seen: set[str],
    events: list[dict[str, str]],
    *,
    region: list[str] | None = None,
    country_scope: str = "",
    upcoming_only: bool,
    max_pages: int = 0,
) -> None:
    page_num = 1
    total_pages = 1
    while page_num <= total_pages:
        if max_pages and stats["pages_visited"] >= max_pages:
            stats["truncated"] = True
            break
        payload = fetch_payload(
            list_body(page_num, region=region, country_scope=country_scope)
        )
        total_pages = int(payload.get("totalPages") or 1)
        stats["pages_visited"] += 1
        append_api_items(payload, source, seen, events, upcoming_only=upcoming_only)
        page_num += 1


def list_filter_urls() -> list[str]:
    base = LIST_URL
    urls = [base]
    for region in NON_CHINA_REGIONS:
        urls.append(f"{base}?region={region}&countryScope=NON_CHINA")
    for region in CHINA_REGIONS:
        urls.append(f"{base}?region={region}&countryScope=CHINA")
    return list(dict.fromkeys(urls))


def activate_coming_soon_tab(page: Any) -> bool:
    page.wait_for_timeout(1500)
    clicked = page.evaluate(
        """() => {
          const nodes = [...document.querySelectorAll('button, a, [role="button"], span, div')];
          for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').trim();
            if (/^coming soon$/i.test(text)) {
              node.click();
              return true;
            }
          }
          return false;
        }"""
    )
    if clicked:
        page.wait_for_timeout(2500)
        LOG.info("ExpoFinder: selected Coming Soon listing tab")
        return True
    for label in ("Coming Soon", "Coming soon"):
        try:
            page.get_by_text(label, exact=False).first.click(timeout=4000)
            page.wait_for_timeout(2500)
            LOG.info("ExpoFinder: selected '%s' listing tab (fallback)", label)
            return True
        except Exception:
            continue
    LOG.warning("ExpoFinder: could not click Coming Soon tab; using date filter only")
    return False


def wait_for_listing_cards(page: Any) -> None:
    try:
        page.wait_for_selector('a[href*="/detail/"]', timeout=45000)
    except Exception:
        LOG.warning("ExpoFinder: no detail links visible yet")


def dom_scroll(
    page: Any,
    source: dict[str, str],
    stats: dict[str, Any],
    seen: set[str],
    events: list[dict[str, str]],
    *,
    max_rounds: int,
) -> None:
    stagnant = 0
    for _round in range(max_rounds):
        batch = page.evaluate(EXPOFINDER_COLLECT_JS)
        added = append_dom_items(batch, source, seen, events)
        stagnant = stagnant + 1 if added == 0 else 0
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
    LOG.info("ExpoFinder DOM scroll: %d unique events so far", len(seen))


def enrich_details(
    events: list[dict[str, str]],
    stats: dict[str, Any],
    *,
    workers: int = 20,
    max_details: int = 0,
) -> None:
    urls = []
    for event in events:
        url = (event.get("event_url") or "").strip()
        if url and "/en/detail/" in url:
            urls.append(url)
    urls = list(dict.fromkeys(urls))
    if max_details > 0:
        urls = urls[:max_details]
    if not urls:
        return

    LOG.info("ExpoFinder: fetching %d detail pages", len(urls))

    def fetch_one(detail_url: str) -> tuple[str, str]:
        try:
            resp = requests.get(detail_url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            return detail_url, resp.text
        except Exception as exc:
            LOG.debug("detail failed %s: %s", detail_url, exc)
            return detail_url, ""

    fetched: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch_one, u) for u in urls]
        for index, future in enumerate(as_completed(futures), start=1):
            url, html = future.result()
            if html:
                fetched[url] = html
            if index % 500 == 0:
                LOG.info("ExpoFinder detail progress %d / %d", index, len(urls))

    url_to_indices: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        url = (event.get("event_url") or "").strip()
        if url in fetched:
            url_to_indices.setdefault(url, []).append(index)

    for detail_url, html in fetched.items():
        detail, official = parse_expofinder_detail(html, detail_url)
        for idx in url_to_indices.get(detail_url, []):
            events[idx] = merge_detail_into_listing(events[idx], detail, official)

    stats["detail_pages_visited"] = len(fetched)


def fill_domains_serper(
    events: list[dict[str, str]],
    *,
    max_lookups: int = 0,
) -> int:
    """Optional Serper search when detail page has no official website."""
    try:
        from elib.serper_domain import SerperKeyPool, discover_domain_for_row, load_serper_keys
        from elib.paths import ENV_FILE
    except ImportError:
        LOG.warning("Serper helpers unavailable; skip domain search")
        return 0

    keys = load_serper_keys(ENV_FILE)
    if not keys:
        LOG.info("No Serper keys; skip domain search")
        return 0
    pool = SerperKeyPool(keys)
    filled = 0
    for event in events:
        if normalize_domain(event.get("event_domain") or ""):
            continue
        if max_lookups and filled >= max_lookups:
            break
        result = discover_domain_for_row(event, pool)
        domain = normalize_domain(result.get("event_domain") or "")
        if domain:
            event["event_domain"] = domain
            event["domain_source"] = result.get("domain_source") or "serper"
            filled += 1
    LOG.info("ExpoFinder Serper filled %d domains", filled)
    return filled


def scrape_expofinder(
    *,
    upcoming_only: bool = True,
    enrich: bool = True,
    serper_domains: bool = False,
    serper_max: int = 0,
    max_api_pages: int = 0,
    max_details: int = 0,
    all_regions: bool = False,
    headless: bool = True,
    browser_profile: str = "",
    login_wait_seconds: int = 0,
    session_file: str | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source = SOURCE_EXPOFINDER
    stats = base_stats()
    stats["listing_mode"] = "upcoming" if upcoming_only else "all"
    session_path = resolve_session_path(session_file)
    stats["session_file"] = str(session_path) if session_path else ""
    events: list[dict[str, str]] = []
    seen: set[str] = set()
    errors: list[str] = []

    api_session = build_requests_session(session_path, extra_headers=HEADERS) if session_path else None

    try:
        paginate_api(
            source,
            stats,
            lambda body: fetch_api_http(body, api_session),
            seen,
            events,
            upcoming_only=upcoming_only,
            max_pages=max_api_pages,
        )
        if events:
            LOG.info("ExpoFinder API collected %d listing rows with session", len(events))
    except Exception as exc:
        errors.append(str(exc))
        LOG.warning("ExpoFinder HTTP API failed (%s); using browser", exc)

    api_listing_complete = bool(
        session_path
        and events
        and not all_regions
        and not (max_api_pages and stats["pages_visited"] < max_api_pages)
    )
    if api_listing_complete:
        LOG.info("ExpoFinder: skipping browser (session API listing complete, %d rows)", len(events))

    if not api_listing_complete:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                storage_state = str(session_path) if session_path else None
                if browser_profile:
                    context = playwright.chromium.launch_persistent_context(
                        browser_profile,
                        headless=headless,
                        user_agent=HEADERS["User-Agent"],
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    browser = None
                else:
                    browser = playwright.chromium.launch(headless=headless)
                    context = browser.new_context(
                        user_agent=HEADERS["User-Agent"],
                        storage_state=storage_state,
                    )
                    page = context.new_page()
                if session_path and browser_profile:
                    loaded = load_session_file(session_path)
                    if loaded["cookies"]:
                        context.add_cookies(loaded["cookies"])

                page.goto(list_filter_urls()[0], wait_until="domcontentloaded", timeout=120000)
                if login_wait_seconds:
                    page.wait_for_timeout(login_wait_seconds * 1000)
                else:
                    page.wait_for_timeout(3500)

                if upcoming_only:
                    activate_coming_soon_tab(page)
                wait_for_listing_cards(page)

                try:
                    paginate_api(
                        source,
                        stats,
                        lambda body: fetch_api_browser(page, body),
                        seen,
                        events,
                        upcoming_only=upcoming_only,
                        max_pages=max_api_pages,
                    )
                except Exception as exc:
                    msg = str(exc)
                    errors.append(msg)
                    if "login" in msg.lower() or "3010" in msg:
                        LOG.warning(
                            "ExpoFinder API needs login; using scroll-only listing capture"
                        )
                    else:
                        LOG.warning("ExpoFinder in-browser API failed: %s", exc)

                filter_urls = list_filter_urls() if all_regions else [list_filter_urls()[0]]
                for index, list_url in enumerate(filter_urls):
                    if index:
                        page.goto(list_url, wait_until="domcontentloaded", timeout=120000)
                        page.wait_for_timeout(2500)
                        if upcoming_only:
                            activate_coming_soon_tab(page)
                        wait_for_listing_cards(page)
                    LOG.info("ExpoFinder listing view %d/%d", index + 1, len(filter_urls))
                    dom_scroll(
                        page,
                        source,
                        stats,
                        seen,
                        events,
                        max_rounds=SCROLL_PER_FILTER if index else SCROLL_MAX_ROUNDS,
                    )

                if browser:
                    browser.close()
                else:
                    context.close()
        except Exception as exc:
            errors.append(str(exc))
            LOG.warning("ExpoFinder browser session failed: %s", exc)

    if api_session:
        api_session.close()

    if upcoming_only and events:
        today = date.today().isoformat()
        filtered = [
            e
            for e in events
            if not re.match(r"\d{4}-\d{2}-\d{2}", e.get("event_start_date") or "")
            or (e.get("event_start_date") or "") >= today
        ]
        if len(filtered) < len(events):
            LOG.info(
                "ExpoFinder date filter removed %d past/ongoing rows",
                len(events) - len(filtered),
            )
        events = filtered

    if enrich and events:
        enrich_details(events, stats, max_details=max_details)

    if serper_domains and events:
        fill_domains_serper(events, max_lookups=serper_max)

    stats["raw_rows"] = len(events)
    if errors:
        stats["error"] = "; ".join(errors)
    if not events:
        stats["status"] = "loaded_no_events"
    return events, stats
