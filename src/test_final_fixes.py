"""Final fix-pass regression tests (C1, C2, M1-M4, L1).

C1  thin discovery items must not create weak standalone events
C2  events.last_development is strictly monotonic
M1  live-blog headline/body coherence requires substantive overlap
M2  political "landslide" never creates disaster-landslide identity
M3  opinion/analysis headline formats rejected
M4  anniversary/retrospective framing rejected without a development
L1  dateline/truncation artifacts stripped or rejected

Run with:  .venv/bin/python -m pytest src/test_final_fixes.py -q
"""
import sqlite3

from src.event_memory import decide, init_events
from src.storage import init_schema
from src.editorial import editorial_eligibility
from src.telegram_briefing import clean_sentence_text
from src.telegram_summarizer import summarize_rows

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_db():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return conn


def item(title, summary, source="BBC World", tier=2,
         discovery=False, effective_at=None, **kw):
    base = {
        "id": source + "|" + title,
        "title": title,
        "summary": summary,
        "url": "https://example.com/" + source + "/" + title,
        "source": source,
        "source_category": "world",
        "primary_source": False,
        "tier": tier,
        "category": "world",
        "score": 70,
        "confidence": "medium",
        "discovery": discovery,
    }
    if effective_at:
        base["effective_at"] = effective_at
    base.update(kw)
    return base


# Minimal pipeline config for the C1 enrichment tests: the
# article allowlist is derived from the feed domains, so the
# test URL's domain must be listed.
CONFIG_DICT = {
    "article_extraction": {
        "enabled": True,
        "max_fetches_per_run": 15,
        "max_article_sentences": 12,
        "min_domain_interval_seconds": 0,
    },
    "feeds": [
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "DW", "url": "https://rss.dw.com/rdf/rss-en-world"},
    ],
}


