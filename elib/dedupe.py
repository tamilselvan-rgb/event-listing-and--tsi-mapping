"""Deduplicate events by official ``event_domain`` + normalized start date."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from elib.event_domains import normalize_domain

REMOVED_EXTRA = [
    "duplicate_group_id",
    "dedupe_domain",
    "dedupe_start_date",
    "dedupe_role",
    "kept_event_name",
    "kept_source_name",
    "kept_event_url",
]


def normalize_start_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text)
    if match:
        a, b, c = match.groups()
        year = c if len(c) == 4 else f"20{c}"
        try:
            month, day = int(a), int(b)
            if month > 12:
                day, month = month, day
            return datetime(int(year), month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return re.sub(r"\s+", " ", text).lower()


def dedupe_group_key(row: dict[str, str]) -> tuple[Any, ...]:
    domain = normalize_domain(row.get("event_domain") or "")
    start = normalize_start_date(row.get("event_start_date") or "")
    if not domain:
        name = (row.get("event_name") or "").strip().lower()
        url = (row.get("event_url") or "").strip().lower()
        disambig = name or url
        return ("", start, disambig)
    return (domain, start)


def row_quality_score(row: dict[str, str]) -> int:
    score = 0
    if row.get("tsi_located") == "Yes":
        score += 100
    if row.get("ei_located") == "Yes":
        score += 50
    if (row.get("tradeintelshow_event_id") or "").strip():
        score += 25
    for field in ("event_end_date", "venue", "city", "country", "event_url", "event_name"):
        if (row.get(field) or "").strip():
            score += 2
    return score


def dedupe_by_domain_and_start_date(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = dedupe_group_key(row)
        domain = key[0] if key else ""
        start = key[1] if len(key) > 1 else ""
        groups[key].append({**row, "dedupe_domain": str(domain), "dedupe_start_date": str(start)})

    kept: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    group_summaries: list[dict[str, Any]] = []
    group_index = 0

    for key, members in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0])):
        domain = key[0] if key else ""
        start = key[1] if len(key) > 1 else ""
        if len(members) == 1:
            kept.append(members[0])
            continue

        group_index += 1
        gid = f"G{group_index:05d}"
        ranked = sorted(members, key=row_quality_score, reverse=True)
        winner = ranked[0]
        kept.append(winner)

        removed_names: list[str] = []
        for loser in ranked[1:]:
            removed_names.append(loser.get("event_name") or "")
            removed.append(
                {
                    **loser,
                    "duplicate_group_id": gid,
                    "dedupe_domain": str(domain),
                    "dedupe_start_date": str(start),
                    "dedupe_role": "removed",
                    "kept_event_name": winner.get("event_name") or "",
                    "kept_source_name": winner.get("source_name") or "",
                    "kept_event_url": winner.get("event_url") or "",
                }
            )

        group_summaries.append(
            {
                "duplicate_group_id": gid,
                "dedupe_domain": domain or "(empty)",
                "dedupe_start_date": start or "(empty)",
                "row_count": len(members),
                "removed_count": len(members) - 1,
                "kept_event_name": winner.get("event_name") or "",
                "kept_source_name": winner.get("source_name") or "",
                "removed_event_names": removed_names,
            }
        )

    stats = {
        "input_rows": len(rows),
        "output_rows": len(kept),
        "removed_rows": len(removed),
        "duplicate_groups": len(group_summaries),
    }
    return kept, removed, stats, group_summaries


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")[:200]


def write_dedupe_report(
    path: Path,
    stats: dict[str, Any],
    kept_rows: list[dict[str, str]],
    group_summaries: list[dict[str, Any]],
    *,
    kept_csv_name: str,
    removed_csv_name: str,
    max_group_rows: int = 150,
) -> None:
    tier = Counter(row.get("listing_tier") or "" for row in kept_rows)
    lines = [
        "# Deduplication report (event domain + start date)",
        "",
        f"- Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        f"- Input rows: {stats['input_rows']}",
        f"- Output rows (kept): {stats['output_rows']}",
        f"- Rows removed as duplicates: {stats['removed_rows']}",
        f"- Duplicate groups: {stats['duplicate_groups']}",
        "",
        "## Rule",
        "",
        "1. **Domain** = normalized `event_domain` (official event site).",
        "2. **Start date** = normalized `event_start_date`.",
        "3. Same domain + same start date → one row kept (best TSI/EI/field completeness).",
        "4. Rows without `event_domain` only merge when name and start date are identical.",
        "",
        "## Files",
        "",
        f"- `{kept_csv_name}` — unique events after dedup.",
        f"- `{removed_csv_name}` — removed duplicates with group id and kept row.",
        "",
        "## After dedup (TSI columns if mapped)",
        "",
        f"- In Event Intelligence: {sum(1 for r in kept_rows if r.get('ei_located') == 'Yes')}",
        f"- Published on TSI: {sum(1 for r in kept_rows if r.get('tsi_located') == 'Yes')}",
        f"- With `event_domain` filled: {sum(1 for r in kept_rows if (r.get('event_domain') or '').strip())}",
        "",
        "## Listing tier (kept)",
        "",
        "| Tier | Count |",
        "|---|---:|",
    ]
    for label, count in sorted(tier.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| {label or '(empty)'} | {count} |")

    lines += [
        "",
        f"## Largest duplicate groups (top {max_group_rows})",
        "",
        "| Group | Domain | Start date | Total | Removed | Kept event |",
        "|---|---|---|---:|---:|---|",
    ]
    for g in group_summaries[:max_group_rows]:
        lines.append(
            f"| {g['duplicate_group_id']} | {_md_cell(str(g['dedupe_domain']))} | "
            f"{_md_cell(str(g['dedupe_start_date']))} | {g['row_count']} | {g['removed_count']} | "
            f"{_md_cell(g['kept_event_name'])} |"
        )
    if len(group_summaries) > max_group_rows:
        lines.append(f"\n… and {len(group_summaries) - max_group_rows} more groups.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
