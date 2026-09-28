"""Post-scrape pipeline: domains → dedupe → TSI mapping."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from elib.csv_io import write_csv
from elib.dedupe import REMOVED_EXTRA, dedupe_by_domain_and_start_date, write_dedupe_report
from elib.domain_enrich import enrich_events_with_domains
from elib.event_domains import attach_event_domains, strip_tsi_columns
from elib.fields import DEDUPE_AUDIT_FIELDS, FIELDS, FINAL_CSV_FIELDS, TSI_FIELDS
from elib.paths import ENV_FILE, OUTPUT_DIR
from elib.tsi_mapping import apply_tsi_mapping, create_tsi_engine

LOG = logging.getLogger("event-listing.pipeline")

EVENTS_SCRAPED = OUTPUT_DIR / "events_scraped.csv"
EVENTS_DEDUPED = OUTPUT_DIR / "events_deduped.csv"
EVENTS_FINAL = OUTPUT_DIR / "events_final.csv"
EVENTS_DEDUP_REMOVED = OUTPUT_DIR / "events_dedup_removed.csv"
PIPELINE_REPORT = OUTPUT_DIR / "pipeline_report.md"
DEDUPE_REPORT = OUTPUT_DIR / "dedupe_report.md"


def _input_already_tsi_mapped(events: list[dict[str, str]]) -> bool:
    if not events:
        return False
    sample = events[0].get("ei_located") or ""
    return sample in ("Yes", "No")


def finalize_events(
    events: list[dict[str, str]],
    *,
    env_path: Path | None = None,
    skip_mongo: bool = False,
    skip_domain_web_search: bool = True,
    domain_workers: int = 8,
    output_dir: Path | None = None,
    scrape_stats: dict[str, Any] | None = None,
    fast_existing_tsi: bool = False,
    final_csv_name: str = "events_final.csv",
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """
    1. Preliminary TSI match (for domain lookup + dedupe ranking), unless skipped.
    2. Attach official ``event_domain`` (EI Mongo + URL inference).
    3. Optional web search for missing domains.
    4. Dedupe by ``event_domain`` + start date.
    5. Final TSI mapping on kept rows.
    """
    env_path = env_path or ENV_FILE
    out_dir = output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = create_tsi_engine(env_path=env_path, skip_mongo=skip_mongo)
    use_fast = fast_existing_tsi or _input_already_tsi_mapped(events)

    if use_fast:
        LOG.info("Using existing TSI columns for domain lookup + dedupe ranking (fast path).")
        with_domains_input = events
    else:
        base = [strip_tsi_columns(e) for e in events]
        LOG.info("Preliminary TSI match on %d scraped rows (for domains + dedupe ranking)...", len(base))
        with_domains_input = apply_tsi_mapping(base, engine)

    with_domains, domain_stats = attach_event_domains(with_domains_input, env_path=env_path)
    LOG.info("Event domains: %s", domain_stats)

    if not skip_domain_web_search:
        LOG.info("Web search for missing official domains...")
        with_domains = enrich_events_with_domains(
            with_domains,
            max_workers=max(1, domain_workers),
            use_cache=True,
            on_progress=lambda msg: LOG.info("%s", msg),
        )
        with_domains, domain_stats = attach_event_domains(with_domains, env_path=env_path)

    kept, removed, dedupe_stats, group_summaries = dedupe_by_domain_and_start_date(with_domains)
    LOG.info(
        "Deduped: %d kept, %d removed, %d groups",
        dedupe_stats["output_rows"],
        dedupe_stats["removed_rows"],
        dedupe_stats["duplicate_groups"],
    )

    final_base = [strip_tsi_columns(r) for r in kept]
    final_rows = apply_tsi_mapping(final_base, engine)

    write_csv(out_dir / EVENTS_DEDUPED.name, final_base, FIELDS + ["listing_domain"])
    write_csv(out_dir / final_csv_name, final_rows, TSI_FIELDS)
    if final_csv_name != EVENTS_FINAL.name:
        write_csv(out_dir / EVENTS_FINAL.name, final_rows, FINAL_CSV_FIELDS)
    write_csv(out_dir / EVENTS_DEDUP_REMOVED.name, removed, FIELDS + ["listing_domain"] + DEDUPE_AUDIT_FIELDS)

    write_dedupe_report(
        out_dir / DEDUPE_REPORT.name,
        dedupe_stats,
        final_rows,
        group_summaries,
        kept_csv_name=EVENTS_FINAL.name,
        removed_csv_name=EVENTS_DEDUP_REMOVED.name,
    )
    write_pipeline_report(
        out_dir / PIPELINE_REPORT.name,
        scrape_stats=scrape_stats or {},
        domain_stats=domain_stats,
        dedupe_stats=dedupe_stats,
        final_rows=final_rows,
    )
    return final_rows, removed, {**dedupe_stats, "domain": domain_stats}


def write_pipeline_report(
    path: Path,
    *,
    scrape_stats: dict[str, Any],
    domain_stats: dict[str, int],
    dedupe_stats: dict[str, Any],
    final_rows: list[dict[str, Any]],
) -> None:
    tier = Counter(row.get("listing_tier") or "" for row in final_rows)
    lines = [
        "# Event listing pipeline report",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        "## Flow",
        "",
        "1. Scrape configured sources (`config/sources.json`).",
        "2. Write `events_scraped.csv` (name, dates, venue, city, country, listing URLs).",
        "3. Resolve **event_domain** (Event Intelligence + direct event URLs).",
        "4. **Deduplicate** by `event_domain` + `event_start_date`.",
        "5. **TSI / Event Intelligence** match on unique rows → `events_final.csv`.",
        "",
        "## Scrape",
        "",
        f"- Raw rows scraped: {scrape_stats.get('raw_rows', '—')}",
        f"- After scrape-level dedup: {scrape_stats.get('unique_rows', '—')}",
        f"- Sources configured: {scrape_stats.get('source_count', '—')}",
        "",
        "## Event domain resolution",
        "",
    ]
    for key, count in sorted(domain_stats.items(), key=lambda x: -x[1]):
        lines.append(f"- {key}: {count}")
    lines += [
        "",
        "## Deduplication (domain + start date)",
        "",
        f"- Input: {dedupe_stats.get('input_rows', 0)}",
        f"- Kept: {dedupe_stats.get('output_rows', 0)}",
        f"- Removed: {dedupe_stats.get('removed_rows', 0)}",
        f"- Duplicate groups: {dedupe_stats.get('duplicate_groups', 0)}",
        "",
        "## Final TSI mapping (`events_final.csv`)",
        "",
        f"- Rows: {len(final_rows)}",
        f"- In Event Intelligence: {sum(1 for r in final_rows if r.get('ei_located') == 'Yes')}",
        f"- Published on TSI: {sum(1 for r in final_rows if r.get('tsi_located') == 'Yes')}",
        f"- EI not published: {tier.get('EI not published', 0)}",
        f"- Not in Event Intelligence: {tier.get('Not in Event Intelligence', 0)}",
        "",
        "## Outputs",
        "",
        "| File | Description |",
        "|---|---|",
        "| `events_scraped.csv` | All scraped events (before domain dedup) |",
        "| `events_deduped.csv` | Unique events, core fields only |",
        "| `events_final.csv` | **Deliverable** — core fields + TSI columns |",
        "| `events_dedup_removed.csv` | Duplicate rows removed in step 4 |",
        "| `dedupe_report.md` | Dedup summary |",
        "| `scrape_report.md` | Per-source scrape status |",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