def event_row(conn, event_id):
    return conn.execute(
        "SELECT canonical_title, canonical_summary, "
        "first_seen, last_development, event_time "
        "FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()


def state_of(conn, event_id):
    import json
    row = conn.execute(
        "SELECT canonical_state FROM events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return json.loads(row[0])


def event_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]


def event_ids(conn):
    return [
        r[0]
        for r in conn.execute(
            "SELECT event_id FROM events ORDER BY event_id"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# C1 - thin discovery items never create weak standalone events
# ---------------------------------------------------------------------------


class TestThinDiscoveryHold:
    def test_thin_story_enriched_identity_merges_with_strong_report(self):
        # The confirmed Uttarakhand case, fixed via the user's
        # option B: the thin Al Jazeera one-liner is enriched
        # from its full article BEFORE event memory, so its
        # identity carries the real facts and the SCMP report
        # merges into the SAME event (one post, not two).
        from src.article_extractor import (
            enrich_thin_story_before_event_memory,
            OK_STATUS,
        )
        aj = item(
            "Seven killed in tunnel accident at Indian hydropower project",
            "Water and debris burst into a state-run hydropower "
            "tunnel in Uttarakhand following a landslide.",
            source="Al Jazeera", tier=2,
            url="https://www.aljazeera.com/news/2026/8/14/"
                "seven-killed-in-tunnel-accident",
        )
        article_text = (
            "At least seven people were killed and three remained "
            "trapped after a tunnel at the Vishnugad-Pipalkoti "
            "hydropower project in Uttarakhand collapsed, officials "
            "said.\nWater and debris burst into the tunnel following "
            "a landslide late on Thursday night.\nRescue teams from "
            "the National Disaster Response Force were working to "
            "reach the trapped workers, Chief Minister Pushkar Singh "
            "Dhami said."
        )
        def mock_fetcher(url, cfg, allowlist, robots=None, pace=None):
            return (OK_STATUS, {"text": article_text, "title": aj["title"]})

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        enriched, stats = enrich_thin_story_before_event_memory(
            aj, CONFIG_DICT, now, fetcher=mock_fetcher, max_fetches=5,
        )
        assert stats["expanded"] == 1
        assert len(enriched["article_sentences"]) >= 2
        assert "collapsed" in enriched["summary"]

        conn = make_db()
        s1, e1, _ = decide(conn, enriched)
        assert s1 == "NEW"
        s2, e2, _ = decide(conn, item(
            "At least 7 killed in India after hydropower project "
            "tunnel collapses",
            "A tunnel under construction at a hydropower project in "
            "India's northern hill state of Uttarakhand collapsed "
            "overnight, leaving at least seven workers dead, "
            "officials said on Friday. A rescue operation was "
            "launched after a sudden rush of water and debris "
            "entered the tunnel at the Vishnugad-Pipalkoti "
            "hydroelectric project in Chamoli district later on "
            "Thursday night.",
            source="South China Morning Post", tier=2,
        ))
        assert s2 in ("UPDATE", "DUPLICATE")
        assert e2 == e1
        assert event_count(conn) == 1
        conn.close()

    def test_enrichment_failure_leaves_item_unchanged(self):
        from src.article_extractor import (
            enrich_thin_story_before_event_memory,
        )
        thin = item(
            "Reports of incident in northern India",
            "Officials reported an incident in northern India.",
            source="DW", discovery=True,
        )
        def mock_fetcher(url, cfg, allowlist, robots=None, pace=None):
            raise RuntimeError("network down")

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        kept, stats = enrich_thin_story_before_event_memory(
            thin, CONFIG_DICT, now, fetcher=mock_fetcher, max_fetches=5,
        )
        assert kept is thin
        assert kept.get("summary") == thin["summary"]
        assert stats["expanded"] == 0

    def test_thin_discovery_held_strong_same_event_one_event(self):
        # The confirmed Uttarakhand case: a thin discovery item
        # ("Seven killed in tunnel accident in India, officials
        # say") must NOT create a second event next to the
        # fact-dense one.
        conn = make_db()
        # Fact-dense discovery seed (bound fact "7 dead" + the
        # distinctive place "Uttarakhand") anchors the event -
        # the real DW seed from the confirmed case.
        s1, e1, _ = decide(conn, item(
            "India news: 7 dead, 3 trapped in Uttarakhand "
            "tunnel collapse",
            "Seven people died and three were trapped after a "
            "tunnel collapsed in northern India.",
            source="DW", discovery=True,
        ))
        assert s1 == "NEW"
        # Thin discovery report of the SAME event: held, no event.
        s2, e2, _ = decide(conn, item(
            "Seven killed in tunnel accident in India, officials say",
            "Seven people were killed in a tunnel accident in "
            "India, officials said.",
            source="Al Jazeera", discovery=True,
        ))
        assert s2 == "HELD"
        assert e2 is None
        # A strong report of the same event anchors the one event.
        s3, e3, _ = decide(conn, item(
            "India tunnel collapse rescue operation under way",
            "Rescuers were working through the night to free "
            "three workers trapped in the tunnel collapse.",
            source="SCMP", tier=2,
        ))
        assert s3 in ("NEW", "UPDATE")
        assert e3 == e1
        assert event_count(conn) == 1
        conn.close()

    def test_thin_discovery_plus_unrelated_strong_stays_separate(self):
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "Officials issue statement on regional situation",
            "Officials issued a statement about the situation "
            "in the region.",
            source="DW", discovery=True,
        ))
        assert s1 == "HELD"
        s2, e2, _ = decide(conn, item(
            "Colombia earthquake kills 281, 379 missing",
            "A magnitude 7.4 earthquake killed 281 people in "
            "Colombia with 379 still missing.",
            source="Reuters", tier=1,
        ))
        assert s2 == "NEW"
        assert event_count(conn) == 1
        assert event_ids(conn) == [e2]
        conn.close()

    def test_two_unrelated_tunnel_accidents_stay_separate(self):
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "Tunnel collapse in Uttarakhand kills 7",
            "Seven workers died when a tunnel collapsed in "
            "Uttarakhand, India.",
            source="Reuters", tier=1,
        ))
        s2, e2, _ = decide(conn, item(
            "Tunnel collapse in Turkey kills 3",
            "Three people were killed when a tunnel collapsed "
            "in eastern Turkey.",
            source="AFP", tier=1,
        ))
        assert s1 == "NEW" and s2 == "NEW"
        assert e1 != e2
        assert event_count(conn) == 2
        conn.close()

    def test_thin_discovery_plus_enriched_article_canonical_improves(self):
        conn = make_db()
        s1, _, _ = decide(conn, item(
            "Reports of incident in northern India",
            "Officials reported an incident in northern India.",
            source="DW", discovery=True,
        ))
        assert s1 == "HELD"
        # The strong report anchors the event with real content.
        s2, e2, _ = decide(conn, item(
            "India tunnel collapse kills 7, 3 still trapped",
            "Seven people were confirmed dead and three remained "
            "trapped after a tunnel collapse in Uttarakhand, "
            "with rescuers working through the night.",
            source="Reuters", tier=1, strong=2,
        ))
        assert s2 == "NEW"
        row = event_row(conn, e2)
        assert "tunnel" in (row[0] or "").lower()
        best = state_of(conn, e2)["best_story"]
        assert "tunnel" in (best["title"] or "").lower()
        assert best["strength"] >= 10
        conn.close()

    def test_repeated_thin_discovery_never_creates_events(self):
        conn = make_db()
        for _ in range(3):
            status, eid, _ = decide(conn, item(
                "Seven killed in tunnel accident in India, officials say",
                "Seven people were killed in a tunnel accident "
                "in India, officials said.",
                source="Al Jazeera", discovery=True,
            ))
            assert status == "HELD"
            assert eid is None
        assert event_count(conn) == 0
        conn.close()

    def test_thin_non_discovery_still_creates_event(self):
        # A thin NON-discovery report may be the only source of a
        # genuine new event: pre-existing behavior is preserved.
        conn = make_db()
        status, eid, _ = decide(conn, item(
            "Global markets tumble on trade war fears",
            "Stock markets fell sharply as traders worried "
            "about tariffs.",
            source="Reuters",
        ))
        assert status == "NEW"
        assert eid is not None
        conn.close()

    def test_fact_dense_discovery_still_creates_event(self):
        conn = make_db()
        status, eid, _ = decide(conn, item(
            "India news: 7 dead, 3 trapped in Uttarakhand "
            "tunnel collapse",
            "Seven people died and three were trapped after a "
            "tunnel collapsed in northern India.",
            source="DW", discovery=True,
        ))
        assert status == "NEW"
        assert eid is not None
        conn.close()

    def test_word_number_casualty_matches_digit_seed(self):
        # The confirmed Uttarakhand split in its full form: the
        # DW seed spells the count with a digit ("7 dead") while
        # the Al Jazeera report spells it out ("seven workers
        # have been killed").  Word-numbers in casualty contexts
        # must produce the same bound fact, so the enriched AJ
        # report merges into the DW-seeded event via impact+type
        # instead of creating a second event.
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "India news: 7 dead, 3 trapped in Uttarakhand "
            "tunnel collapse",
            "Seven people died and three were trapped after a "
            "tunnel collapsed in northern India.",
            source="DW", discovery=True,
        ))
        assert s1 == "NEW"
        # The enriched AJ report: full article sentences with
        # spelled-out casualty counts.
        s2, e2, _ = decide(conn, item(
            "Seven killed in tunnel accident at Indian hydropower "
            "project",
            "At least seven workers have been killed and 13 "
            "others injured after a tunnel accident at a "
            "construction site in Uttarakhand, India, according "
            "to local officials.",
            source="Al Jazeera", tier=2,
        ))
        assert s2 in ("UPDATE", "DUPLICATE")
        assert e2 == e1
        assert event_count(conn) == 1
        conn.close()

    def test_word_number_outside_casualty_context_untouched(self):
        # "Seven" in a non-casualty phrase must not fabricate an
        # impact fact that could falsely merge stories.
        from src.event_memory import _impact_pairs
        assert _impact_pairs(
            "The seven-day festival drew crowds of seven hundred."
        ) == set()
        assert "7:people" in _impact_pairs(
            "Seven people were killed in the blast."
        )
        assert "5:magnitude" not in _impact_pairs(
            "Five minutes later the building collapsed."
        )


