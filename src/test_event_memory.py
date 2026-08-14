"""Regression tests for cross-run event memory.

Each test drives `decide()` exactly the way the pipeline does:
a first story creates an event (NEW), a second story about the
same event is either suppressed (DUPLICATE) or published as a
development (UPDATE), and unrelated stories stay separate (NEW
with a different event_id).

Run with:  .venv/bin/python -m pytest src/test_event_memory.py -q
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from src.event_memory import (
    decide,
    init_events,
    mark_queued,
    purge_expired,
)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stories(
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            source TEXT,
            category TEXT,
            summary TEXT,
            score INTEGER,
            confidence TEXT,
            event_id TEXT,
            event_status TEXT,
            first_seen TEXT
        )
        """
    )
    init_events(conn)
    return conn


def item(title, summary, source="BBC World", score=70):
    return {
        "id": source + "|" + title,
        "title": title,
        "summary": summary,
        "url": "https://example.com/" + source + "/" + title,
        "source": source,
        "source_category": "world",
        "primary_source": False,
        "tier": 2,
        "category": "world",
        "score": score,
        "confidence": "medium",
    }


QUAKE_SUMMARY = (
    "A powerful earthquake struck a coastal region at dawn, "
    "killing 100 people and damaging buildings."
)


# ---------------------------------------------------------------------------
# A-F core regression cases
# ---------------------------------------------------------------------------


