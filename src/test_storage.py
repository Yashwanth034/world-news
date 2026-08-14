"""Tests for the website-ready event/article data model (Phase B).

Covers schema creation, safe idempotent migration of legacy
databases, persistence of sector / region / country / entities /
timestamps / related sources / verification metadata, the
event-timeline rule (last_development advances ONLY on UPDATE),
and future-website-style queries.

The verified Telegram/event-memory behavior is untouched by these
changes; the final test in this file pins the Telegram message
format.

Run with:  .venv/bin/python -m pytest src/test_storage.py -q
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src.storage import (
    EVENT_COLUMNS,
    STORY_COLUMNS,
    event_time_of,
    init_schema,
    story_meta,
)
from src.event_memory import decide, init_events, story_entities

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

T0 = "2026-08-14T06:00:00+00:00"
T1 = "2026-08-14T07:00:00+00:00"
T2 = "2026-08-14T08:00:00+00:00"
NOW = "2026-08-14T09:00:00+00:00"


def item(title, summary, eff=None, source="BBC", **kw):
    base = {
        "id": source + "|" + title,
        "title": title,
        "summary": summary,
        "url": "https://example.com/" + source + "/" + title,
        "source": source,
        "source_category": "world",
        "primary_source": False,
        "tier": 2,
        "category": "world",
        "score": 80,
        "confidence": "medium",
        "effective_at": eff,
        "published_at": eff,
        "sector": "climate",
        "subsector": "earthquakes",
        "region": "Asia",
        "subregion": "East Asia",
        "country": None,
        "entities": ["tokyo", "japan"],
    }
    base.update(kw)
    return base


def new_db():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    init_events(conn)
    return conn


def legacy_db():
    """A database with the PRE-Phase-B schema (11-col stories,
    9-col events) plus one historical story row."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE stories(
            id TEXT PRIMARY KEY, title TEXT, url TEXT, source TEXT,
            category TEXT, summary TEXT, score INTEGER,
            confidence TEXT, event_id TEXT, event_status TEXT,
            first_seen TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE events(
            event_id TEXT PRIMARY KEY,
            canonical_title TEXT NOT NULL,
            category TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            major INTEGER DEFAULT 0,
            queued_count INTEGER DEFAULT 0,
            canonical_summary TEXT DEFAULT '',
            canonical_state TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute(
        "INSERT INTO stories VALUES("
        "'legacy1','Old story','http://x','BBC','world','old',60,"
        "'medium',NULL,NULL,'2026-01-01')"
    )
    conn.commit()
    return conn


def story_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(stories)")}


def event_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(events)")}


# ---------------------------------------------------------------------------
# A. schema creation / migration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_fresh_db_has_full_column_set(self):
        conn = new_db()
        assert {"sector", "subsector", "region", "subregion", "country",
                "entities", "published_at", "updated_at", "event_time",
                "last_seen", "verification"} <= story_cols(conn)
        assert {"sector", "subsector", "region", "subregion", "country",
                "entities", "event_time", "last_development",
                "related_sources", "verification"} <= event_cols(conn)

    def test_legacy_db_upgrades_in_place(self):
        conn = legacy_db()
        before = set(STORY_COLUMNS) | set(EVENT_COLUMNS)
        init_schema(conn)
        init_events(conn)
        assert {"sector", "entities", "verification"} <= story_cols(conn)
        assert {"last_development", "related_sources"} <= event_cols(conn)
        # historical row preserved, new fields NULL (not fabricated)
        row = conn.execute(
            "SELECT title, sector, region, event_id FROM stories "
            "WHERE id='legacy1'"
        ).fetchone()
        assert row[0] == "Old story"
        assert row[1] is None and row[2] is None and row[3] is None

    def test_migration_is_idempotent(self):
        conn = legacy_db()
        init_schema(conn)
        cols1 = story_cols(conn)
        ev_cols1 = event_cols(conn)
        init_schema(conn)  # second run must be a no-op
        assert story_cols(conn) == cols1
        assert event_cols(conn) == ev_cols1
        # and the events table path (init_events) too
        init_events(conn)
        assert story_cols(conn) == cols1

    def test_legacy_events_still_readable_and_matchable(self):
        conn = legacy_db()
        init_schema(conn)
        init_events(conn)
        status, eid, _ = decide(
            conn,
            item(
                "Earthquake kills 100 people in Japan",
                "A powerful quake struck Tokyo killing 100.",
                T0,
            ),
        )
        assert status == "NEW"
        ev = conn.execute(
            "SELECT sector, region, last_development, event_time "
            "FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
        assert ev[0] == "climate" and ev[1] == "Asia"
        assert ev[2] == T0 and ev[3] == T0


# ---------------------------------------------------------------------------
# B. persistence of the new metadata
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_sector_and_subsector_persisted(self):
        conn = new_db()
        _, eid, _ = decide(conn, item("Quake hits Japan", "summary", T0))
        ev = conn.execute(
            "SELECT sector, subsector FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        assert ev == ("climate", "earthquakes")

    def test_source_region_and_event_region_are_separate(self):
        # The event region must come from the article text, not the
        # publisher: a Guardian (UK) story about Chile -> South America.
        conn = new_db()
        it = item(
            "Earthquake strikes Chile",
            "A powerful quake hit Santiago.",
            T0,
            source="Guardian",
            region="South America",
            subregion="South America",
        )
        _, eid, _ = decide(conn, it)
        ev = conn.execute(
            "SELECT region, subregion FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        assert ev[0] == "South America"

    def test_country_null_when_unknown(self):
        # Country is only stored when confidently identified; it stays
        # NULL otherwise (never invented).
        conn = new_db()
        it = item(
            "Quake hits coastal region",
            "A powerful quake struck a coastal region.",
            T0,
        )
        _, eid, _ = decide(conn, it)
        ev = conn.execute(
            "SELECT country FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        assert ev[0] is None

    def test_entities_persisted_as_json(self):
        conn = new_db()
        it = item(
            "Earthquake kills 100 people in Japan",
            "A powerful quake struck Tokyo killing 100.",
            T0,
        )
        _, eid, _ = decide(conn, it)
        raw = conn.execute(
            "SELECT entities FROM events WHERE event_id=?", (eid,)
        ).fetchone()[0]
        assert isinstance(raw, str)
        assert "japan" in json.loads(raw)

    def test_story_entities_helper_returns_sorted_list(self):
        ents = story_entities(
            "Typhoon Yagi hits Philippines",
            "Typhoon Yagi slammed Manila.",
        )
        assert ents == sorted(set(ents))
        assert "yagi" in ents

    def test_event_time_and_last_development_on_new(self):
        conn = new_db()
        _, eid, _ = decide(conn, item("Quake hits Japan", "summary", T0))
        ev = conn.execute(
            "SELECT event_time, last_development FROM events "
            "WHERE event_id=?",
            (eid,),
        ).fetchone()
        assert ev[0] == T0
        assert ev[1] == T0  # NEW: last_development = initial event time

    def test_duplicate_does_not_advance_last_development(self):
        conn = new_db()
        _, eid, _ = decide(
            conn,
            item("Earthquake kills 100 in Japan", "Quake in Tokyo.", T0),
        )
        status, eid2, _ = decide(
            conn,
            item(
                "Powerful quake leaves 100 dead in Tokyo",
                "A strong quake in Japan left 100 dead.",
                T1,
                source="Guardian",
            ),
        )
        assert status == "DUPLICATE" and eid == eid2
        ev = conn.execute(
            "SELECT last_development FROM events WHERE event_id=?", (eid,)
        ).fetchone()
        assert ev[0] == T0  # a mere repeat never moves the timeline

    def test_update_advances_last_development(self):
        conn = new_db()
        _, eid, _ = decide(
            conn,
            item("Earthquake kills 100 in Japan", "Quake in Tokyo.", T0),
        )
        status, eid2, _ = decide(
            conn,
            item(
                "Japan earthquake death toll rises to 180",
                "Officials in Tokyo raised the toll to 180.",
                T2,
                source="AFP",
            ),
        )
        assert status == "UPDATE" and eid == eid2
        ev = conn.execute(
            "SELECT event_time, last_development FROM events "
            "WHERE event_id=?",
            (eid,),
        ).fetchone()
        assert ev[0] == T0       # event_time stays anchored
        assert ev[1] == T2       # UPDATE advances the timeline

    def test_event_sector_anchored_to_canonical_story(self):
        """A merged story about the same event (classified under a
        different sector) must not retag the EVENT.  The event
        keeps the canonical story's sector/region."""
        conn = new_db()
        _, eid, _ = decide(
            conn,
            item(
                "Earthquake in Colombia kills 250",
                "A powerful 7.4 quake killed more than 250 people.",
                T0,
            ),
        )
        # A follow-up from another source about survivors/search
        # gets classified differently (health/medicine terms), but
        # the event must stay climate/earthquakes.
        decide(
            conn,
            item(
                "Colombia quake: rescue teams dig for survivors",
                "Hospitals in Cali are treating the wounded from the quake.",
                T1,
                source="AFP",
                sector="health",
                subsector="medicine",
                region="South America",
                subregion="South America",
            ),
        )
        ev = conn.execute(
            "SELECT sector, subsector, region FROM events "
            "WHERE event_id=?",
            (eid,),
        ).fetchone()
        assert ev[0] == "climate" and ev[1] == "earthquakes"
        assert ev[2] == "South America"

    def test_related_sources_accumulate(self):
        conn = new_db()
        _, eid, _ = decide(conn, item("Quake hits Japan", "summary", T0))
        decide(
            conn,
            item(
                "Quake hits Japan again",
                "Same quake reported by another outlet.",
                T1,
                source="Guardian",
            ),
        )
        decide(
            conn,
            item(
                "Japan quake toll rises",
                "Officials raised the toll.",
                T2,
                source="AFP",
            ),
        )
        raw = conn.execute(
            "SELECT related_sources FROM events WHERE event_id=?", (eid,)
        ).fetchone()[0]
        assert set(json.loads(raw)) == {"BBC", "Guardian", "AFP"}

    def test_verification_metadata_persisted(self):
        conn = new_db()
        it = item(
            "Quake hits Japan",
            "summary",
            T0,
            primary_source=True,
            tier=1,
            corroborating_sources=3,
            strong_corroboration=2,
            verified_match_count=5,
        )
        _, eid, _ = decide(conn, it)
        raw = conn.execute(
            "SELECT verification FROM events WHERE event_id=?", (eid,)
        ).fetchone()[0]
        ver = json.loads(raw)
        assert ver["tier"] == 1 and ver["primary_source"] is True
        assert ver["corroborating_sources"] == 3
        assert ver["strong_corroboration"] == 2

    def test_story_meta_maps_item_fields(self):
        it = item(
            "Quake hits Japan",
            "summary",
            T0,
            published_at=T0,
            updated_at=T1,
        )
        meta = story_meta(it, NOW)
        assert meta["sector"] == "climate"
        assert meta["subsector"] == "earthquakes"
        assert meta["region"] == "Asia"
        assert meta["subregion"] == "East Asia"
        assert meta["country"] is None
        assert meta["event_time"] == T0
        assert meta["last_seen"] == NOW
        assert meta["published_at"] == T0
        assert meta["updated_at"] == T1
        assert "japan" in json.loads(meta["entities"])
        assert json.loads(meta["verification"])["tier"] == 2

    def test_event_time_of_prefers_effective(self):
        it = item(
            "Quake hits Japan",
            "summary",
            T0,
            published_at=T0,
            updated_at=T2,
        )
        assert event_time_of(it) == T0


# ---------------------------------------------------------------------------
# C. future-website-style queries
# ---------------------------------------------------------------------------


class TestFutureQueries:
    def _seed(self):
        conn = new_db()
        # (id, title, sector, subsector, region, country, entities,
        #  event_time, last_seen, published_at, source)
        rows = [
            ("s1", "AI chip breakthrough", "technology", "ai",
             "North America", "US", '["nvidia"]',
             "2026-08-14T01:00:00+00:00", "2026-08-14T04:00:00+00:00",
             "2026-08-14T01:00:00+00:00", "SCMP"),
            ("s2", "Cyberattack on bank", "technology", "cybersecurity",
             "Europe", "UK", '["bank-of-england"]',
             "2026-08-14T02:00:00+00:00", "2026-08-14T03:00:00+00:00",
             "2026-08-14T02:00:00+00:00", "Guardian"),
            ("s3", "Rupee falls against dollar", "economy", "currencies",
             "Asia", "India", '["rupee"]',
             "2026-08-14T03:00:00+00:00", "2026-08-14T05:00:00+00:00",
             "2026-08-14T03:00:00+00:00", "The Hindu"),
            ("s4", "Earthquake in Chile", "climate", "earthquakes",
             "South America", "Chile", '["chile"]',
             "2026-08-13T10:00:00+00:00", "2026-08-13T10:00:00+00:00",
             "2026-08-13T10:00:00+00:00", "AFP"),
            ("s5", "Fed rate decision", "economy", "banking",
             "North America", "US", '["fed"]',
             "2026-08-13T20:00:00+00:00", "2026-08-13T22:00:00+00:00",
             "2026-08-13T20:00:00+00:00", "Reuters"),
        ]
        for r in rows:
            conn.execute(
                """INSERT INTO stories(
                    id, title, sector, subsector, region, country,
                    entities, event_time, last_seen, published_at,
                    source, url, category, summary, score,
                    confidence, event_id, event_status, first_seen
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                    r[7], r[8], r[9], r[10], "http://x/" + r[0],
                    "world", "sum", 70, "medium", "ev-" + r[0],
                    "NEW", r[7],
                ),
            )
        conn.commit()
        return conn

    def _titles(self, conn, sql, *args):
        return [r[0] for r in conn.execute(sql, args).fetchall()]

    def test_all_technology_events(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE sector='technology'",
        )
        assert set(rows) == {"AI chip breakthrough", "Cyberattack on bank"}

    def test_all_finance_events(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE sector='economy'",
        )
        assert set(rows) == {"Rupee falls against dollar", "Fed rate decision"}

    def test_all_events_in_india(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE country='India'",
        )
        assert rows == ["Rupee falls against dollar"]

    def test_all_events_in_asia(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE region='Asia'",
        )
        assert rows == ["Rupee falls against dollar"]

    def test_all_cybersecurity_events(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE subsector='cybersecurity'",
        )
        assert rows == ["Cyberattack on bank"]

    def test_events_involving_chosen_entity(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE entities LIKE ?",
            '%"nvidia"%',
        )
        assert rows == ["AI chip breakthrough"]

    def test_most_recently_updated_events(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories ORDER BY last_seen DESC LIMIT 3",
        )
        assert rows == [
            "Rupee falls against dollar",   # 05:00
            "AI chip breakthrough",         # 04:00
            "Cyberattack on bank",          # 03:00
        ]

    def test_events_by_source(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE source='The Hindu'",
        )
        assert rows == ["Rupee falls against dollar"]

    def test_event_timeline_ordered_by_development_time(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories ORDER BY event_time ASC",
        )
        assert rows == [
            "Earthquake in Chile",
            "Fed rate decision",
            "AI chip breakthrough",
            "Cyberattack on bank",
            "Rupee falls against dollar",
        ]

    def test_events_updated_in_last_24_hours(self):
        conn = self._seed()
        rows = self._titles(
            conn,
            "SELECT title FROM stories WHERE last_seen >= ?",
            "2026-08-14T00:00:00+00:00",
        )
        assert set(rows) == {
            "AI chip breakthrough",
            "Cyberattack on bank",
            "Rupee falls against dollar",
        }


# ---------------------------------------------------------------------------
# D. Telegram output unchanged
# ---------------------------------------------------------------------------


class TestTelegramUnchanged:
    def test_telegram_message_format_pinned(self):
        """The Phase-B metadata must never leak into Telegram output.

        The item carries the new website-ready fields; the rendered
        message must look exactly like the verified format (same
        label, headline, 2-4 sentences, source line, read-more
        link) and contain none of the internal metadata.
        """
        from src.test_telegram import CFG, briefing_item, build_message

        item = briefing_item(
            label="\U0001F4F0 NEWS",
            headline="Earthquake kills 100 people in Japan",
        )
        item["sector"] = "climate"
        item["subsector"] = "earthquakes"
        item["region"] = "Asia"
        item["subregion"] = "East Asia"
        item["country"] = "Japan"
        item["entities"] = ["japan"]
        item["verification"] = {"tier": 2}

        msg = build_message(item, CFG)
        text = msg["text"]
        assert text.startswith("\U0001F4F0 NEWS\n\n")
        assert "Earthquake kills 100 people in Japan" in text
        assert "Source: BBC World" in text
        assert "Read the full report" in text
        # internal website metadata never leaks into the message
        for leaked in ("climate", "earthquakes", "Asia", "japan",
                       "sector", "event_id", "tier"):
            assert leaked not in text, f"metadata leaked: {leaked}"