# ---------------------------------------------------------------------------
# C2 - last_development is strictly monotonic
# ---------------------------------------------------------------------------


class TestMonotonicLastDevelopment:
    def _seed(self, conn, at):
        s, eid, _ = decide(conn, item(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake killed 100 people in Colombia.",
            effective_at=at,
        ))
        return eid

    def test_older_update_does_not_rewind(self):
        conn = make_db()
        eid = self._seed(conn, "2026-08-14T10:30:00")
        assert event_row(conn, eid)[3] == "2026-08-14T10:30:00"
        # An UPDATE with an OLDER effective time must not rewind.
        s2, _, _ = decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "Rescuers said the death toll from the Colombia "
            "earthquake had climbed past 180.",
            source="Reuters", tier=1,
            effective_at="2026-08-14T01:05:00",
        ))
        assert s2 == "UPDATE"
        assert event_row(conn, eid)[3] == "2026-08-14T10:30:00"
        conn.close()

    def test_newer_update_advances(self):
        conn = make_db()
        eid = self._seed(conn, "2026-08-14T10:30:00")
        s2, _, _ = decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "Rescuers said the death toll had climbed past 180.",
            source="Reuters", tier=1,
            effective_at="2026-08-14T11:00:00",
        ))
        assert s2 == "UPDATE"
        assert event_row(conn, eid)[3] == "2026-08-14T11:00:00"
        conn.close()

    def test_null_then_newer_update_sets_value(self):
        conn = make_db()
        eid = self._seed(conn, None)
        # Simulate a legacy row with a NULL last_development.
        conn.execute(
            "UPDATE events SET last_development=NULL "
            "WHERE event_id=?",
            (eid,),
        )
        conn.commit()
        s2, _, _ = decide(conn, item(
            "Colombia earthquake death toll rises to 180",
            "Rescuers said the death toll had climbed past 180.",
            source="Reuters", tier=1,
            effective_at="2026-08-14T11:00:00",
        ))
        assert s2 == "UPDATE"
        assert event_row(conn, eid)[3] == "2026-08-14T11:00:00"
        conn.close()

    def test_null_update_never_destroys_existing_value(self):
        conn = make_db()
        eid = self._seed(conn, "2026-08-14T11:00:00")
        # A duplicate (advance_development=False -> NULL candidate)
        # must never clear the stored value.
        s2, _, _ = decide(conn, item(
            "Powerful quake leaves 100 dead in Colombia",
            "A strong quake left 100 dead in the region.",
            source="Reuters",
        ))
        assert s2 == "DUPLICATE"
        assert event_row(conn, eid)[3] == "2026-08-14T11:00:00"
        conn.close()


