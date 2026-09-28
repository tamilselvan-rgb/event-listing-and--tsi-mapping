"""Sanitize scraped rows: valid domains only, English text where required."""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any

from elib.event_domains import host_of, normalize_domain

LOG = logging.getLogger("event-listing.row_normalize")

# Unicode blocks for scripts we translate to English (Tamil, Devanagari, etc.)
NON_LATIN_PRIMARY = re.compile(
    r"[\u0B80-\u0BFF\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0C00-\u0C7F]"
)

TEXT_FIELDS = ("event_name", "venue", "city", "country", "event_start_date", "event_end_date")


def _bad_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return not u or u.startswith(("javascript:", "mailto:", "tel:", "data:", "#"))


def sanitize_url(url: str) -> str:
    return "" if _bad_url(url) else (url or "").strip()


@lru_cache(maxsize=4096)
def _translate_to_english(text: str) -> str:
    if not text or not NON_LATIN_PRIMARY.search(text):
        return text
    try:
        from deep_translator import GoogleTranslator

        time.sleep(0.25)
        return GoogleTranslator(source="auto", target="en").translate(text[:4900])
    except Exception as exc:
        LOG.warning("Translation failed (%s); keeping original", exc)
        return text


def to_english_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or not NON_LATIN_PRIMARY.search(text):
        return text
    return _translate_to_english(text)


def normalize_event_row(row: dict[str, str], *, force_english: bool = True) -> dict[str, str]:
    """Clean URLs/domains; optional English for non-Latin text (e.g. Trade.gov.in Tamil UI)."""
    item = dict(row)
    item["event_url"] = sanitize_url(item.get("event_url") or "")
    item["official_site_url"] = sanitize_url(item.get("official_site_url") or "")

    dom = normalize_domain(item.get("event_domain") or "")
    item["event_domain"] = dom

    if force_english and (item.get("source_name") or "").strip().lower() == "trade.gov.in":
        for field in TEXT_FIELDS:
            if item.get(field):
                item[field] = to_english_text(item[field])

    return item


def normalize_event_rows(
    rows: list[dict[str, str]],
    *,
    force_english: bool = True,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        out.append(normalize_event_row(row, force_english=force_english))
    return out
