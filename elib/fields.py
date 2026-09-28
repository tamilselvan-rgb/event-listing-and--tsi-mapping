"""CSV column sets for the event-listing pipeline."""

# Primary event fields (user-facing core)
EVENT_CORE_FIELDS = [
    "event_name",
    "event_start_date",
    "event_end_date",
    "event_domain",
    "venue",
    "city",
    "country",
]

# Extra scrape / traceability columns
SCRAPE_META_FIELDS = [
    "source_name",
    "source_url",
    "event_url",
    "official_site_url",
    "domain_source",
    "scraped_at",
    "scrape_status",
    "scrape_error",
]

TSI_MATCH_FIELDS = [
    "ei_located",
    "tsi_located",
    "event_central_id",
    "tradeintelshow_event_id",
    "tsi_url",
    "tsi_slug",
    "listing_tier",
    "match_method",
    "match_score",
    "match_confidence",
    "past_edition_detected",
    "past_edition_slug",
]

# Back-compat names used across scraper and TSI helpers
FIELDS = EVENT_CORE_FIELDS + SCRAPE_META_FIELDS
TSI_FIELDS = FIELDS + TSI_MATCH_FIELDS
FINAL_CSV_FIELDS = EVENT_CORE_FIELDS + TSI_MATCH_FIELDS + ["source_name"]

DEDUPE_AUDIT_FIELDS = [
    "duplicate_group_id",
    "dedupe_domain",
    "dedupe_start_date",
    "dedupe_role",
    "kept_event_name",
    "kept_source_name",
    "kept_event_url",
]