# ---------------------------------------------------------------------------
# M1 - live-blog headline/body coherence requires substantive overlap
# ---------------------------------------------------------------------------


def _summarize_rows(texts, headline, source=None):
    rows = [
        {"text": t, "source": "X", "item_id": "i%d" % i}
        for i, t in enumerate(texts)
    ]
    return summarize_rows(
        rows,
        source or " ".join(texts),
        headline,
        cfg={"min_sentences": 2, "max_sentences": 4},
    )


class TestLiveBlogCoherence:
    def test_headline_fire_body_heatwave_rejected(self):
        kept, stats = _summarize_rows(
            [
                "London recorded its hottest day on record as "
                "the heatwave continued.",
                "Temperatures hit 34 degrees Celsius in the "
                "capital.",
            ],
            "Nineteen people taken to hospital after West "
            "Midlands fire",
        )
        assert kept is None
        assert stats["rejected"] == "coherence"

    def test_headline_fire_body_fire_accepted(self):
        kept, stats = _summarize_rows(
            [
                "Fire crews battled the blaze in the West "
                "Midlands overnight.",
                "Nineteen people were taken to hospital with "
                "burns.",
            ],
            "Nineteen people taken to hospital after West "
            "Midlands fire",
        )
        assert kept is not None
        assert stats["rejected"] is None

    def test_liveblog_update_marker_body_rejected(self):
        # The Phase F finding: the extracted body was only an
        # update pointer ("We just got an update from the West
        # Midlands ambulance service.") - no facts at all.
        kept, stats = _summarize_rows(
            [
                "We just got an update from the West Midlands "
                "ambulance service.",
                "We bring you the latest as it comes in.",
            ],
            "Nineteen people taken to hospital after West "
            "Midlands fire - Europe live",
        )
        assert kept is None

    def test_multiple_liveblog_items_unrelated_body_rejected(self):
        kept, stats = _summarize_rows(
            [
                "Brent crude fell below 70 dollars a barrel on "
                "Tuesday.",
                "The yen weakened to a 38-year low against the "
                "dollar.",
            ],
            "Markets live: bond yields surge as inflation "
            "fears grow",
        )
        assert kept is None
        assert stats["rejected"] == "coherence"


