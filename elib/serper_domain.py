"""Official event domain discovery via Serper (Google Search API)."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from elib.event_domains import normalize_domain
from elib.listing_hosts import LISTING_HOSTS
from elib.paths import ENV_FILE, PACKAGE_ROOT, PIPELINE_ROOT

if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from src.domain_resolver import _matches_event_brand, is_aggregator_domain  # noqa: E402
from src.domain_utils import extract_domain  # noqa: E402

LOG = logging.getLogger("event-listing.serper")
SERPER_URL = "https://google.serper.dev/search"
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


class SerperKeyPool:
    """Round-robin Serper API keys; cooldown on 429 instead of permanent disable."""

    def __init__(self, keys: list[str], min_interval_sec: float = 0.22) -> None:
        self._keys = [k.strip() for k in keys if k.strip()]
        if not self._keys:
            raise ValueError("No Serper API keys configured")
        self._lock = threading.Lock()
        self._index = 0
        self._cooldown_until: dict[str, float] = {k: 0.0 for k in self._keys}
        self._disabled: set[str] = set()
        self._min_interval = min_interval_sec
        self._last_call: dict[str, float] = {k: 0.0 for k in self._keys}

    def acquire(self) -> str:
        while True:
            with self._lock:
                now = time.monotonic()
                live = [
                    k
                    for k in self._keys
                    if k not in self._disabled and self._cooldown_until.get(k, 0) <= now
                ]
                if not live:
                    wait = min(
                        (self._cooldown_until.get(k, now) - now for k in self._keys if k not in self._disabled),
                        default=1.0,
                    )
                else:
                    key = live[self._index % len(live)]
                    self._index += 1
                    elapsed = now - self._last_call.get(key, 0)
                    if elapsed < self._min_interval:
                        wait = self._min_interval - elapsed
                    else:
                        self._last_call[key] = now
                        return key
            time.sleep(max(wait, 0.05))

    def cooldown(self, key: str, seconds: float = 8.0) -> None:
        with self._lock:
            self._cooldown_until[key] = time.monotonic() + seconds
            LOG.warning("Serper key cooldown %.0fs (%d keys active)", seconds, len(self._keys) - len(self._disabled))

    def disable(self, key: str) -> None:
        with self._lock:
            self._disabled.add(key)
            LOG.warning("Serper key disabled (%d keys left)", len(self._keys) - len(self._disabled))


def load_serper_keys(env_path: Path | None = None) -> list[str]:
    load_dotenv(env_path or ENV_FILE)
    raw = (os.getenv("SERPER_API_KEYS") or os.getenv("SERPER_API_KEY") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def build_search_query(row: dict[str, str]) -> str:
    name = (row.get("event_name") or "").strip()
    parts = [name]
    city = (row.get("city") or "").strip()
    country = (row.get("country") or "").strip()
    if city and city.lower() not in name.lower():
        parts.append(city)
    elif country and country.lower() not in name.lower():
        parts.append(country)
    year = _YEAR_RE.search(name) or _YEAR_RE.search(row.get("event_start_date") or "")
    if year and year.group(1) not in name:
        parts.append(year.group(1))
    parts.append("official website")
    return " ".join(parts)


def pick_domain_from_organic(organic: list[dict[str, Any]], event_name: str) -> tuple[str, str]:
    for item in organic:
        link = (item.get("link") or "").strip()
        if not link:
            continue
        dom = normalize_domain(extract_domain(link))
        if not dom or dom in LISTING_HOSTS or is_aggregator_domain(dom):
            continue
        if _matches_event_brand(dom, event_name) or not event_name:
            return dom, link
    for item in organic:
        link = (item.get("link") or "").strip()
        dom = normalize_domain(extract_domain(link))
        if dom and dom not in LISTING_HOSTS and not is_aggregator_domain(dom):
            return dom, link
    return "", ""


def serper_search(query: str, key_pool: SerperKeyPool, timeout: int = 25) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(len(key_pool._keys) * 2):
        api_key = key_pool.acquire()
        try:
            resp = requests.post(
                SERPER_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 8, "gl": "us", "hl": "en"},
                timeout=timeout,
            )
            if resp.status_code in (401, 403):
                key_pool.disable(api_key)
                continue
            if resp.status_code == 429:
                key_pool.cooldown(api_key, seconds=10.0)
                time.sleep(0.5)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if "429" in str(exc):
                key_pool.disable(api_key)
            continue
    raise RuntimeError(f"Serper search failed: {last_error}")


def discover_domain_for_row(row: dict[str, str], key_pool: SerperKeyPool) -> dict[str, str]:
    name = (row.get("event_name") or "").strip()
    if not name:
        return {"event_domain": "", "official_site_url": "", "domain_source": ""}
    query = build_search_query(row)
    try:
        data = serper_search(query, key_pool)
    except Exception as exc:
        LOG.debug("Serper failed for %s: %s", name[:60], exc)
        return {"event_domain": "", "official_site_url": "", "domain_source": "serper_error"}
    organic = data.get("organic") or []
    dom, url = pick_domain_from_organic(organic, name)
    if dom:
        return {"event_domain": dom, "official_site_url": url, "domain_source": "serper"}
    return {"event_domain": "", "official_site_url": "", "domain_source": "serper_no_match"}


class SerperDomainCache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (PACKAGE_ROOT / "cache" / "serper_domains.json")
        self._data: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}
        self.purge_errors()

    def purge_errors(self) -> int:
        """Remove failed API entries so the next run can retry Serper."""
        with self._lock:
            before = len(self._data)
            self._data = {
                key: value
                for key, value in self._data.items()
                if value.get("domain_source") not in ("serper_error", "")
                or value.get("event_domain")
            }
            return before - len(self._data)

    def cache_key(self, row: dict[str, str]) -> str:
        return "|".join(
            [
                (row.get("event_name") or "").strip().lower(),
                (row.get("event_start_date") or "").strip()[:10],
                (row.get("city") or "").strip().lower(),
            ]
        )

    def get(self, row: dict[str, str]) -> dict[str, str] | None:
        hit = self._data.get(self.cache_key(row))
        if not hit:
            return None
        if hit.get("domain_source") == "serper_error":
            return None
        return hit

    def set(self, row: dict[str, str], result: dict[str, str]) -> None:
        if result.get("domain_source") == "serper_error":
            return
        with self._lock:
            self._data[self.cache_key(row)] = result

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
