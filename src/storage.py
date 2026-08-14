"""Database schema for the website-ready event/article data model.

This module owns the *storage* schema of the two core tables:

    stories   - one row per collected article (the raw item record)
    events    - one row per canonical event (immutable identity +
                accumulated observability metadata)

It is deliberately separate from `event_memory.py`, which owns the
matching/identity logic and is never changed by schema work here.
The schema is migrated in place, idempotently: running the
migration on an existing database only adds missing columns with
safe defaults and never drops or rewrites data.

New fields are storage/observability fields for the future website
(sector / region / country / entities / event timeline / related
sources / verification).  They do NOT feed event matching, dedup or
scoring, and they are NOT shown on Telegram.
"""

import json
import sqlite3

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

STORY_COLUMNS = [
    # legacy core fields (unchanged semantics)
    ("id", "TEXT PRIMARY KEY"),
    ("title", "TEXT"),
    ("url", "TEXT"),
    ("source", "TEXT"),
    ("category", "TEXT"),
    ("summary", "TEXT"),
    ("score", "INTEGER"),
    ("confidence", "TEXT"),
    ("event_id", "TEXT"),
    ("event_status", "TEXT"),
    ("first_seen", "TEXT"),
    # website-ready observability fields (added by this phase)
    ("sector", "TEXT"),
    ("subsector", "TEXT"),
    ("region", "TEXT"),
    ("subregion", "TEXT"),
    ("country", "TEXT"),
    ("entities", "TEXT"),          # JSON list
    ("published_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("event_time", "TEXT"),
    ("last_seen", "TEXT"),
    ("verification", "TEXT"),      # JSON object
]

EVENT_COLUMNS = [
    ("event_id", "TEXT PRIMARY KEY"),
    ("canonical_title", "TEXT NOT NULL"),
    ("category", "TEXT"),
    ("first_seen", "TEXT NOT NULL"),
    ("last_seen", "TEXT NOT NULL"),
    ("major", "INTEGER DEFAULT 0"),
    ("queued_count", "INTEGER DEFAULT 0"),
    ("canonical_summary", "TEXT DEFAULT ''"),
    ("canonical_state", "TEXT DEFAULT '{}'"),
    # website-ready observability fields (added by this phase)
    ("sector", "TEXT"),
    ("subsector", "TEXT"),
    ("region", "TEXT"),
    ("subregion", "TEXT"),
    ("country", "TEXT"),
    ("entities", "TEXT"),          # JSON list (accumulated)
    ("event_time", "TEXT"),
    ("last_development", "TEXT"),  # latest meaningful development time
    ("related_sources", "TEXT"),   # JSON list of source names
    ("verification", "TEXT"),      # JSON object
]

EVENT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_event_time "
    "ON events(event_time)",
    "CREATE INDEX IF NOT EXISTS idx_events_last_development "
    "ON events(last_development)",
    "CREATE INDEX IF NOT EXISTS idx_events_sector "
    "ON events(sector)",
    "CREATE INDEX IF NOT EXISTS idx_events_region "
    "ON events(region)",
    "CREATE INDEX IF NOT EXISTS idx_events_country "
    "ON events(country)",
    "CREATE INDEX IF NOT EXISTS idx_events_major "
    "ON events(major)",
]


def create_stories(conn):
    """Create the stories table (idempotent) with the full
    website-ready column set."""
    cols = ",\n        ".join(
        f"{name} {decl}"
        for name, decl in STORY_COLUMNS
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS stories(
            {cols}
        )
        """
    )
    _add_missing_columns(conn, "stories", STORY_COLUMNS)


def create_events(conn):
    """Create the events table (idempotent) with the full
    website-ready column set."""
    cols = ",\n            ".join(
        f"{name} {decl}"
        for name, decl in EVENT_COLUMNS
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS events(
            {cols}
        )
        """
    )
    _add_missing_columns(conn, "events", EVENT_COLUMNS)
    for index_sql in EVENT_INDEXES:
        conn.execute(index_sql)


def _add_missing_columns(conn, table, columns):
    """Add any missing columns to an existing table.

    Idempotent: columns that already exist (whatever their
    declared type) are left untouched.  This is the safe upgrade
    path for databases created before the website-ready fields
    existed.
    """
    existing = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }
    for name, decl in columns:
        if name in existing:
            continue
        base = decl.split()[0]
        default = ""
        if "TEXT" in decl and name != "id" and name != "event_id":
            # New observability columns default to NULL so legacy
            # rows read as null/unknown rather than an invented
            # empty value.  Id/event_id stay NOT NULL (they always
            # exist in practice and the ALTER path only fires for
            # malformed tables).
            default = " DEFAULT NULL"
        elif "INTEGER" in decl:
            default = " DEFAULT 0"
        conn.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {name} {base}{default}"
        )


