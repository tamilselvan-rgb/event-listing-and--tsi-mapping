#!/usr/bin/env python3
"""Open ExpoFinder in a browser; log in manually, then save session cookies to config/."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from site_scrapers.session_cookies import DEFAULT_EXPOFINDER_SESSION

LIST_URL = "https://www.expofinder.com/en/list"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_EXPOFINDER_SESSION,
        help="Playwright storage_state JSON path (gitignored)",
    )
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=120_000)
        print(f"Browser open: {LIST_URL}")
        print("1. Log in to ExpoFinder (if needed).")
        print("2. Confirm the exhibition list loads (cards / Coming Soon).")
        print("3. Return here and press Enter to save the session.")
        input("Press Enter when ready to save session... ")
        context.storage_state(path=str(args.output))
        browser.close()

    print(f"Saved session to {args.output}")
    print("Run: python -m site_scrapers.main expofinder --session-file", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
