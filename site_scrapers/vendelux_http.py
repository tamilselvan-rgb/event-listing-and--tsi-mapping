"""HTTP fetch for vendelux.com (Cloudflare-protected)."""

from __future__ import annotations


def fetch_html(url: str, *, timeout: int = 90) -> str:
    try:
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(url, impersonate="chrome", timeout=timeout)
    except ImportError:
        import cloudscraper

        response = cloudscraper.create_scraper().get(url, timeout=timeout)
    response.raise_for_status()
    return response.text
