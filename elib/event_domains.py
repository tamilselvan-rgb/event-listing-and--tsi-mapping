"""Resolve official event_domain (not the listing-directory host)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient

from elib.listing_hosts import LISTING_HOSTS
from elib.paths import ENV_FILE

INVALID_PSEUDO_DOMAINS = frozenset(
    {
        "javascript",
        "mailto",
        "void",
        "undefined",
        "null",
        "none",
        "about",
        "blank",
        "http",
        "https",
    }
)


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https", ""):
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    host = host.split(":")[0]
    if "." not in host or host in INVALID_PSEUDO_DOMAINS:
        return ""
    return host


def normalize_domain(value: str) -> str:
    text = (value or "").strip().lower()
    if not text or text in INVALID_PSEUDO_DOMAINS:
        return ""
    host = host_of(text)
    if host:
        return host
    bare = text.removeprefix("www.")
    if "." in bare and bare not in LISTING_HOSTS and bare not in INVALID_PSEUDO_DOMAINS:
        return bare
    return ""


def listing_domain(row: dict[str, str]) -> str:
    for field in ("event_url", "source_url"):
        host = host_of(row.get(field) or "")
        if host:
            return host
    return ""


def load_mongo_domains(ids: list[str], env_path: Path | None = None) -> dict[str, str]:
    env_path = env_path or ENV_FILE
    load_dotenv(env_path)
    uri = (os.getenv("MONGODB_URI") or "").strip()
    if not uri:
        return {}
    db_name = (os.getenv("MONGODB_DB_NAME") or "event_intelligence").strip()
    client = MongoClient(
        uri,
        connectTimeoutMS=30000,
        serverSelectionTimeoutMS=45000,
        tlsDisableOCSPEndpointCheck=True,
    )
    db = client[db_name]
    object_ids: list[ObjectId] = []
    id_by_oid: dict[ObjectId, str] = {}
    for raw in ids:
        try:
            oid = ObjectId(raw)
        except Exception:
            continue
        object_ids.append(oid)
        id_by_oid[oid] = raw

    found: dict[str, str] = {}
    chunk = 500
    for start in range(0, len(object_ids), chunk):
        batch = object_ids[start : start + chunk]
        for doc in db.events_central.find({"_id": {"$in": batch}}, {"event_domain": 1}):
            raw_id = id_by_oid.get(doc["_id"], str(doc["_id"]))
            found[raw_id] = normalize_domain(str(doc.get("event_domain") or ""))
    client.close()
    return found


def _valid_event_domain(domain: str) -> bool:
    dom = normalize_domain(domain)
    return bool(dom) and dom not in LISTING_HOSTS and dom not in INVALID_PSEUDO_DOMAINS


def attach_event_domains(
    rows: list[dict[str, str]],
    *,
    env_path: Path | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Fill ``event_domain`` from EI Mongo, URL inference, or existing value."""
    from elib.domain_enrich import infer_domain_from_urls

    env_path = env_path or ENV_FILE
    ids = sorted(
        {(r.get("event_central_id") or "").strip() for r in rows if (r.get("event_central_id") or "").strip()}
    )
    mongo_domains = load_mongo_domains(ids, env_path)

    stats: dict[str, int] = {}
    out: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        listing = listing_domain(item)
        existing = normalize_domain(item.get("event_domain") or "")
        item["listing_domain"] = listing
        if _valid_event_domain(existing):
            item["event_domain"] = existing
            item["domain_source"] = item.get("domain_source") or "existing"
            stats["existing"] = stats.get("existing", 0) + 1
            out.append(item)
            continue

        eid = (item.get("event_central_id") or "").strip()
        official = mongo_domains.get(eid, "") if eid else ""
        if _valid_event_domain(official):
            item["event_domain"] = official
            item["domain_source"] = "event_intelligence"
            stats["event_intelligence"] = stats.get("event_intelligence", 0) + 1
        elif official and official in LISTING_HOSTS:
            item["event_domain"] = ""
            stats["ei_listing_host_cleared"] = stats.get("ei_listing_host_cleared", 0) + 1
        elif eid and eid in mongo_domains:
            item["event_domain"] = ""
            stats["ei_domain_blank"] = stats.get("ei_domain_blank", 0) + 1
        else:
            inferred = normalize_domain(infer_domain_from_urls(item))
            direct = host_of(item.get("event_url") or "")
            if not inferred and _valid_event_domain(direct):
                inferred = direct
            if _valid_event_domain(inferred):
                item["event_domain"] = inferred
                item["domain_source"] = "event_url"
                stats["event_url"] = stats.get("event_url", 0) + 1
            else:
                item["event_domain"] = ""
                stats["listing_only"] = stats.get("listing_only", 0) + 1
        out.append(item)
    return out, stats


def strip_tsi_columns(row: dict[str, Any]) -> dict[str, Any]:
    from elib.fields import TSI_MATCH_FIELDS

    cleaned = dict(row)
    for key in TSI_MATCH_FIELDS:
        cleaned.pop(key, None)
    return cleaned
