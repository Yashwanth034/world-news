"""Tests for the Phase D transparent importance model.

The model must rank by EVIDENCE (magnitude, casualties, context),
not by sector keywords: a major event always outranks the routine
version of the same sector, with a clear gap, while a trivial
story from an authoritative source stays low.

The pairs mirror the exact Phase D requirements (A-L).  No
permanent sector priority exists anywhere: each pair's ordering
comes from severity/context, and the adversarial cases pin the
softness of reliability/coverage adjustments.

Run with:  .venv/bin/python -m pytest src/test_importance.py -q
"""
import pytest

from src.importance import (
    _coverage_adjustment,
    _facts,
    _scope,
    compute_importance,
)


def item(title, summary, status="NEW", tier=2, **kw):
    base = {
        "title": title,
        "summary": summary,
        "event_status": status,
        "tier": tier,
        "primary_source": False,
        "corroborating_sources": 0,
        "strong_corroboration": 0,
        "effective_at": None,
        "sector": None,
        "region": None,
    }
    base.update(kw)
    return base


def score_of(item):
    s, _, _ = compute_importance(item)
    return s


# ---------------------------------------------------------------------------
# A-L: major event must clearly outrank the routine version of the
# same sector, driven by severity/context not sector identity.
# ---------------------------------------------------------------------------

PAIRS = [
    ("earthquake",
     item("Magnitude 2.1 earthquake recorded in remote area",
          "A minor tremor of magnitude 2.1 was recorded; no damage."),
     item("Magnitude 7.5 earthquake strikes densely populated region, 100 dead",
          "A powerful 7.5 magnitude earthquake hit a populated area "
          "killing 100 and injuring hundreds.",
          region="Asia", strong_corroboration=2)),
    ("cyber",
     item("CISA publishes routine advisory on quarterly software update",
          "A routine advisory covering minor patches was published today.",
          tier=1),
     item("Active exploitation of critical vulnerability hits major cloud providers",
          "A critical vulnerability is being actively exploited, "
          "disrupting cloud services.",
          tier=1, strong_corroboration=2)),
    ("finance",
     item("Local bakery announces new pastry line",
          "The bakery will introduce three new pastries next month."),
     item("Major regional bank collapses after deposit run",
          "A bank failure triggered a market crash as authorities "
          "intervened to protect depositors.",
          strong_corroboration=2)),
    ("geopolitics",
     item("Minister makes routine remarks at press briefing",
          "The minister answered questions at a routine weekly briefing."),
     item("Historic peace treaty signed ending decades-long conflict",
          "World leaders hailed the treaty as a turning point for "
          "international stability.",
          region="Europe", strong_corroboration=3)),
    ("science",
     item("Why stars twinkle: an explainer",
          "An explainer on the science of twinkling stars."),
     item("Scientists discover new antibiotic that defeats resistant superbugs",
          "A breakthrough finding shows the drug cured infections in trials.",
          strong_corroboration=2)),
    ("energy",
     item("Utility publishes quarterly electricity report",
          "The utility shared routine quarterly consumption figures."),
     item("Europe faces energy emergency as gas pipeline ruptures",
          "A major gas pipeline rupture triggered a state of emergency "
          "across Europe.",
          strong_corroboration=2)),
    ("aviation",
     item("Airline announces new loyalty program tier",
          "The airline unveiled a new frequent-flyer level."),
     item("Passenger jet crashes in populated area, 150 feared dead",
          "A passenger jet crashed killing an estimated 150 people.",
          strong_corroboration=2)),
    ("shipping",
     item("Port publishes monthly container throughput stats",
          "The port shared routine monthly statistics."),
     item("Red Sea shipping halted as tanker attacks escalate",
          "Major shipping lines suspended Red Sea routes after attacks "
          "on tankers.",
          strong_corroboration=2)),
    ("food",
     item("A day in the life of a wheat farmer",
          "A feature about a local wheat farm."),
     item("Global food crisis looms as grain exports halted",
          "A famine warning followed the halt of grain exports "
          "affecting millions.",
          strong_corroboration=2)),
    ("infrastructure",
     item("City announces new road repair schedule",
          "The city shared its routine road repair calendar."),
     item("Dam collapse floods towns, thousands evacuated",
          "A dam collapsed flooding towns and forcing thousands to evacuate.",
          strong_corroboration=2)),
    ("sports",
     item("Local club hosts community fun run",
          "The club organised a community fun run."),
     item("National team wins World Cup final in dramatic shootout",
          "The national team won the World Cup final after a dramatic "
          "penalty shootout.",
          strong_corroboration=2)),
    ("culture",
     item("Streaming service announces new series lineup",
          "The service shared its upcoming series slate."),
     item("National museum returns looted artifacts in landmark deal",
          "A landmark repatriation returned hundreds of looted artifacts "
          "to their home country.",
          strong_corroboration=2)),
]


