# Event listing workflow

Scrape **13 configured directory sites**, normalize events, resolve each event’s **official domain**, **deduplicate by domain + start date**, and **map TradeShowIntel / Event Intelligence**.

## Pipeline

```text
config/sources.json     →  scrape_events.py
                              ↓
                         events_scraped.csv
                         (name, start/end date, venue, city, country, listing URLs)
                              ↓
                         run_pipeline.py  (or scrape_events.py runs this automatically)
                              ↓
                         events_deduped.csv   unique rows, core fields
                         events_final.csv     deliverable + TSI columns
                         events_dedup_removed.csv
                         pipeline_report.md, dedupe_report.md, scrape_report.md
```

### Core CSV columns

| Column | Description |
|--------|-------------|
| `event_name` | Event title |
| `event_start_date` | Start date (as scraped) |
| `event_end_date` | End date |
| `event_domain` | Official event website host (blank if unknown; never a listing site) |
| `venue` | Venue name |
| `city` | City |
| `country` | Country |

### How scraping works

1. **Listing pass** — each source adapter follows pagination, “load more”, and scroll (where needed) to collect every event card.
2. **Detail pass** (default) — for each event URL, the scraper opens the **detail page** and merges name, dates, venue, city, country, and **official website** from JSON-LD and “Website” links. `event_domain` is set only when a real external host is found; otherwise it stays **blank**.
3. **Dedupe + TSI** — domain + start date deduplication, then Event Intelligence mapping.

Skip detail fetching for a faster run:

```powershell
python event-listing/scrape_events.py --skip-detail-enrich
```

Cap detail requests per source (testing):

```powershell
python event-listing/scrape_events.py --max-detail-pages 500
```

`events_final.csv` adds TSI fields: `ei_located`, `tsi_located`, `listing_tier`, `tsi_url`, `event_central_id`, and related match metadata.

### Deduplication rule

- **Key** = normalized `event_domain` + normalized `event_start_date`.
- Same key → one row kept (best EI/TSI match and fullest fields).
- Rows **without** `event_domain` are not merged in bulk; only exact name + date duplicates drop.

## Layout

```text
event-listing/
  .env                    # MONGODB_URI (read-only EI catalog)
  config/sources.json     # 13 scrape sources
  scrape_events.py        # Full scrape + pipeline
  run_pipeline.py         # Domain + dedupe + TSI only (no re-scrape)
  map_event_tsi.py        # Re-map TSI on events_deduped.csv
  dedupe_by_domain.py     # Dedupe-only CLI
  elib/                   # CSV, domains, dedupe, TSI, pipeline
  output/                 # CSVs and reports
```

## Run

From the repository root:

```powershell
python -m pip install -r event-listing/requirements.txt
python event-listing/scrape_events.py
```

Re-run post-processing without scraping:

```powershell
python event-listing/run_pipeline.py
```

Optional slow web search for missing official domains:

```powershell
python event-listing/run_pipeline.py --domain-web-search
```

Re-map TSI only:

```powershell
python event-listing/map_event_tsi.py --input event-listing/output/events_deduped.csv
```

Browser profile for login-gated sources:

```powershell
python event-listing/scrape_events.py --browser-profile .\event-listing\browser-profile
```

## Optional ranking handoff

```powershell
python combined-ranking-tsi-pipeline/main.py --input event-listing/output/events_final.csv --dry-run
```
