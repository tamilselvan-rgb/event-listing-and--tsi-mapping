"""Read-only Event Intelligence (MongoDB) catalog for scraped event matching."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from elib.ranking_tsi import ranking_tsi_mapper

LOG = logging.getLogger("event-listing.mongo_ei")

TSI_EVENTS_BASE = "https://tradeshowintel.com/events"
_PRO_PUBLISHED = frozenset({"PUBLISHED", "READY_TO_PUBLISH"})


def _mongo_tls_extras() -> dict:
    out: dict = {}
    if os.getenv("MONGODB_TLS_DISABLE_OCSP", "").strip().lower() in ("1", "true", "yes"):
        out["tlsDisableOCSPEndpointCheck"] = True
    ca = os.getenv("MONGODB_TLS_CAFILE", "").strip()
    if ca.lower() == "certifi":
        try:
            import certifi

            out["tlsCAFile"] = certifi.where()
        except ImportError:
            LOG.warning("MONGODB_TLS_CAFILE=certifi but certifi is not installed")
    elif ca:
        out["tlsCAFile"] = ca
    return out


def _listing_tier(pro_status: str, tradeintelshow_event_id: str) -> str:
    if pro_status in _PRO_PUBLISHED:
        return "Pro"
    if str(tradeintelshow_event_id or "").strip():
        return "Basic"
    return "EI not published"


def load_events_central_catalog(env_path: Path):
    """
    Load events_central + pro enrichment status from MongoDB (read-only).

    Returns None if env is missing, URI unset, or connection fails.
    """
    from pymongo import MongoClient

    tsi = ranking_tsi_mapper()
    TsiCatalogItem = tsi.TsiCatalogItem
    _slug_is_weak = tsi._slug_is_weak

    if not env_path.is_file():
        LOG.warning("Mongo env file not found: %s", env_path)
        return None

    load_dotenv(env_path)
    uri = (os.getenv("MONGODB_URI") or "").strip()
    if not uri:
        LOG.warning("MONGODB_URI not set in %s", env_path)
        return None

    db_name = (os.getenv("MONGODB_DB_NAME") or "event_intelligence").strip()
    try:
        client = MongoClient(
            uri,
            connectTimeoutMS=30000,
            serverSelectionTimeoutMS=45000,
            **_mongo_tls_extras(),
        )
        client.admin.command("ping")
        db = client[db_name]

        pro_status: dict[str, str] = {}
        for doc in db.event_tsi_pro_enrichments.find(
            {},
            {"event_central_id": 1, "status": 1},
        ):
            cid = doc.get("event_central_id")
            if cid is not None:
                pro_status[str(cid)] = str(doc.get("status") or "")

        catalog = {}
        cursor = db.events_central.find(
            {"event_name": {"$exists": True, "$ne": ""}},
            {
                "event_name": 1,
                "event_slug": 1,
                "tradeintelshow_event_id": 1,
                "event_domain": 1,
            },
        )
        for doc in cursor:
            name = str(doc.get("event_name") or "").strip()
            if not name:
                continue
            eid = str(doc["_id"])
            slug = str(doc.get("event_slug") or "").strip()
            tid = str(doc.get("tradeintelshow_event_id") or "").strip()
            tier = _listing_tier(pro_status.get(eid, ""), tid)
            if slug and not _slug_is_weak(slug):
                key = slug
            else:
                key = f"ei-{eid}"
            if key in catalog:
                key = f"ei-{eid}"
            tsi_url = f"{TSI_EVENTS_BASE}/{slug}" if slug and tid else ""
            catalog[key] = TsiCatalogItem(
                event_id=eid,
                event_name=name,
                event_slug=slug,
                tradeintelshow_event_id=tid,
                listing_tier=tier,
                tsi_url=tsi_url,
                event_domain=str(doc.get("event_domain") or "").strip(),
            )
        client.close()
        LOG.info(
            "Event Intelligence MongoDB catalog loaded (read-only): %d events from %s",
            len(catalog),
            db_name,
        )
        return catalog
    except Exception as exc:
        LOG.warning("MongoDB catalog load failed (%s); file-based catalog only", exc)
        return None