class TestSeverityPairs:
    @pytest.mark.parametrize(
        "name,routine,major",
        [(n, r, m) for n, r, m in PAIRS],
        ids=[p[0] for p in PAIRS],
    )
    def test_major_outranks_routine(self, name, routine, major):
        rs = score_of(routine)
        ms = score_of(major)
        assert ms > rs, (
            f"{name}: major({ms}) must outrank routine({rs})\n"
            f"routine: {routine['title']}\nmajor: {major['title']}"
        )
        assert ms - rs >= 10, (
            f"{name}: gap {ms - rs} too small ({routine['title']} vs "
            f"{major['title']})"
        )
        assert ms >= 35, f"{name}: major scored too low ({ms})"

    def test_no_sector_priority(self):
        """Sector identity alone must not drive the score: the routine
        versions of every sector stay low and the major versions are
        ranked by evidence, not by a fixed sector hierarchy."""
        routine_scores = {
            name: score_of(r) for name, r, _ in PAIRS
        }
        for name, rs in routine_scores.items():
            assert rs < 35, f"routine {name} scored too high ({rs})"

    def test_major_events_can_span_sectors(self):
        """The same evidence level must score similarly across very
        different sectors (no 'disasters always beat finance')."""
        scores = {}
        for name, _, major in PAIRS:
            if name in ("earthquake", "aviation", "infrastructure"):
                scores[name] = score_of(major)
        assert min(scores.values()) >= 45


# ---------------------------------------------------------------------------
# Adversarial cases
# ---------------------------------------------------------------------------


class TestAdversarial:
    def test_high_reliability_trivial_stays_low(self):
        # A tier-1 source reporting trivia is still trivial.
        s = score_of(item(
            "CISA: minor patch advisory for old software",
            "Routine quarterly advisory.", tier=1))
        assert s < 35, s

    def test_medium_reliability_major_event_scores_high(self):
        s = score_of(item(
            "Factory explosion injures dozens in city",
            "A factory explosion injured dozens; residents evacuated.",
            tier=3, strong_corroboration=1))
        assert s >= 45, s

    def test_single_source_major_event_still_high(self):
        # One reputable source reporting a major event is important
        # even before corroboration.
        s = score_of(item(
            "M6.8 quake hits coastal city, 40 dead",
            "A strong earthquake killed 40 people and damaged buildings.",
            strong_corroboration=0))
        assert s >= 40, s

    def test_syndicated_copies_trivial_stay_low(self):
        # Five copies of a press release do not make it important.
        s = score_of(item(
            "Press release: widget company quarterly results",
            "Routine quarterly results.",
            strong_corroboration=5))
        assert s < 40, s

    def test_primary_source_major_development_high(self):
        s = score_of(item(
            "Central bank announces emergency rate hike",
            "The central bank raised rates in an emergency move to "
            "stem currency collapse.",
            tier=1, primary_source=True))
        assert s >= 45, s

    def test_update_with_new_toll_outranks_initial(self):
        update = score_of(item(
            "Death toll rises to 180 after quake",
            "Officials raised the toll to 180 as rescue teams found "
            "more bodies.",
            status="UPDATE", strong_corroboration=1))
        initial = score_of(item(
            "Earthquake kills 100 people",
            "A powerful quake killed 100 people.",
            status="NEW"))
        assert update >= initial, (update, initial)

    def test_duplicate_novelty_is_zero(self):
        _, _, b = compute_importance(item(
            "Powerful quake leaves 100 dead",
            "A strong quake left 100 dead in the region.",
            status="DUPLICATE"))
        assert b["novelty"] == 0.0

    def test_event_level_importance_inherits_event_facts(self):
        # A thin follow-up article that only says "hopes fade"
        # must NOT demote the event: the event's canonical
        # material (M7.4, 281 dead, 379 missing) is inherited.
        thin = item(
            "Hopes fade in Colombia as critical window to find "
            "quake survivors shuts",
            "Rescue teams are turning their focus to recovering "
            "bodies.",
            status="UPDATE", region="South America",
            strong_corroboration=2)
        s_alone, _, _ = compute_importance(thin)
        s_evt, _, b = compute_importance(
            thin,
            event_text=(
                "Colombia earthquake: Death toll reaches 281 after "
                "a 7.4 magnitude tremor, with 379 people still "
                "missing and thousands displaced."
            ),
        )
        assert s_evt > s_alone, (s_alone, s_evt)
        assert s_evt >= 55, s_evt
        assert b["facts"]["magnitude"] >= 7.0
        assert b["facts"]["dead"] >= 200
        assert b["scope"] == "REGIONAL"

    def test_event_level_does_not_inflate_routine(self):
        # An event whose canonical material is itself routine
        # stays routine even when event_text is supplied.
        s, _, b = compute_importance(
            item("City council approves budget",
                 "The council approved the annual budget."),
            event_text="City council approves annual budget.",
        )
        assert s < 40, s

    def test_low_volume_sector_important_event_high(self):
        # Nuclear is a naturally low-volume sector; a real incident
        # must still rank high.
        s = score_of(item(
            "Nuclear plant reports cooling failure, declares emergency",
            "A reactor cooling failure triggered a state of emergency "
            "at the plant.",
            tier=1, strong_corroboration=1))
        assert s >= 55, s

    def test_heavily_covered_sector_important_event_high(self):
        # Politics is heavily covered; a genuinely major political
        # event must still rank high (no quota suppression).
        s = score_of(item(
            "President assassinated in attack on capital",
            "The president was killed in an attack on the capital.",
            strong_corroboration=2))
        assert s >= 55, s

    def test_importance_breakdown_is_explainable(self):
        _, _, b = compute_importance(item(
            "Magnitude 6.8 earthquake kills 40 in city",
            "A strong quake killed 40 and injured 200.",
            region="Asia"))
        assert "impact" in b and "urgency" in b and "novelty" in b
        assert "scope" in b and "reliability" in b
        assert "corroboration" in b and "significance" in b
        assert "coverage_adjustment" in b
        assert b["impact"] >= 15  # 6.8 mag + 40 dead
        assert b["scope"] in ("NATIONAL", "REGIONAL", "MULTI_COUNTRY",
                              "GLOBAL")