# ---------------------------------------------------------------------------
# M2 - political "landslide" must not create disaster identity
# ---------------------------------------------------------------------------


class TestLandslideDisambiguation:
    def test_landslide_victory_never_merges_with_tunnel_collapse(self):
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "India tunnel collapse kills 7 after landslide",
            "Seven people died when a landslide brought down a "
            "tunnel under construction in India.",
            source="Reuters", tier=1,
        ))
        s2, e2, _ = decide(conn, item(
            "Hichilema wins Zambia election by landslide",
            "Hakainde Hichilema defeated his rival by a "
            "landslide in the 2021 election.",
            source="BBC", tier=2,
        ))
        assert s1 == "NEW" and s2 == "NEW"
        assert e1 != e2
        # The political landslide never entered the identity.
        assert "landslide" not in (
            state_of(conn, e2)["identity"]["core_words"]
        )
        conn.close()

    def test_landslide_victory_never_merges_with_earthquake(self):
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "Earthquake kills 5 in Chile",
            "A magnitude 6.2 earthquake killed five people in "
            "Chile.",
            source="Reuters", tier=1,
        ))
        s2, e2, _ = decide(conn, item(
            "Ruling party wins landslide victory in election",
            "The ruling party won a landslide victory in the "
            "national election.",
            source="AFP", tier=1,
        ))
        assert e1 != e2
        assert "landslide" not in (
            state_of(conn, e2)["identity"]["core_words"]
        )
        conn.close()

    def test_terrain_landslide_still_merges(self):
        conn = make_db()
        s1, e1, _ = decide(conn, item(
            "Landslide blocks mountain road in Himachal",
            "A landslide blocked a mountain road in Himachal "
            "Pradesh.",
            source="Reuters", tier=1,
        ))
        s2, e2, _ = decide(conn, item(
            "Landslide kills 3 in Himachal Pradesh",
            "Three people were killed by a landslide in "
            "Himachal Pradesh.",
            source="AFP", tier=1,
        ))
        assert e1 == e2
        assert "landslide" in (
            state_of(conn, e1)["identity"]["core_words"]
        )
        conn.close()


# ---------------------------------------------------------------------------
# M3 - opinion / analysis headline formats
# ---------------------------------------------------------------------------


