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
    conn.commit()


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
