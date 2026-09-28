"""Official event domain discovery (web search) and optional location hints from event sites."""

from __future__ import annotations

import json
import logging
import re
import sys
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from elib.paths import PACKAGE_ROOT, PIPELINE_ROOT

if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from src.domain_resolver import DomainResolver, is_aggregator_domain  # noqa: E402
from src.domain_utils import extract_domain  # noqa: E402

LOG = logging.getLogger("event-listing.domain_enrich")

from elib.listing_hosts import LISTING_HOSTS

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}


def infer_domain_from_urls(row: dict[str, str]) -> str:
    for field in ("event_url", "official_site_url"):
        dom = extract_domain(row.get(field) or "")
        if dom and dom not in LISTING_HOSTS and not is_aggregator_domain(dom):
            return dom
    return ""


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(text or ""))).strip()


def fill_location_from_official_site(row: dict[str, str], timeout: int = 8) -> dict[str, str]:
    """If venue/city/country missing, try JSON-LD on the official site URL."""
    if row.get("venue") and row.get("city") and row.get("country"):
        return row
    site = (row.get("official_site_url") or "").strip()
    if not site:
        return row
    try:
        resp = requests.get(site, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return row
        soup = BeautifulSoup(resp.text, "html.parser")
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.string or node.get_text() or "{}")
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") not in ("Event", "BusinessEvent", "ExhibitionEvent", None):
                    if "@type" in item and "Event" not in str(item.get("@type", "")):
                        continue
                loc = item.get("location") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = loc.get("address") if isinstance(loc, dict) else {}
                if isinstance(addr, str):
                    addr = {"streetAddress": addr}
                updated = dict(row)
                if not updated.get("venue"):
                    updated["venue"] = _clean(loc.get("name") if isinstance(loc, dict) else "")
                if not updated.get("city"):
                    updated["city"] = _clean(addr.get("addressLocality") if isinstance(addr, dict) else "")
                if not updated.get("country"):
                    updated["country"] = _clean(addr.get("addressCountry") if isinstance(addr, dict) else "")
                return updated
    except Exception as exc:
        LOG.debug("Location fetch failed for %s: %s", site, exc)
    return row


def enrich_events_with_domains(
    events: list[dict[str, str]],
    *,
    max_workers: int = 8,
    use_cache: bool = True,
    on_progress: Any = None,
) -> list[dict[str, str]]:
    """Discover official ``event_domain`` via web search when not already set."""
    cache_dir = PACKAGE_ROOT / "cache"
    resolver = DomainResolver(cache_dir=cache_dir, use_cache=use_cache)

    prepared: list[dict[str, str]] = []
    for row in events:
        item = dict(row)
        if not (item.get("event_domain") or "").strip():
            inferred = infer_domain_from_urls(item)
            if inferred:
                item["event_domain"] = inferred
                item["domain_source"] = "listing_url"
        prepared.append(item)

    # Map to resolver shape (uses event_domain when ignore_input_domains=False)
    resolver_input = [{**e, "event_domain": e.get("event_domain", "")} for e in prepared]
    enriched = resolver.resolve_events_parallel(
        resolver_input,
        max_workers=max_workers,
        ignore_input_domains=False,
        use_cache=use_cache,
        on_progress=on_progress,
    )

    out: list[dict[str, str]] = []
    for raw, resolved in zip(prepared, enriched):
        merged = {**raw}
        dom = (resolved.get("domain") or "").strip()
        if dom:
            merged["event_domain"] = dom
        merged["domain_source"] = resolved.get("domain_source") or merged.get("domain_source") or ""
        merged["official_site_url"] = resolved.get("source_url") or merged.get("official_site_url") or ""
        if not merged.get("event_domain") and resolved.get("third_party_domain"):
            merged["domain_source"] = "listing_only"
        merged = fill_location_from_official_site(merged)
        out.append(merged)
    return out