class TestOpinionAnalysisRejection:
    def test_cannot_save_analysis_rejected(self):
        assert editorial_eligibility({
            "title": "Just like with the yen, America cannot "
                     "save the AI bubble",
            "summary": "The article argues the bubble will burst.",
        }) is False

    def test_why_is_analysis_rejected(self):
        assert editorial_eligibility({
            "title": "Why the yen is falling against the dollar",
            "summary": "The article explains the yen's slide.",
        }) is False

    def test_can_x_save_question_rejected(self):
        assert editorial_eligibility({
            "title": "Can AI save the National Health Service?",
            "summary": "The piece weighs AI's potential.",
        }) is False

    def test_reporting_with_why_mid_sentence_kept(self):
        # "why" mid-sentence in a reporting headline is news.
        assert editorial_eligibility({
            "title": "Officials explain why the bridge collapsed "
                     "during the storm",
            "summary": "The bridge gave way on Monday evening. "
                       "Officials said corrosion was the cause.",
        }) is True


# ---------------------------------------------------------------------------
# M4 - anniversary / retrospective framing
# ---------------------------------------------------------------------------


class TestAnniversaryRejection:
    def test_a_year_after_without_development_rejected(self):
        assert editorial_eligibility({
            "title": "A year after student protests that shook "
                     "Valjevo: Serbia at a crossroads",
            "summary": "The piece looks back at the protests and "
                       "their aftermath.",
        }) is False

    def test_marks_anniversary_rejected(self):
        assert editorial_eligibility({
            "title": "Serbia marks the anniversary of the "
                     "student protests",
            "summary": "Crowds gathered to commemorate the "
                       "protests.",
        }) is False

    def test_anniversary_with_current_development_kept(self):
        # A genuine current development attached to an
        # anniversary reference stays eligible.
        assert editorial_eligibility({
            "title": "One year after disaster, government "
                     "releases new investigation findings",
            "summary": "The report identifies failures in the "
                       "response.",
        }) is True

    def test_years_after_with_status_fact_kept(self):
        assert editorial_eligibility({
            "title": "Five years after the quake, 5,000 people "
                     "still displaced",
            "summary": "Officials said reconstruction remains "
                       "incomplete.",
        }) is True


# ---------------------------------------------------------------------------
# L1 - dateline and truncation artifacts
# ---------------------------------------------------------------------------


class TestDatelineAndTruncation:
    def test_all_caps_dateline_stripped(self):
        assert clean_sentence_text(
            "NEW DELHI \u2013 India's finance minister said "
            "growth was strong."
        ) == (
            "India's finance minister said growth was strong."
        )

    def test_wire_dateline_stripped(self):
        assert clean_sentence_text(
            "SEOUL, Aug. 14 (Yonhap) -- The government "
            "announced new measures."
        ) == (
            "The government announced new measures."
        )

    def test_agency_dateline_stripped(self):
        assert clean_sentence_text(
            "NEW YORK (AP) - Wall Street rallied at the close."
        ) == (
            "Wall Street rallied at the close."
        )

    def test_ordinary_sentence_never_stripped(self):
        assert clean_sentence_text(
            "Paris city council votes on the housing plan "
            "next week."
        ) == (
            "Paris city council votes on the housing plan "
            "next week."
        )

    def test_truncated_ellipsis_sentence_rejected(self):
        kept, stats = _summarize_rows(
            [
                "The airline avoided any further time behind "
                "schedule...",
                "More than 12 flights were cancelled and "
                "passengers were rebooked.",
                "The airline said 500 affected passengers will "
                "receive compensation.",
            ],
            "Airline delays: passengers rebooked after "
            "maintenance issues",
        )
        assert kept is not None
        # The truncated sentence must not appear in the output.
        joined = " ".join(r["text"] for r in kept)
        assert "behind" not in joined
        assert "..." not in joined
        assert len(kept) >= 2