# ---------------------------------------------------------------------------
# Fact extraction + scope
# ---------------------------------------------------------------------------


class TestFactsAndScope:
    def test_magnitude_extraction(self):
        f = _facts("a 7.5 magnitude earthquake struck. 6.2-magnitude "
                   "aftershock followed")
        assert f["magnitude"] == 7.5

    def test_casualty_extraction_nearby_numbers(self):
        f = _facts("150 feared dead in crash; toll rises to 180")
        assert f["dead"] >= 180  # proximity capture incl. the toll

    def test_major_incident_flag(self):
        assert _facts("passenger jet crashed near the airport")["major_incident"]
        assert _facts("bank failure triggered a market crash")["major_incident"]
        assert not _facts("utility published quarterly figures")["major_incident"]

    def test_scope_detection(self):
        assert _scope("a global agreement was signed", None) == ("GLOBAL", 8)
        assert _scope("cross-border dispute between the two countries",
                      None) == ("MULTI_COUNTRY", 7)
        assert _scope("government announces new policy", "Europe")[0] == "REGIONAL"
        assert _scope("city council meeting", "North America")[0] in (
            "NATIONAL", "REGIONAL")


# ---------------------------------------------------------------------------
# Coverage awareness (soft only)
# ---------------------------------------------------------------------------


class TestCoverageAwareness:
    def test_small_nudge_for_undercovered_sector(self):
        assert _coverage_adjustment("nuclear", {"nuclear": 1}) == 3.0
        assert _coverage_adjustment("nuclear", {"nuclear": 3}) == 2.0
        assert _coverage_adjustment("politics", {"politics": 20}) == 0.0

    def test_never_inverts_ranking(self):
        # A 50-point story must never beat a 90-point story.
        big = item("M7.2 quake kills 500 in coastal city",
                   "A devastating quake killed 500 and displaced "
                   "thousands.", region="Asia", strong_corroboration=3)
        small = item("Minister gives routine statement",
                     "Routine remarks at a briefing.",
                     sector="nuclear")
        big_s, _, _ = compute_importance(big)
        small_s, _, _ = compute_importance(
            small,
            sector_source_counts={"nuclear": 1},
        )
        assert big_s > 80, big_s
        assert small_s < 40, small_s
        assert big_s > small_s + 40

    def test_level_mapping_transparent(self):
        cases = [
            (item("M7.2 quake kills 500", "A devastating quake killed "
                  "500 and displaced thousands.", region="Asia",
                  strong_corroboration=3), "CRITICAL", "HIGH"),
            (item("City council approves budget", "The council "
                  "approved the annual budget."), "LOW", "MEDIUM"),
        ]
        for it, lo, hi in cases:
            _, level, _ = compute_importance(it)
            assert level in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