class TestEarthquakeDedup:
    def test_a_reworded_same_facts_is_one_post(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Powerful quake leaves 100 dead",
                "Officials said the quake killed 100 people and "
                "damaged buildings.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2
        conn.close()

    def test_b_death_toll_update_is_two_posts_update(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Earthquake death toll rises to 180",
                "The death toll from the earthquake rose to 180, "
                "officials said.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "UPDATE"
        assert eid1 == eid2
        conn.close()

    def test_c_national_emergency_is_an_update(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Government declares national emergency",
                "The government declared a national emergency "
                "after the earthquake.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "UPDATE"
        assert eid1 == eid2
        conn.close()

    def test_d_reworded_confirmation_is_one_post(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Coastal city quake: 100 confirmed dead",
                "The quake in the coastal city left 100 confirmed "
                "dead, officials said.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2
        conn.close()

    def test_e_soft_reaction_is_suppressed(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Official calls earthquake a tragedy",
                "An official called the earthquake a tragedy.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2
        conn.close()

    def test_f_unrelated_colombia_stories_stay_separate(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item(
                "Colombia earthquake kills 100",
                "A powerful earthquake struck Colombia, killing "
                "100 people.",
            ),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Colombia president announces tax reform",
                "The president announced a tax reform plan in "
                "Colombia.",
                source="Reuters",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "NEW"
        assert eid1 != eid2
        conn.close()


# ---------------------------------------------------------------------------
# Cross-run persistence (the system runs every ~15 minutes)
# ---------------------------------------------------------------------------


class TestCrossRunPersistence:
    def test_state_survives_separate_connections(self, tmp_path):
        db_path = str(tmp_path / "news.db")

        conn1 = sqlite3.connect(db_path)
        conn1.execute(
            """
            CREATE TABLE IF NOT EXISTS stories(
                id TEXT PRIMARY KEY, title TEXT, url TEXT,
                source TEXT, category TEXT, summary TEXT,
                score INTEGER, confidence TEXT, event_id TEXT,
                event_status TEXT, first_seen TEXT
            )
            """
        )
        init_events(conn1)
        status1, eid1, _ = decide(
            conn1,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        conn1.commit()
        conn1.close()

        conn2 = sqlite3.connect(db_path)
        conn2.execute(
            """
            CREATE TABLE IF NOT EXISTS stories(
                id TEXT PRIMARY KEY, title TEXT, url TEXT,
                source TEXT, category TEXT, summary TEXT,
                score INTEGER, confidence TEXT, event_id TEXT,
                event_status TEXT, first_seen TEXT
            )
            """
        )
        init_events(conn2)
        status2, eid2, _ = decide(
            conn2,
            item(
                "Powerful quake leaves 100 dead in coastal region",
                "The quake killed 100 people, officials said.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2

        row = conn2.execute(
            "SELECT canonical_state FROM events WHERE event_id=?",
            (eid1,),
        ).fetchone()
        state = json.loads(row[0])
        # The reworded duplicate's title joined the event memory
        # so a future reworded headline is still recognized.
        assert "Powerful quake leaves 100 dead" in state["titles"][0]
        assert "100" in state["numbers"]
        conn2.close()

    def test_canonical_state_accumulates_across_updates(self):
        conn = make_db()
        _, eid, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        _, _, _ = decide(
            conn,
            item(
                "Earthquake death toll rises to 180",
                "The death toll rose to 180, officials said.",
                source="Reuters",
            ),
        )
        row = conn.execute(
            "SELECT canonical_state FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
        state = json.loads(row[0])
        assert "100" in state["numbers"]
        assert "180" in state["numbers"]
        assert len(state["titles"]) >= 2
        assert "killed" in state["consequences"]
        conn.close()


# ---------------------------------------------------------------------------
# Real-audit regression cases
# ---------------------------------------------------------------------------


class TestRealRegressionCases:
    def _pair(self, t1, s1, t2, s2, source2="Reuters"):
        conn = make_db()
        status1, eid1, _ = decide(conn, item(t1, s1))
        status2, eid2, _ = decide(conn, item(t2, s2, source=source2))
        conn.close()
        return status1, eid1, status2, eid2

    def test_twitch_amazon_ai_training_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Twitch streams quietly used to train Amazon AI model",
            "Twitch livestreams were used to train an Amazon AI "
            "model without creators' consent, a report found.",
            "Amazon AI trained on Twitch streams without consent, "
            "report says",
            "The company used thousands of hours of Twitch video "
            "to train its AI model.",
        )
        assert st1 == "NEW"
        assert st2 == "DUPLICATE"
        assert e1 == e2

    def test_colombia_earthquake_toll_update(self):
        st1, e1, st2, e2 = self._pair(
            "Earthquake kills 100 in Colombia",
            "A powerful earthquake struck western Colombia, "
            "killing 100 people.",
            "Colombia quake: death toll climbs past 100",
            "Rescuers said the death toll from the Colombia "
            "earthquake had climbed past 100.",
        )
        assert st1 == "NEW"
        assert st2 == "UPDATE"
        assert e1 == e2

    def test_qusra_west_bank_raid_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Israeli forces raid Qusra in occupied West Bank",
            "Troops entered the village of Qusra overnight and "
            "detained residents.",
            "West Bank: Israeli troops arrest suspects in Qusra raid",
            "The military said arrests were made during the raid "
            "on Qusra.",
        )
        assert st1 == "NEW"
        assert st2 == "UPDATE"
        assert e1 == e2

    def test_spain_eclipse_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Total solar eclipse sweeps across Spain",
            "The eclipse crossed northern Spain this afternoon, "
            "drawing huge crowds.",
            "Spain: skywatchers watch rare total solar eclipse",
            "Thousands gathered to watch the total solar eclipse "
            "in Spain.",
        )
        assert st1 == "NEW"
        assert st2 == "DUPLICATE"
        assert e1 == e2

    def test_malawi_cameroon_wafcon_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Malawi beat Cameroon to reach WAFCON quarter-finals",
            "Malawi won 2-1 to qualify for the quarter-finals.",
            "Cameroon out of WAFCON after Malawi defeat",
            "Cameroon were eliminated from WAFCON after losing "
            "to Malawi.",
        )
        assert st1 == "NEW"
        assert st2 == "DUPLICATE"
        assert e1 == e2

    def test_french_ambassador_dispute_merges(self):
        st1, e1, st2, e2 = self._pair(
            "France recalls ambassador from Niger",
            "Paris said the ambassador would return after the "
            "dispute escalated.",
            "Niger expels French ambassador amid row",
            "The government ordered the French ambassador to leave.",
        )
        assert st1 == "NEW"
        assert st2 == "DUPLICATE"
        assert e1 == e2

    def test_iran_strait_of_hormuz_tanker_seizure_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Seven oil tankers seized near Strait of Hormuz",
            "Iranian forces boarded seven tankers in the strait.",
            "Iran seizes 7 oil tankers in Hormuz Strait",
            "Commandos took control of seven vessels near Hormuz.",
        )
        assert st1 == "NEW"
        assert st2 == "DUPLICATE"
        assert e1 == e2

    def test_different_countries_never_merge(self):
        st1, e1, st2, e2 = self._pair(
            "Earthquake hits Japan",
            "A magnitude 6.4 quake struck Japan.",
            "Earthquake hits Canada",
            "A magnitude 5.9 quake struck Canada.",
        )
        assert st1 == "NEW"
        assert st2 == "NEW"
        assert e1 != e2

    def test_south_korea_and_north_korea_stories_never_merge(self):
        # "South Korea" and "North Korea" share the bare word
        # "korea" but are different places; unrelated stories in
        # each stay separate.
        st1, e1, st2, e2 = self._pair(
            "'I lost $14,000 in a month': Investors hit by "
            "Korean stock market's wild swings",
            "South Korea's stock market has swung wildly, "
            "leaving retail investors nursing heavy losses.",
            "North Korea slams US-South Korea military drills "
            "and threatens strong response",
            "North Korea condemned the joint US-South Korea "
            "military drills.",
        )
        assert st1 == "NEW"
        assert st2 == "NEW"
        assert e1 != e2

    def test_different_named_storms_never_merge(self):
        # Tropical Storm Lala and Tropical Storm Hernan are
        # different events even though every topic word overlaps.
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item(
                "Hurricane warning is issued for the Big Island "
                "as Tropical Storm Lala approaches",
                "The storm is expected to approach Hawaii.",
                source="Google News Pacific Discovery",
            ),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Tropical Storm Lala forms in the Pacific and "
                "hurricane watch is issued",
                "Tropical Storm Lala formed in the Pacific.",
                source="Google News Pacific Discovery",
            ),
        )
        status3, eid3, _ = decide(
            conn,
            item(
                "Tropical Storm Hernan forms in the Pacific while "
                "a different weather system could soak Hawaii",
                "Tropical Storm Hernan formed in the Pacific.",
                source="Google News Pacific Discovery",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert status3 == "NEW"
        assert eid1 == eid2
        assert eid1 != eid3
        conn.close()

    def test_same_named_storm_merges(self):
        st1, e1, st2, e2 = self._pair(
            "Typhoon Yagi hits the Philippines",
            "The storm slammed into the northern coast.",
            "Super Typhoon Yagi makes landfall in the "
            "Philippines, 6 dead",
            "At least six people were killed as Yagi came ashore.",
        )
        assert st1 == "NEW"
        assert st2 == "UPDATE"
        assert e1 == e2


# ---------------------------------------------------------------------------
# Existing helpers still work
# ---------------------------------------------------------------------------


class TestEventMemoryHelpers:
    def test_mark_queued_increments_count(self):
        conn = make_db()
        _, eid, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        mark_queued(conn, eid)
        count = conn.execute(
            "SELECT queued_count FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()[0]
        assert count == 1
        conn.close()

    def test_purge_expired_is_idempotent(self):
        conn = make_db()
        decide(conn, item("Earthquake kills 100 people", QUAKE_SUMMARY))
        result = purge_expired(conn, story_memory_hours=48)
        assert set(result) == {
            "stories_expired",
            "normal_events_expired",
            "major_events_expired",
        }
        assert result["stories_expired"] == 0
        conn.close()

    def test_same_source_never_reposted(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 people", QUAKE_SUMMARY),
        )
        # Same source reporting the same event again, even with a
        # slightly different phrasing, is always a duplicate.
        status2, eid2, _ = decide(
            conn,
            item(
                "Earthquake kills 100 people in coastal region",
                "The earthquake killed 100 people.",
                source="BBC World",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2
        conn.close()


# ---------------------------------------------------------------------------
# Adversarial separation: shared attribute must never merge unrelated events
# ---------------------------------------------------------------------------

class TestAdversarialSeparation:
    """Unrelated events that merely share a person, place, topic,
    company, number or entity must stay separate."""

    def _assert_separate(self, pairs, label):
        conn = make_db()
        statuses = []
        for i, (title, summary) in enumerate(pairs):
            status, eid, _ = decide(conn, item(title, summary))
            statuses.append(status)
            conn.execute(
                "UPDATE events SET last_seen=? WHERE event_id=?",
                (
                    (datetime.now(timezone.utc)
                     + timedelta(minutes=i * 10)).isoformat(),
                    eid,
                ),
            )
        nevents = conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        conn.close()
        assert nevents == len(pairs), label
        assert all(s == "NEW" for s in statuses), (
            label, statuses
        )

    def test_same_person_different_event(self):
        self._assert_separate(
            [
                ("Elon Musk announces new Tesla factory in Texas",
                 "Musk said the plant would open next year."),
                ("Elon Musk testifies in SEC lawsuit over tweets",
                 "The hearing took place in federal court."),
            ],
            "person",
        )

    def test_same_city_different_event(self):
        self._assert_separate(
            [
                ("Massive fire breaks out in downtown Chicago",
                 "Flames engulfed a warehouse district."),
                ("Chicago transit strike halts train service",
                 "Union workers walked off the job."),
            ],
            "city",
        )

    def test_same_country_different_event(self):
        self._assert_separate(
            [
                ("Brazil floods leave 40 dead in the south",
                 "Rivers burst their banks after heavy rain."),
                ("Brazil launches new space agency program",
                 "Officials announced the initiative in Brasilia."),
            ],
            "country",
        )

    def test_same_company_different_event(self):
        self._assert_separate(
            [
                ("Apple unveils new iPhone model",
                 "The device features a faster chip."),
                ("Apple fined by EU regulators over app store rules",
                 "The penalty follows a long antitrust probe."),
            ],
            "company",
        )

    def test_same_topic_different_event(self):
        self._assert_separate(
            [
                ("Wildfires burn across Greece",
                 "Thousands were evacuated from islands."),
                ("Wildfire study warns of rising risk in Australia",
                 "Scientists published new climate research."),
            ],
            "topic",
        )

    def test_same_location_different_event(self):
        self._assert_separate(
            [
                ("Earthquake strikes near Tokyo",
                 "A magnitude 6.2 quake shook buildings."),
                ("Tokyo hosts international robotics expo",
                 "Companies showed new machines."),
            ],
            "location",
        )

    def test_same_numbers_different_event(self):
        self._assert_separate(
            [
                ("Train crash kills 12 in Spain",
                 "Twelve people died in the derailment."),
                ("12 artists named to national film awards",
                 "The shortlist was announced today."),
            ],
            "numbers",
        )

    def test_same_entities_different_event(self):
        self._assert_separate(
            [
                ("US, China resume trade talks",
                 "Delegations met in Geneva."),
                ("US, China hold rare military exercises",
                 "Warships conducted drills together."),
            ],
            "entities",
        )

    def test_different_storms_similar_names(self):
        self._assert_separate(
            [
                ("Tropical Storm Hernan strengthens in the Pacific",
                 "Winds reached 60 mph near Hawaii."),
                ("Tropical Storm Hermine weakens off the coast of Mexico",
                 "The system faded into a depression."),
            ],
            "storms",
        )

    def test_different_elections_same_country(self):
        self._assert_separate(
            [
                ("Voters head to polls in French local elections",
                 "Turnout was high across regions."),
                ("France holds presidential election runoff",
                 "Two candidates face a final vote."),
            ],
            "elections",
        )

    def test_different_attacks_same_city(self):
        self._assert_separate(
            [
                ("Bomb blast kills 5 in Kabul market",
                 "Explosives detonated at midday."),
                ("Gunmen attack Kabul military hospital",
                 "Security forces responded to the siege."),
            ],
            "attacks",
        )

    def test_different_earthquakes_same_region(self):
        # Different magnitudes = different quakes, even in the
        # same country.
        self._assert_separate(
            [
                ("Earthquake strikes off the coast of Chile",
                 "A 6.8 tremor was recorded offshore."),
                ("Chile quake damages buildings in Santiago",
                 "The 5.9 temblor shook the capital."),
            ],
            "quakes",
        )

    def test_different_court_cases_same_person(self):
        self._assert_separate(
            [
                ("Court hears fraud charges against ex-banker",
                 "Prosecutors allege a decade of schemes."),
                ("Ex-banker faces new trial over tax evasion",
                 "Jury selection begins next week."),
            ],
            "court",
        )


# ---------------------------------------------------------------------------
# UPDATE cadence: updates only for material developments
# ---------------------------------------------------------------------------

class TestUpdateCadence:
    """One major event must not spam UPDATEs.  Reaction/statement
    chains stay suppressed; only material developments (new toll,
    emergency, rescue) publish an UPDATE."""

    QUAKE = (
        "A powerful earthquake struck a coastal region at dawn, "
        "killing 100 people and damaging buildings."
    )

    def _chain(self, stories, label):
        conn = make_db()
        statuses = []
        now = datetime.now(timezone.utc)
        for i, story in enumerate(stories):
            title, summary = story
            status, eid, _ = decide(conn, item(title, summary))
            statuses.append(status)
            conn.execute(
                "UPDATE events SET last_seen=? WHERE event_id=?",
                (
                    (now + timedelta(minutes=i * 10)).isoformat(),
                    eid,
                ),
            )
        nevents = conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        conn.close()
        return statuses, nevents

    def test_reaction_chain_no_update_spam(self):
        statuses, nevents = self._chain(
            [
                ("Earthquake kills 100 in Colombia", self.QUAKE),
                ("Official calls earthquake a tragedy",
                 "The minister said the nation mourned the quake victims."),
                ("Minister says quake is a tragedy for the country",
                 "The interior minister called the quake a painful loss."),
                ("Police comment on quake response",
                 "Police said crews were working around the clock after the quake."),
                ("President expresses condolences after earthquake",
                 "The president sent his sympathies to the quake families."),
            ],
            "reaction",
        )
        assert nevents == 1
        assert statuses == [
            "NEW", "DUPLICATE", "DUPLICATE", "DUPLICATE", "DUPLICATE",
        ]

    def test_material_development_chain_updates(self):
        statuses, nevents = self._chain(
            [
                ("Earthquake kills 100 in Colombia", self.QUAKE),
                ("Colombia earthquake death toll rises to 180",
                 "Officials raised the confirmed toll to 180 on Friday."),
                ("Government declares national emergency after earthquake",
                 "A state of emergency was declared across quake-hit regions."),
                ("Rescuers pull 12 survivors from earthquake rubble in Colombia",
                 "Crews freed a family trapped since the quake struck."),
            ],
            "dev",
        )
        assert nevents == 1
        assert statuses == ["NEW", "UPDATE", "UPDATE", "UPDATE"]

    def test_same_source_resend_duplicate(self):
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 in Colombia", self.QUAKE),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Powerful quake leaves 100 dead in Colombia",
                self.QUAKE,
            ),
        )
        assert status1 == "NEW"
        assert status2 == "DUPLICATE"
        assert eid1 == eid2
        conn.close()

    def test_repeat_of_same_development_stays_duplicate(self):
        # "death toll rises to 180" is a material UPDATE; a
        # second story repeating the SAME figure must not be
        # published again as another UPDATE.
        conn = make_db()
        status1, eid1, _ = decide(
            conn,
            item("Earthquake kills 100 in Colombia", self.QUAKE),
        )
        status2, eid2, _ = decide(
            conn,
            item(
                "Colombia earthquake death toll rises to 180",
                "Officials raised the confirmed toll to 180.",
                source="Reuters",
            ),
        )
        status3, eid3, _ = decide(
            conn,
            item(
                "Death toll from Colombia quake now 180, officials confirm",
                "The toll was confirmed at 180 on Friday evening.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "UPDATE"
        assert status3 == "DUPLICATE"
        assert eid1 == eid2 == eid3
        conn.close()


# ---------------------------------------------------------------------------
# Chain-merge isolation: identity is anchored and never broadens
# ---------------------------------------------------------------------------

class TestChainMergeIsolation:
    """The structural guarantee: an event's matching surface is its
    immutable first-story identity.  A weakly-related story B that
    merges (or must not merge) with A can never widen A's identity
    so that a third story C - which shares vocabulary only with B -
    attaches to A."""

    def test_chain_generic_vocabulary_never_links(self):
        conn = make_db()
        # A: the anchored event
        status1, e1, _ = decide(
            conn,
            item(
                "US tariffs on 40 nations take effect",
                "New duties on imports from 40 countries took effect "
                "today.",
            ),
        )
        # B: unrelated, shares loose trade/market vocabulary with A
        status2, e2, _ = decide(
            conn,
            item(
                "Global markets tumble on trade war fears",
                "Stock markets fell sharply as traders worried about "
                "tariffs.",
                source="Reuters",
            ),
        )
        # C: shares words with B and the word "tariff" with A
        status3, e3, _ = decide(
            conn,
            item(
                "Wall Street rout deepens as tariff fears spread",
                "The selloff accelerated amid fresh tariff worries.",
                source="Bloomberg",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "NEW"
        assert status3 == "NEW"
        assert len({e1, e2, e3}) == 3
        conn.close()

    def test_chain_year_and_weekday_never_link(self):
        # Real audit failures: "2026" and "Wednesday" acted as
        # identity links between unrelated stories.
        conn = make_db()
        status1, e1, _ = decide(
            conn,
            item(
                "Total solar eclipse 2026 crosses Europe",
                "The eclipse will be visible across much of Europe.",
            ),
        )
        status2, e2, _ = decide(
            conn,
            item(
                "UK records hottest day of 2026",
                "Temperatures hit a record high this afternoon.",
                source="Reuters",
            ),
        )
        # dev marker ("death toll") plus NO identity link: separate
        status3, e3, _ = decide(
            conn,
            item(
                "Ferry accident in Kariba: death toll rises",
                "Officials raised the number of dead after the ferry "
                "sank on Wednesday.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "NEW"
        assert status3 == "NEW"
        assert len({e1, e2, e3}) == 3
        conn.close()

    def test_chain_merged_update_does_not_broaden_identity(self):
        # A merges B (a real UPDATE).  C shares Colombia with the
        # event but is a different story: it must stay NEW because
        # the identity never absorbed B's vocabulary.
        conn = make_db()
        status1, e1, _ = decide(
            conn,
            item(
                "Earthquake kills 100 in Colombia",
                "A powerful earthquake struck western Colombia, "
                "killing 100 people.",
            ),
        )
        status2, e2, _ = decide(
            conn,
            item(
                "Colombia earthquake death toll rises to 180",
                "Rescuers said the death toll from the Colombia "
                "earthquake had climbed past 180.",
                source="Reuters",
            ),
        )
        # C: different event, same country - must not join the quake
        status3, e3, _ = decide(
            conn,
            item(
                "Colombia floods displace thousands after heavy rain",
                "Rivers burst their banks across the south, forcing "
                "thousands to flee.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "UPDATE"
        assert status3 == "NEW"
        assert e1 == e2
        assert e1 != e3
        conn.close()

    def test_chain_different_storm_never_absorbed(self):
        # A merges a Yagi follow-up; a DIFFERENT storm approaching
        # the same coast must remain its own event.
        conn = make_db()
        status1, e1, _ = decide(
            conn,
            item(
                "Typhoon Yagi hits the Philippines",
                "The storm slammed into the northern coast.",
            ),
        )
        status2, e2, _ = decide(
            conn,
            item(
                "Rescuers search for survivors after Typhoon Yagi",
                "Crews combed flooded villages in the typhoon's path.",
                source="Reuters",
            ),
        )
        status3, e3, _ = decide(
            conn,
            item(
                "Tropical Storm Wanda forms near the Philippines",
                "A new system formed east of the islands.",
                source="Al Jazeera",
            ),
        )
        assert status1 == "NEW"
        assert status2 == "UPDATE"
        assert status3 == "NEW"
        assert e1 == e2
        assert e1 != e3
        conn.close()

    def test_canonical_identity_is_never_broadened_by_updates(self):
        conn = make_db()
        status1, eid, _ = decide(
            conn,
            item(
                "Earthquake kills 100 in Colombia",
                "A powerful earthquake struck western Colombia, "
                "killing 100 people.",
            ),
        )
        decide(
            conn,
            item(
                "Colombia earthquake death toll rises to 180",
                "Rescuers said the death toll from the Colombia "
                "earthquake had climbed past 180.",
                source="Reuters",
            ),
        )
        decide(
            conn,
            item(
                "Government declares national emergency after earthquake",
                "A state of emergency was declared in quake-hit regions.",
                source="Al Jazeera",
            ),
        )
        row = conn.execute(
            "SELECT canonical_title, canonical_summary, canonical_state "
            "FROM events WHERE event_id=?",
            (eid,),
        ).fetchone()
        state = json.loads(row[2])
        identity = state["identity"]
        # Identity stays anchored to the FIRST story - matching is
        # never broadened by later, stronger reports.
        assert identity["title"] == "Earthquake kills 100 in Colombia"
        assert "180" not in identity["numbers"]
        assert "emergency" not in identity["core_words"]
        # The accumulated half carries the developments.
        assert "180" in state["numbers"]
        assert len(state["titles"]) >= 3
        # Canonical CONTENT may improve (Phase E): the best story
        # (death toll, tier-1 Reuters) becomes the event's
        # canonical title/summary, while the identity above is
        # untouched.  The weak "national emergency" report (low
        # strength) must never win.
        assert "180" in row[0]
        assert "national emergency" not in row[0]
        best = state["best_story"]
        assert best["title"] == row[0]
        assert "180" in best["summary"]
        conn.close()


# ---------------------------------------------------------------------------
# Boilerplate / temporal / role words can never create a match
# ---------------------------------------------------------------------------

class TestBoilerplateCannotCreateMatches:
    """Live-blog and generic text must never act as identity:
    "Follow", "Get", "Read", "latest", "update" and day names
    appear in nearly every feed summary."""

    def _assert_no_shared_event(self, pairs, label):
        conn = make_db()
        statuses = []
        for title, summary in pairs:
            status, _, _ = decide(conn, item(title, summary))
            statuses.append(status)
        nevents = conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        conn.close()
        assert nevents == len(pairs), (label, nevents)
        assert all(s == "NEW" for s in statuses), (label, statuses)

    def test_live_blog_boilerplate_never_links(self):
        self._assert_no_shared_event(
            [
                ("Typhoon Yagi makes landfall in the Philippines",
                 "The storm struck the northern coast on Wednesday."),
                ("Follow live: get the latest updates on the storm here",
                 "Read on for the full story as it develops."),
                ("Watch: storm coverage continues through the night",
                 "Join us for the latest."),
            ],
            "boilerplate",
        )

    def test_weekday_alone_never_links(self):
        self._assert_no_shared_event(
            [
                ("Earthquake strikes near Tokyo on Wednesday",
                 "A magnitude 6.2 quake shook buildings at dawn."),
                ("Wednesday's stock market rally fades on Wall Street",
                 "Indexes gave up early gains by the close."),
            ],
            "weekday",
        )

    def test_role_words_never_add_entity_identity(self):
        # "President" is a job title, not an event.  The two
        # stories share Trump + the word president and nothing else.
        self._assert_no_shared_event(
            [
                ("President Trump orders new tariffs on steel imports",
                 "The duties take effect next month."),
                ("Trump says president's job is 'underrated' in speech",
                 "The remarks came during a campaign event."),
            ],
            "roles",
        )


# ---------------------------------------------------------------------------
# Same-attribute adversarial battery (extended)
# ---------------------------------------------------------------------------

class TestAdversarialSeparationExtended:
    """Additional shared-attribute traps: the same generic action
    in the same city, the same deal verb in the same country, and
    two visits by different people to the same place."""

    def _assert_separate(self, pairs, label):
        conn = make_db()
        statuses = []
        now = datetime.now(timezone.utc)
        for i, (title, summary) in enumerate(pairs):
            status, eid, _ = decide(conn, item(title, summary))
            statuses.append(status)
            conn.execute(
                "UPDATE events SET last_seen=? WHERE event_id=?",
                (
                    (now + timedelta(minutes=i * 10)).isoformat(),
                    eid,
                ),
            )
        nevents = conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0]
        conn.close()
        assert nevents == len(pairs), (label, nevents)
        assert all(s == "NEW" for s in statuses), (label, statuses)

    def test_same_generic_action_same_city_different_incident(self):
        self._assert_separate(
            [
                ("Police arrest three suspects in Paris drug bust",
                 "Officers raided an apartment near the station."),
                ("Two people arrested in Paris after jewelry robbery",
                 "The suspects were caught the same evening."),
            ],
            "paris-arrests",
        )

    def test_same_deal_verb_same_country_different_deal(self):
        self._assert_separate(
            [
                ("France signs climate accord with EU partners",
                 "The agreement sets new emissions targets."),
                ("France signs trade deal with Asian bloc",
                 "The pact lowers tariffs on industrial goods."),
            ],
            "france-deals",
        )

    def test_two_visits_same_place_different_people(self):
        self._assert_separate(
            [
                ("Putin visits Tehran for talks with Iran's leaders",
                 "The visit focused on regional security."),
                ("Blinken visits Tehran as prisoner swap talks open",
                 "The US envoy arrived for negotiations."),
            ],
            "tehran-visits",
        )
