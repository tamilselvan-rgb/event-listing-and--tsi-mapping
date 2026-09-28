"""TSI / Event Intelligence matching for scraped event rows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from elib.fields import FIELDS, TSI_FIELDS
from elib.mongo_ei_catalog import load_events_central_catalog
from elib.paths import ENV_FILE, PIPELINE_ROOT
from elib.ranking_tsi import ranking_tsi_mapper

LOG = logging.getLogger("event-listing.tsi")


def create_tsi_engine(env_path: Path | None = None, skip_mongo: bool = False):
    """Build matcher: MongoDB catalog from ``event-listing/.env`` when available."""
    TsiEngine = ranking_tsi_mapper().TsiEngine
    env_path = env_path or ENV_FILE
    engine = TsiEngine(config_dir=PIPELINE_ROOT / "config")
    if not skip_mongo:
        mongo_catalog = load_events_central_catalog(env_path)
        if mongo_catalog:
            engine.apply_mongo_catalog(mongo_catalog)
        else:
            LOG.warning("Mongo catalog unavailable (%s); using local TSI files only", env_path)
    engine.load()
    return engine


def tsi_match_row(event: dict[str, str], engine) -> dict[str, Any]:
    if event.get("scrape_status") != "ok":
        return {
            "ei_located": "Not checked",
            "tsi_located": "Not checked",
            "event_central_id": "",
            "tradeintelshow_event_id": "",
            "tsi_url": "",
            "tsi_slug": "",
            "listing_tier": "",
            "match_method": "source_failed",
            "match_score": 0,
            "match_confidence": "none",
            "past_edition_detected": "No",
            "past_edition_slug": "",
        }
    return engine.match_event(
        event_name=event.get("event_name") or "",
        domain=event.get("event_domain") or "",
        start_date=event.get("event_start_date") or "",
        city=event.get("city") or "",
        country=event.get("country") or "",
        venue=event.get("venue") or "",
    )


def apply_tsi_mapping(
    events: list[dict[str, str]],
    engine,
) -> list[dict[str, Any]]:
    return [{**event, **tsi_match_row(event, engine)} for event in events]