def init_schema(conn):
    """Migrate an existing connection to the full schema.

    Safe to run on every collection cycle: creating an existing
    table is a no-op and missing columns are added only once.
    """
    create_stories(conn)
    create_events(conn)
    create_source_health(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Source-health history
# ---------------------------------------------------------------------------

SOURCE_HEALTH_COLUMNS = [
    ("source_id", "TEXT PRIMARY KEY"),
    ("source_name", "TEXT"),
    ("source_type", "TEXT"),
    ("tier", "INTEGER"),
    ("region", "TEXT"),
    ("sector", "TEXT"),
    # cumulative run-level counters (never reset)
    ("attempt_count", "INTEGER DEFAULT 0"),
    ("success_count", "INTEGER DEFAULT 0"),
    ("failure_count", "INTEGER DEFAULT 0"),
    ("articles_fetched", "INTEGER DEFAULT 0"),
    ("articles_accepted", "INTEGER DEFAULT 0"),
    ("articles_rejected", "INTEGER DEFAULT 0"),
    ("duplicates_generated", "INTEGER DEFAULT 0"),
    ("editorial_rejected_count", "INTEGER DEFAULT 0"),
    ("summarized_count", "INTEGER DEFAULT 0"),
    ("published_count", "INTEGER DEFAULT 0"),
    # timestamps / state
    ("last_success", "TEXT"),
    ("last_failure", "TEXT"),
    ("last_article_at", "TEXT"),
    ("last_seen", "TEXT"),
    ("last_error", "TEXT"),
]


def create_source_health(conn):
    cols = ",\n    ".join(
        f"{name} {decl}"
        for name, decl in SOURCE_HEALTH_COLUMNS
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS source_health(
            {cols}
        )
        """
    )
    _add_missing_columns(conn, "source_health", SOURCE_HEALTH_COLUMNS)


def _error_class(error):
    """Classify a fetch error string into a safe category.

    Only the category is stored - never the raw error body, which
    could contain URLs, headers or untrusted feed content.
    """
    if not error:
        return None
    low = str(error).lower()
    if "403" in low or "forbidden" in low:
        return "HTTP_403"
    if "404" in low or "not found" in low:
        return "HTTP_404"
    if "timeout" in low or "timed out" in low:
        return "TIMEOUT"
    if "dns" in low or "name or service not known" in low \
            or "nodename nor servname" in low:
        return "DNS_ERROR"
    if "bozo" in low or "parse" in low or "syntax" in low \
            or "xml" in low:
        return "PARSE_ERROR"
    if "connection" in low or "refused" in low:
        return "CONNECTION_ERROR"
    return "OTHER"


def record_source_health(conn, run, now_iso, feeds=None):
    """Merge one run's per-source metrics into the persistent
    source_health table (additive/historical).

    `run` maps source name -> dict with:
        attempted (bool), failed (bool), error (str|None),
        fetched, accepted, duplicates, editorial_rejected,
        summarized, queued

    Counters accumulate across runs (attempt_count = sum of all
    runs); last_success / last_failure / last_error are the most
    recent values, never overwritten by older history.  Returns
    the number of sources updated.
    """
    feeds = feeds or {}
    updated = 0
    for name, m in (run or {}).items():
        feed = feeds.get(name) or {}
        attempted = bool(m.get("attempted"))
        failed = bool(m.get("failed"))
        # Per-run deltas (added to whatever the table already
        # holds, so history accumulates across runs).
        attempt = 1 if attempted else 0
        success = 1 if (attempted and not failed) else 0
        failure = 1 if (attempted and failed) else 0

        error_class = _error_class(m.get("error")) if failed else None

        conn.execute(
            """
            INSERT INTO source_health(
                source_id, source_name, source_type, tier, region,
                sector, attempt_count, success_count, failure_count,
                articles_fetched, articles_accepted, articles_rejected,
                duplicates_generated, editorial_rejected_count,
                summarized_count, published_count, last_success,
                last_failure, last_article_at, last_seen, last_error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_name=excluded.source_name,
                source_type=excluded.source_type,
                tier=excluded.tier,
                region=excluded.region,
                sector=excluded.sector,
                attempt_count=source_health.attempt_count+excluded.attempt_count,
                success_count=source_health.success_count+excluded.success_count,
                failure_count=source_health.failure_count+excluded.failure_count,
                articles_fetched=source_health.articles_fetched+excluded.articles_fetched,
                articles_accepted=source_health.articles_accepted+excluded.articles_accepted,
                articles_rejected=source_health.articles_rejected+excluded.articles_rejected,
                duplicates_generated=source_health.duplicates_generated+excluded.duplicates_generated,
                editorial_rejected_count=source_health.editorial_rejected_count+excluded.editorial_rejected_count,
                summarized_count=source_health.summarized_count+excluded.summarized_count,
                published_count=source_health.published_count+excluded.published_count,
                last_success=COALESCE(excluded.last_success, source_health.last_success),
                last_failure=COALESCE(excluded.last_failure, source_health.last_failure),
                last_article_at=COALESCE(excluded.last_article_at, source_health.last_article_at),
                last_seen=excluded.last_seen,
                last_error=COALESCE(excluded.last_error, source_health.last_error)
            """,
            (
                name, feed.get("name") or name,
                feed.get("type"), feed.get("tier"),
                feed.get("region"), feed.get("sector"),
                attempt, success, failure,
                int(m.get("fetched", 0)),
                int(m.get("accepted", 0)),
                int(m.get("editorial_rejected", 0))
                + int(m.get("rejected", 0)),
                int(m.get("duplicates", 0)),
                int(m.get("editorial_rejected", 0)),
                int(m.get("summarized", 0)),
                int(m.get("published", 0)),
                (now_iso if not failed else None),
                (now_iso if failed else None),
                (m.get("last_article_at") or now_iso
                 if m.get("fetched") else None),
                now_iso,
                error_class,
            ),
        )
        updated += 1
    conn.commit()
    return updated


def sector_source_counts(conn):
    """Sector -> number of distinct sources that have contributed
    stories to that sector in the persistent database.

    Used by the importance model as the trusted coverage signal
    (Phase C source intelligence).  Empty/None sectors are
    skipped; a sector absent from the map gets no adjustment.
    """
    try:
        rows = conn.execute(
            "SELECT sector, COUNT(DISTINCT source) "
            "FROM stories "
            "WHERE sector IS NOT NULL AND sector != '' "
            "GROUP BY sector"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}
    except Exception:
        return {}


def source_health_rows(conn):
    """All persistent source-health rows as dicts."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM source_health ORDER BY source_id"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def as_json(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _first_present(item, *keys, default=None):
    for key in keys:
        value = item.get(key)
        if value:
            return value
    return default


def event_time_of(item):
    """Best available timestamp for 'when the event happened'.

    Prefers the feed's effective time (newest of published /
    updated), then published_at, then the record time.
    """
    return _first_present(
        item,
        "effective_at",
        "published_at",
        "updated_at",
        default=None,
    )


def story_meta(item, now_iso):
    """Extra story-row values derived from a pipeline item.

    Returns a dict keyed by the stories columns that are NOT part
    of the legacy 11-column insert.  Values that cannot be derived
    confidently are None (stored as NULL) rather than invented.
    """
    entities = item.get("entities")
    return {
        "sector": item.get("sector"),
        "subsector": item.get("subsector"),
        "region": item.get("region"),
        "subregion": item.get("subregion"),
        "country": item.get("country"),
        "entities": as_json(entities) if entities else None,
        "published_at": item.get("published_at"),
        "updated_at": item.get("updated_at"),
        "event_time": event_time_of(item),
        "last_seen": now_iso,
        "verification": as_json(
            {
                key: item.get(key)
                for key in (
                    "tier",
                    "primary_source",
                    "corroborating_sources",
                    "strong_corroboration",
                    "verified_match_count",
                )
                if item.get(key) is not None
            }
        ),
    }


def event_meta(item, state, now_iso, canonical_title=None,
                canonical_summary=None, advance_development=False):
    """Extra events-row values derived from the event's canonical
    (immutable) identity text and its ACCUMULATED state.

    Sector / region / country describe the EVENT, so they are
    computed from the canonical title+summary - a later article
    about the same event (possibly classified differently or about
    a sub-aspect) must never retag the event.  The item's values
    are used only when no canonical text is available (defensive
    fallback for legacy/corrupt rows).

    `advance_development` is True only for a genuine UPDATE (a
    material development): last_development is then moved to the
    incoming story's effective time.  Duplicate articles never
    advance it - the requirement is that a mere repeat of the same
    event must not move the timeline.
    """
    from src.sectors import classify_sector
    from src.regions import classify_event_region

    canonical_title = canonical_title or state.get("title") or ""
    canonical_summary = canonical_summary or state.get("summary") or ""
    if canonical_title:
        sector, subsector = classify_sector(
            canonical_title,
            canonical_summary,
        )
        region, subregion = classify_event_region(
            canonical_title,
            canonical_summary,
        )
    else:
        # Defensive fallback: no canonical text available.
        sector = item.get("sector")
        subsector = item.get("subsector")
        region = item.get("region")
        subregion = item.get("subregion")

    entities = sorted(set(state.get("entities") or []))
    sources = sorted(set(state.get("sources") or []))
    development = event_time_of(item) or now_iso
    return {
        "sector": sector,
        "subsector": subsector,
        "region": region,
        "subregion": subregion,
        "country": item.get("country"),
        "entities": as_json(entities) if entities is not None else None,
        "event_time": event_time_of(item),
        "last_development": (
            development
            if advance_development
            else None  # None -> keep the stored value
        ),
        "related_sources": (
            as_json(sources) if sources is not None else None
        ),
        "verification": as_json(
            {
                key: item.get(key)
                for key in (
                    "tier",
                    "primary_source",
                    "corroborating_sources",
                    "strong_corroboration",
                    "verified_match_count",
                )
                if item.get(key) is not None
            }
        ),
    }
