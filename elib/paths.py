"""Canonical paths for the event-listing workflow."""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_ROOT / "config"
OUTPUT_DIR = PACKAGE_ROOT / "output"
ENV_FILE = PACKAGE_ROOT / ".env"
PIPELINE_ROOT = PACKAGE_ROOT.parent / "combined-ranking-tsi-pipeline"
SOURCES_FILE = CONFIG_DIR / "sources.json"
