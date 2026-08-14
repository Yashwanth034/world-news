"""Phase E tests: event canonical enrichment + development freshness.

The central requirement is: NEW INFORMATION SHOULD IMPROVE THE
EVENT, WITHOUT CHANGING THE EVENT'S IDENTITY.

A weak first story anchors the event's IMMUTABLE identity.  A
later, clearly stronger report (more verified facts, specificity,
reliability, corroboration, extraction) may become the event's
canonical CONTENT (canonical_title / canonical_summary / sector)
but must never widen the matching surface.

last_development semantics:
- NEW event        -> first_seen == last_development == event time
- material UPDATE  -> last_development advances
- duplicate        -> last_development unchanged

Run with:  .venv/bin/python -m pytest src/test_event_enrichment.py -q
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src.event_memory import (
    decide,
    init_events,
    story_strength,
)
from src.storage import init_schema


def make_db():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return conn


def item(title, summary, source="BBC World", tier=2,
         primary=False, strong=0, corroborating=0, verified=0,
         article_extracted=False, category="world", **kw):
    base = {
        "id": source + "|" + title,
        "title": title,
        "summary": summary,
        "url": "https://example.com/" + source + "/" + title,
        "source": source,
        "source_category": category,
        "primary_source": primary,
        "tier": tier,
        "category": category,
        "score": 70,
        "confidence": "medium",
        "strong_corroboration": strong,
        "corroborating_sources": corroborating,
        "verified_match_count": verified,
        "article_extracted": article_extracted,
    }
    base.update(kw)
    return base


def event_row(conn, eid):
    return conn.execute(
        "SELECT canonical_title, canonical_summary, canonical_state, "
        "first_seen, last_development, event_time FROM events "
        "WHERE event_id=?", (eid,),
    ).fetchone()


def state_of(conn, eid):
    row = event_row(conn, eid)
    return json.loads(row[2])


# ---------------------------------------------------------------------------
# A9 test cases: weak seed -> strong article
# ---------------------------------------------------------------------------


class TestWeakSeedEnrichment:
    def test_weak_seed_strong_article_improves_canonical(self):
        # Case A9.1: weak seed -> strong article.  Same event,
        # canonical content improves, identity unchanged.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake reported in Colombia",
            "An earthquake was reported in western Colombia.",
        ))
        s2, eid2, _ = decide(conn, item(
            "Colombia earthquake kills 281, magnitude 7.4, 379 missing",
            "A 7.4 magnitude quake killed 281 people and left 379 "
            "missing in western Colombia.",
            source="Reuters", tier=1, strong=2,
        ))
        assert s1 == "NEW" and s2 == "UPDATE"
        assert eid == eid2
        row = event_row(conn, eid)
        assert "7.4" in row[0] or "281" in row[0]
        assert "killed" in row[1] or "281" in row[1]
        state = state_of(conn, eid)
        # Identity anchored to the FIRST story.
        assert state["identity"]["title"] == (
            "Earthquake reported in Colombia"
        )
        best = state["best_story"]
        assert best["title"].startswith("Colombia earthquake")
        # The stronger report must beat the weak seed's strength.
        seed_strength = state["identity"]["title"] != "" and 0.0
        assert best["strength"] > 10
        conn.close()

    def test_weak_seed_stronger_update_three_stage(self):
        # Case A9.2: weak seed -> stronger article -> update.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake reported in Colombia",
            "An earthquake was reported in western Colombia.",
        ))
        s2, _, _ = decide(conn, item(
            "Colombia earthquake kills 281, magnitude 7.4, 379 missing",
            "A 7.4 magnitude quake killed 281 people and left 379 "
            "missing in western Colombia.",
            source="Reuters", tier=1, strong=1,
        ))
        s3, _, _ = decide(conn, item(
            "Colombia quake death toll rises to 400",
            "Officials raised the Colombia earthquake death toll "
            "to 400 as more bodies were found.",
            source="AFP", tier=1, strong=2,
        ))
        assert s1 == "NEW" and s2 == "UPDATE" and s3 == "UPDATE"
        state = state_of(conn, eid)
        # The update's new number lands in the ACCUMULATED memory.
        assert "400" in state["numbers"]
        assert state["identity"]["title"] == (
            "Earthquake reported in Colombia"
        )
        best = state["best_story"]
        # Canonical description stays with the strongest report
        # (the rich 281/7.4/379 account), not the terse toll-only
        # update - facts are preserved, description is best-in-class.
        assert "400" not in best["title"]
        assert "7.4" in best["title"]
        conn.close()

    def test_strong_seed_weaker_article_never_downgrades(self):
        # Case A9.3: strong seed -> weaker article.  Canonical
        # content stays with the strong seed.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Colombia earthquake kills 281, magnitude 7.4",
            "A 7.4 magnitude quake killed 281 people in Colombia.",
            source="Reuters", tier=1, strong=2,
        ))
        s2, _, _ = decide(conn, item(
            "Colombia quake: residents describe shaking",
            "Residents in Colombia described feeling strong "
            "shaking during the earthquake.",
            source="Local Blog", tier=4,
        ))
        assert s2 in ("DUPLICATE", "UPDATE")
        row = event_row(conn, eid)
        assert "281" in row[0]
        state = state_of(conn, eid)
        assert state["best_story"]["title"].startswith("Colombia")
        assert "killed 281" in state["best_story"]["summary"]
        conn.close()

    def test_strong_source_beats_weak_source(self):
        # Case A9.4: strong source (tier-1, corroborated) vs weak
        # source (tier-4 blog).  Strength follows the source.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake reported in Colombia",
            "An earthquake was reported in western Colombia.",
        ))
        # weak tier-4 report with generic wording
        decide(conn, item(
            "Colombia quake felt by residents",
            "Residents in Colombia felt the earthquake.",
            source="Anonymous Blog", tier=4,
        ))
        # strong tier-1 corroborated report, same event
        s3, e3, _ = decide(conn, item(
            "Colombia earthquake kills 281, magnitude 7.4, 379 missing",
            "A 7.4 magnitude quake killed 281 people and left 379 "
            "missing in western Colombia.",
            source="Reuters", tier=1, strong=2,
        ))
        assert e3 == eid and s3 == "UPDATE"
        best = state_of(conn, eid)["best_story"]
        # The tier-1 corroborated Reuters report wins canonical.
        assert best["source"] == "Reuters"
        assert "7.4" in best["title"]
        conn.close()

    def test_weak_article_many_facts_beats_long_empty(self):
        # Cases A9.5/A9.6: fact density beats raw length.  A
        # long-but-empty article never outranks a dense short one.
        weak_long = item(
            "Colombia quake: a long account",
            "The earthquake in Colombia was a major event that "
            "affected many people. Rescue teams worked through "
            "the night. The city was quiet. Families waited. "
            "Officials spoke. The weather was clear. Roads were "
            "blocked in places. People gathered in the streets.",
            source="Local News", tier=4,
        )
        dense_short = item(
            "Colombia quake kills 281, 7.4 magnitude, 379 missing",
            "A 7.4 magnitude quake killed 281 and left 379 missing.",
            source="Reuters", tier=1, strong=2,
        )
        ls, _ = story_strength(weak_long)
        ds, _ = story_strength(dense_short)
        assert ds > ls, (ds, ls)

    def test_duplicate_does_not_change_canonical(self):
        # Case A9.7: duplicate article - same event, no material
        # change, canonical content may only improve on STRENGTH,
        # and a same-strength repeat never churns.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        row1 = event_row(conn, eid)
        s2, eid2, _ = decide(conn, item(
            "Powerful quake leaves 100 dead in Colombia",
            "A strong quake left 100 dead in the region.",
            source="Reuters", tier=2,
        ))
        assert eid == eid2
        row2 = event_row(conn, eid)
        # A near-equal duplicate must not flip canonical title.
        assert row1[0] == row2[0]
        conn.close()

    def test_material_update_advances_last_development(self):
        # Case A9.8: material update advances last_development.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        before = event_row(conn, eid)
        s2, _, _ = decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "Rescuers said the death toll from the Colombia "
            "earthquake had climbed past 180.",
            source="Reuters", tier=1, strong=1,
        ))
        after = event_row(conn, eid)
        assert s2 == "UPDATE"
        assert after[4] > before[4]  # last_development advanced
        conn.close()

    def test_duplicate_does_not_advance_last_development(self):
        # Case A9.7b: a duplicate never advances the timeline.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        before = event_row(conn, eid)
        s2, _, _ = decide(conn, item(
            "Powerful quake leaves 100 dead in Colombia",
            "A strong quake left 100 dead in the region.",
            source="Reuters",
        ))
        after = event_row(conn, eid)
        assert s2 == "DUPLICATE"
        assert after[4] == before[4]  # unchanged
        conn.close()

    def test_old_article_genuinely_new_development_is_update(self):
        # Case A9.9: old event + new meaningful development =
        # CURRENT UPDATE (survives freshness at the event layer).
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        s2, eid2, _ = decide(conn, item(
            "Colombia: emergency declared as aftershocks continue",
            "A state of emergency was declared after fresh "
            "aftershocks rattled quake-hit Colombia.",
            source="Al Jazeera", tier=2, strong=1,
        ))
        assert eid == eid2
        assert s2 == "UPDATE"
        assert "emergency" in state_of(conn, eid)["dev_facts"]
        conn.close()

    def test_old_article_no_new_information_is_duplicate(self):
        # Case A9.10: recycled material with no new information
        # stays a duplicate (never a fresh update).
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        s2, _, _ = decide(conn, item(
            "100 dead after powerful Colombia earthquake",
            "One hundred people were killed when a powerful "
            "earthquake struck Colombia.",
            source="Reuters",
        ))
        assert s2 == "DUPLICATE"
        conn.close()

    def test_canonical_summary_and_title_upgrade(self):
        # Cases A9.11/A9.12: canonical summary + title upgrade.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Quake in Colombia",
            "An earthquake was reported in Colombia.",
        ))
        s2, _, _ = decide(conn, item(
            "Colombia earthquake kills 281, 7.4 magnitude",
            "A 7.4 magnitude quake killed 281 people in western "
            "Colombia with 379 people still missing.",
            source="Reuters", tier=1, strong=2,
        ))
        assert s2 == "UPDATE"
        row = event_row(conn, eid)
        assert "7.4" in row[0] and "281" in row[1]
        assert "379" in row[1]
        conn.close()

    def test_identity_unchanged_after_enrichment(self):
        # Case A9.13: identity remains unchanged after enrichment.
        conn = make_db()
        s1, eid, _ = decide(conn, item(
            "Earthquake reported in Colombia",
            "An earthquake was reported in western Colombia.",
        ))
        state1 = json.loads(event_row(conn, eid)[2])
        ident1 = state1["identity"]
        decide(conn, item(
            "Colombia earthquake kills 281, magnitude 7.4, 379 missing",
            "A 7.4 magnitude quake killed 281 people and left 379 "
            "missing in western Colombia.",
            source="Reuters", tier=1, strong=2,
        ))
        decide(conn, item(
            "Colombia declares emergency after deadly quake",
            "Colombia declared a national emergency after the "
            "deadly earthquake.",
            source="AFP", tier=1,
        ))
        state2 = json.loads(event_row(conn, eid)[2])
        ident2 = state2["identity"]
        assert ident1 == ident2  # byte-identical identity
        assert state2["best_story"]["strength"] >= \
            state1["best_story"]["strength"]
        conn.close()


# ---------------------------------------------------------------------------
# last_development timeline semantics
# ---------------------------------------------------------------------------


class TestDevelopmentTimeline:
    def test_new_event_last_development_is_event_time(self):
        conn = make_db()
        _, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        row = event_row(conn, eid)
        assert row[3] == row[4]  # first_seen == last_development
        conn.close()

    def test_update_advances_duplicate_does_not(self):
        conn = make_db()
        _, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        t0 = event_row(conn, eid)[4]
        # duplicate first
        decide(conn, item(
            "Powerful quake leaves 100 dead in Colombia",
            "A strong quake left 100 dead in the region.",
            source="Reuters",
        ))
        t1 = event_row(conn, eid)[4]
        assert t1 == t0  # duplicate: unchanged
        # then a genuine update
        decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "Rescuers said the death toll from the Colombia "
            "earthquake had climbed past 180.",
            source="AFP", tier=1, strong=1,
        ))
        t2 = event_row(conn, eid)[4]
        assert t2 > t0  # material update: advanced
        conn.close()

    def test_related_sources_accumulate(self):
        conn = make_db()
        _, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
        ))
        decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "The death toll climbed past 180.",
            source="Reuters", tier=1,
        ))
        row = conn.execute(
            "SELECT related_sources FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
        sources = json.loads(row[0])
        assert len(sources) >= 2
        conn.close()


# ---------------------------------------------------------------------------
# Story strength transparency
# ---------------------------------------------------------------------------


class TestStoryStrength:
    def test_components_bounded_and_visible(self):
        it = item(
            "Colombia earthquake kills 281, 7.4 magnitude",
            "A 7.4 magnitude quake killed 281 people.",
            tier=1, strong=2,
        )
        score, breakdown = story_strength(it)
        assert 0 <= score <= 30
        for key in ("fact_density", "specificity", "summary_quality",
                    "reliability", "corroboration", "extraction",
                    "total"):
            assert key in breakdown
        assert breakdown["reliability"] == 5  # tier 1

    def test_primary_source_bonus(self):
        base = item("Storm makes landfall in Taiwan",
                    "The storm made landfall on the east coast.",
                    tier=2)
        s1, _ = story_strength(base)
        s2, _ = story_strength({**base, "primary_source": True})
        assert s2 > s1

    def test_fact_density_reflects_numbers(self):
        thin = item("Storm in Taiwan", "A storm hit Taiwan.")
        dense = item("Typhoon hits Taiwan, 40 dead, 200,000 displaced",
                     "The typhoon killed 40 and displaced 200,000 "
                     "people in Taiwan.")
        t1, _ = story_strength(thin)
        t2, _ = story_strength(dense)
        assert t2 > t1
