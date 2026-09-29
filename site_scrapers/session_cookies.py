"""Load ExpoFinder (or other) session cookies from a local JSON file — never commit this file."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

LOG = logging.getLogger("event-listing.site_scrapers.session")

DEFAULT_EXPOFINDER_SESSION = (
    Path(__file__).resolve().parent.parent / "config" / "expofinder_session.json"
)


def _normalize_playwright_cookie(raw: dict[str, Any]) -> dict[str, Any]:
    cookie = {
        "name": str(raw.get("name") or ""),
        "value": str(raw.get("value") or ""),
        "domain": str(raw.get("domain") or ".expofinder.com"),
        "path": str(raw.get("path") or "/"),
    }
    if not cookie["name"]:
        return {}
    if raw.get("expires") is not None:
        cookie["expires"] = float(raw["expires"])
    if "httpOnly" in raw:
        cookie["httpOnly"] = bool(raw["httpOnly"])
    if "secure" in raw:
        cookie["secure"] = bool(raw["secure"])
    if raw.get("sameSite"):
        cookie["sameSite"] = raw["sameSite"]
    return cookie


def load_session_file(path: Path) -> dict[str, Any]:
    """Return {cookies: [...], storage_state: bool, raw: ...}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "cookies" in data:
        cookies = [
            c for c in (_normalize_playwright_cookie(x) for x in data["cookies"]) if c
        ]
        return {"cookies": cookies, "storage_state": True, "path": path}
    if isinstance(data, list):
        cookies = [c for c in (_normalize_playwright_cookie(x) for x in data) if c]
        return {"cookies": cookies, "storage_state": False, "path": path}
    if isinstance(data, dict) and "Cookie" in data:
        return _parse_cookie_header(str(data["Cookie"]))
    raise ValueError(
        f"Unsupported session format in {path}. "
        "Use Playwright storage_state JSON, a cookie array, or {{\"Cookie\": \"a=b; ...\"}}."
    )


def _parse_cookie_header(header: str) -> dict[str, Any]:
    cookies: list[dict[str, Any]] = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append(
            _normalize_playwright_cookie(
                {"name": name.strip(), "value": value.strip(), "domain": ".expofinder.com"}
            )
        )
    cookies = [c for c in cookies if c]
    return {"cookies": cookies, "storage_state": False, "path": None}


def apply_cookies_to_requests(session: requests.Session, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or ".expofinder.com",
            path=cookie.get("path") or "/",
        )


def build_requests_session(
    cookies_file: Path | None,
    *,
    extra_headers: dict[str, str] | None = None,
) -> requests.Session:
    session = requests.Session()
    if extra_headers:
        session.headers.update(extra_headers)
    if cookies_file and cookies_file.is_file():
        loaded = load_session_file(cookies_file)
        apply_cookies_to_requests(session, loaded["cookies"])
        LOG.info("Loaded %d cookies from %s", len(loaded["cookies"]), cookies_file)
    return session


def resolve_session_path(explicit: str | Path | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    env_path = (os.environ.get("EXPOFINDER_SESSION_FILE") or "").strip()
    if env_path:
        path = Path(env_path)
        return path if path.is_file() else None
    if DEFAULT_EXPOFINDER_SESSION.is_file():
        return DEFAULT_EXPOFINDER_SESSION
    return None
